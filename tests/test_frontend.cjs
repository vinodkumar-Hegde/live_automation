'use strict';
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {resolve} = require('node:path');
const {test} = require('node:test');
const vm = require('node:vm');

function meetingContext() {
  const nodes = new Map();
  const scripts = [];
  const context = vm.createContext({
    URLSearchParams,
    window: {location: {pathname: '/meeting/assigned-id', search: '', origin: 'https://dt.test'}},
    // Leave automatic entry pending so these tests can exercise the SDK independently.
    fetch: () => new Promise(() => {}),
    document: {
      getElementById: id => {
        if (!nodes.has(id)) nodes.set(id, {textContent: '', addEventListener() {}});
        return nodes.get(id);
      },
      querySelectorAll: () => [],
      createElement: () => ({}),
      head: {appendChild(script) {
        scripts.push(script.src);
        if (script.src.includes('/zoom-meeting-')) {
          context.window.ZoomMtg = {setZoomJSLib() {}, preLoadWasm() {}, prepareWebSDK() {}};
        }
        script.onload();
      }},
    },
  });
  vm.runInContext(readFileSync(resolve(__dirname, '../app/static/meeting.js'), 'utf8'), context);
  return {context, nodes, scripts};
}

test('SDK loader includes all dependencies in order and the documented client URL', async () => {
  const {context, scripts} = meetingContext();
  await vm.runInContext('sdk()', context);
  assert.deepEqual(scripts.map(url => url.split('/').pop()), [
    'react.min.js', 'react-dom.min.js', 'redux.min.js', 'redux-thunk.min.js',
    'react-redux.min.js', 'lodash.min.js', 'zoom-meeting-6.10.0.min.js',
  ]);
  assert.equal(scripts.at(-1), 'https://source.zoom.us/zoom-meeting-6.10.0.min.js');
});

for (const mode of ['callbacks', 'promises', 'both']) {
  test(`SDK ${mode} complete initialization before joining exactly once`, async () => {
    const {context, nodes} = meetingContext();
    let initialized = false;
    let joins = 0;
    context.zoomMock = {
      init(options) {
        initialized = true;
        if (mode !== 'promises') options.success();
        if (mode !== 'callbacks') return Promise.resolve();
      },
      join(options) {
        assert.equal(initialized, true);
        joins++;
        if (mode !== 'promises') options.success();
        if (mode !== 'callbacks') return Promise.resolve();
      },
    };
    await vm.runInContext('startZoom(zoomMock, {})', context);
    assert.equal(joins, 1);
    assert.match(nodes.get('status').textContent, /^Connected/);
  });
}

for (const stage of ['init', 'join']) {
  test(`SDK ${stage} Promise rejection reaches the error handler`, async () => {
    const {context, nodes} = meetingContext();
    context.zoomMock = {
      init: () => stage === 'init' ? Promise.reject({errorCode: 300}) : Promise.resolve(),
      join: () => Promise.reject({errorCode: 300}),
    };
    await assert.rejects(vm.runInContext('startZoom(zoomMock, {})', context), /errorCode: 300/);
    assert.doesNotMatch(nodes.get('status').textContent, /^Connected/);
  });
}

test('a fulfilled SDK error response is rejected and never shown as Connected', async () => {
  const {context, nodes} = meetingContext();
  context.zoomMock = {
    init: options => {assert.equal(options.isShowJoiningErrorDialog, false);return Promise.resolve();},
    join: () => Promise.resolve({status: false, errorCode: 1, errorMessage: 'Join refused',
      result: {tid: 'v=2.0;clid=us06;rid=WEB_test'}}),
  };
  await assert.rejects(vm.runInContext('startZoom(zoomMock, {})', context), /Join refused.*tracking ID/);
  assert.doesNotMatch(nodes.get('status').textContent, /^Connected/);
});

test('zero SDK result codes are accepted as success', async () => {
  for (const code of [0, '0']) {
    const {context} = meetingContext();
    context.resultCode = code;
    await vm.runInContext('zoomCall(() => Promise.resolve({errorCode:resultCode}), {}, "SDK failed")', context);
  }
});

test('expired webinar transport refusal is explained even without an SDK callback', async () => {
  const {context, nodes} = meetingContext();
  const reports=[];
  context.fetch=async (path,options)=>{reports.push(JSON.parse(options.body));return {ok:true};};
  context.window.Worker=class {
    addEventListener(name,handler){this.handler=handler;}
  };
  vm.runInContext(`csrf='test-csrf'; joinStage='join'; watchZoomJoinResult();
    const transport=new window.Worker();
    transport.handler({data:{type:'message',data:JSON.stringify({evt:4098,body:{res:3038}})}});`,context);
  await new Promise(setImmediate);
  assert.match(nodes.get('error').textContent,/webinar has expired.*3038/);
  assert.match(nodes.get('error').textContent,/reschedule.*future date\/time/);
  assert.equal(nodes.get('retry').hidden,false);
  assert.equal(reports[0].code,'3038');
});

test('join diagnostics preserve useful codes and redact tokens without an automatic reload', async () => {
  const {context, nodes} = meetingContext();
  const sent = [];
  context.window.location.replace = () => {throw Error('Generic join failure must not reload the page');};
  context.fetch = async (path, options) => {sent.push({path, body: JSON.parse(options.body)});return {ok: true};};
  vm.runInContext(`csrf='test-csrf'; joinStage='join';
    const issue = new Error('errorCode: 1 password=private-pass zak=eyJhbGciOiJIUzI1NiJ9.eyJkYXRhIjoiU0VDUkVUIn0.c2lnbmF0dXJl');
    issue.zoomFailure = {errorCode:1,result:{tid:'v=2.0;clid=us06;rid=WEB_test'}}; fail(issue);`, context);
  await new Promise(setImmediate);
  assert.equal(sent.length, 1);
  assert.equal(sent[0].body.code, '1');
  assert.match(sent[0].body.tracking_id, /WEB_test/);
  assert.doesNotMatch(JSON.stringify(sent), /private-pass|eyJhbGciOi/);
  assert.match(nodes.get('error').textContent, /Diagnostic saved/);
});

test('insecure LAN pages show an HTTPS explanation before calling the SDK', async () => {
  const {context, nodes} = meetingContext();
  context.window.isSecureContext = false;
  let requests = 0;
  context.fetch = async () => {requests++;throw Error('Must not request join credentials');};
  vm.runInContext('entering = false', context);
  await vm.runInContext('enter()', context);
  assert.equal(requests, 0);
  assert.match(nodes.get('error').textContent, /require HTTPS/);
});

function dashboardContext({failure = false, connected = true, assignments = [], search = ''} = {}) {
  const nodes = new Map();
  const state = {role: 'faculty', requests: [], navigations: []};
  const node = id => {
    if (!nodes.has(id)) nodes.set(id, {
      hidden: true, textContent: '', className: '', listeners: {},
      addEventListener(event, handler) { this.listeners[event] = handler; },
      replaceChildren() {}, add() {}, append() {},
    });
    return nodes.get(id);
  };
  const context = vm.createContext({
    URLSearchParams, Date,
    FormData: class {constructor(form) { this.form = form; } entries() {return Object.entries(this.form.values);}},
    Option: class {},
    location: {search, pathname: '/'},
    history: {replaceState() {}},
    window: {location: {assign(url) {state.navigations.push(url);}}},
    document: {getElementById: node, createElement: () => ({append() {}})},
    fetch: async (path, options) => {
      state.requests.push({path, options});
      let result = [];
      if (path === '/api/me') result = {role: state.role, display_name: state.role, csrf: 'test-csrf'};
      if (path === '/api/zoom/status') result = {connected, configured: true, zoom_email: 'faculty@dt.test'};
      if (path === '/api/assignments') result = assignments;
      if (path === '/api/admin/zoom-setup') result = {redirect_uri: 'https://dt.test/auth/zoom/callback', sdk_configured: true, api_configured: true};
      return {ok: !failure, status: failure ? 503 : 200,
        json: async () => failure ? {detail: 'Service unavailable'} : result};
    },
  });
  vm.runInContext(readFileSync(resolve(__dirname, '../app/static/app.js'), 'utf8'), context);
  return {context, nodes, state};
}

test('faculty dashboard has no Zoom account connection step', async () => {
  const {context, nodes, state} = dashboardContext();
  await new Promise(setImmediate);
  assert.equal(nodes.get('zoomSetup').hidden, true);
  assert.equal(state.requests.some(item => item.path.startsWith('/api/zoom/')), false);
  state.role = 'admin';
  await vm.runInContext('showApp(false)', context);
  assert.equal(nodes.get('zoomSetup').hidden, false);
});

test('dashboard startup failures leave a usable login with the error message', async () => {
  const {nodes} = dashboardContext({failure: true});
  await new Promise(setImmediate);
  assert.equal(nodes.get('auth').hidden, false);
  assert.equal(nodes.get('dashboard').hidden, true);
  assert.equal(nodes.get('logout').hidden, true);
  assert.equal(nodes.get('loginMessage').textContent, 'Service unavailable');
});

test('assignment form sends local schedule as a UTC instant', async () => {
  const {nodes, state} = dashboardContext();
  await new Promise(setImmediate);
  const input = '2026-11-12T10:00';
  const button = {};
  const form = {values: {starts_at: input}, querySelector: () => button, reset() {}};
  await nodes.get('assignForm').listeners.submit({preventDefault() {}, currentTarget: form, target: form});
  const request = state.requests.find(item => item.path === '/api/admin/assignments');
  assert.equal(JSON.parse(request.options.body).starts_at, new Date(input).toISOString());
  assert.equal(button.disabled, false);
});

test('faculty login opens the assigned webinar without Zoom OAuth', async () => {
  const {context, state} = dashboardContext({connected: false, assignments: [
    {id: 'assigned', status: 'scheduled', topic: 'Topic'},
  ]});
  await new Promise(setImmediate);
  assert.deepEqual(state.navigations, []);
  await vm.runInContext('showApp(true)', context);
  assert.deepEqual(state.navigations, ['/meeting/assigned']);
  assert.equal(state.requests.some(item => item.path.startsWith('/api/zoom/')), false);
});

test('faculty can choose between several webinars without Zoom authorization', async () => {
  const {context, state} = dashboardContext({connected: false, assignments: [
    {id: 'first', status: 'scheduled', topic: 'First'},
    {id: 'second', status: 'scheduled', topic: 'Second'},
  ]});
  await new Promise(setImmediate);
  await vm.runInContext('showApp(true)', context);
  assert.deepEqual(state.navigations, []);
});

test('admin can see the exact redirect URL and faculty cannot see the setup panel', async () => {
  const {context, nodes, state} = dashboardContext();
  await new Promise(setImmediate);
  assert.equal(nodes.get('zoomSetup').hidden, true);
  state.role = 'admin';
  await vm.runInContext('showApp(false)', context);
  assert.equal(nodes.get('zoomSetup').hidden, false);
  assert.equal(nodes.get('zoomRedirect').value, 'https://dt.test/auth/zoom/callback');
  state.role = 'faculty';
  await vm.runInContext('showApp(false)', context);
  assert.equal(nodes.get('zoomSetup').hidden, true);
});

test('faculty creation uses the host email when advanced identity is blank', async () => {
  const {nodes, state} = dashboardContext();
  await new Promise(setImmediate);
  const form = {values: {email: 'faculty@dt.test', zoom_user_id: ''},
    querySelector: () => ({}), reset() {}};
  await nodes.get('facultyForm').listeners.submit({preventDefault() {}, currentTarget: form, target: form});
  const request = state.requests.find(item => item.path === '/api/admin/faculty');
  assert.equal(JSON.parse(request.options.body).zoom_user_id, 'faculty@dt.test');
});
