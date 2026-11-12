'use strict';
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {resolve} = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');

function harness({cameraDenied = false, officeCapture = false} = {}) {
  const nodes = new Map(), events = {}, intervals = [], requests = [], prompts = {screen: 0};
  let current = {id: null, status: 'idle', office_online: true, office_capture: officeCapture};
  const tracks = [];
  const track = kind => {const value = {kind, stopped: false, addEventListener() {}, stop() {this.stopped = true;}};tracks.push(value);return value;};
  const stream = kinds => {const values = kinds.map(track);return {getTracks: () => values,
    getVideoTracks: () => values.filter(item => item.kind === 'video'), getAudioTracks: () => values.filter(item => item.kind === 'audio')};};
  const node = id => {
    if (!nodes.has(id)) nodes.set(id, {disabled: false, hidden: false, textContent: '',
      listeners: {}, addEventListener(type, handler) {this.listeners[type] = handler;}});
    return nodes.get(id);
  };
  class MediaRecorder {
    static isTypeSupported() {return true;}
    constructor() {this.state = 'inactive';this.listeners = {};}
    start() {this.state = 'recording';}
    addEventListener(event, handler) {this.listeners[event] = handler;}
    stop() {
      this.state = 'inactive';
      queueMicrotask(() => {this.ondataavailable({data: new Blob(['media'])});this.listeners.stop?.();});
    }
  }
  const context = vm.createContext({Blob, Promise, MediaRecorder, navigator: {mediaDevices: {
    getDisplayMedia: async () => {prompts.screen++;return stream(['video']);},
    getUserMedia: async () => {if(cameraDenied)throw new Error('Camera denied');return stream(['video', 'audio']);},
  }}, document: {getElementById: node}, location: {pathname: '/meeting/assigned'},
    window: {MediaRecorder, addEventListener(name, callback) {events[name] = callback;}},
    setInterval(callback) {intervals.push(callback);}, setTimeout(callback) {callback();},
    fetch: async (path, options) => {
      requests.push({path, options});
      let value = {ok: true};
      if (path === '/api/me') value = {role: 'faculty', csrf: 'csrf-token'};
      else if (path.endsWith('/recording')) value = {...current};
      else if (path.endsWith('/start')) {
        const office = JSON.parse(options.body).mode === 'office';
        current = {id: 'session', segment: 0, status: office ? 'starting' : 'awaiting_media', office_online: true,
          mode: office ? 'office' : 'faculty', elapsed: 0};
        value = {...current};
      }
      else if (/\/(pause|stop)$/.test(path)) {current.status = path.endsWith('pause') ? 'pausing' : 'stopping'; value = {...current};}
      else if (path.endsWith('/seal')) current.status = JSON.parse(options.body).action === 'pause' ? 'pausing' : 'stopping';
      else if (path.endsWith('/resume')) {current.segment++;current.status = 'awaiting_media';value = {...current};}
      return {ok: true, json: async () => value};
    },
  });
  vm.runInContext(readFileSync(resolve(__dirname, '../app/static/recorder.js'), 'utf8'), context);
  return {nodes, events, intervals, requests, tracks, prompts, state: () => current};
}

test('Start, Break, Resume and Stop send complete ordered segments without stopping Zoom', async () => {
  const h = harness();
  await new Promise(setImmediate);
  assert.equal(h.nodes.get('recordStart').disabled, true);
  h.events['dt:meeting-connected']();
  assert.equal(h.nodes.get('recordStart').disabled, false);
  await h.nodes.get('recordStart').listeners.click();
  assert.match(h.nodes.get('recordStatus').textContent, /Sending screen/);
  await h.nodes.get('recordBreak').listeners.click();
  const firstSeal = h.requests.find(item => item.path.endsWith('/segments/0/seal'));
  assert.deepEqual(JSON.parse(firstSeal.options.body), {screen_chunks: 1, camera_chunks: 1, action: 'pause'});
  assert.equal(h.tracks.some(track => track.stopped), false); // Keep capture permissions across a break.
  h.state().status = 'paused';
  await h.intervals[0]();
  assert.equal(h.nodes.get('recordResume').disabled, false);
  await h.nodes.get('recordResume').listeners.click();
  await h.nodes.get('recordStop').listeners.click();
  const secondSeal = h.requests.find(item => item.path.endsWith('/segments/1/seal'));
  assert.deepEqual(JSON.parse(secondSeal.options.body), {screen_chunks: 1, camera_chunks: 1, action: 'stop'});
  // Stop releases camera and microphone but keeps the chosen screen for the next recording.
  assert.equal(h.tracks.slice(1).every(track => track.stopped), true);
  assert.equal(h.tracks[0].stopped, false);
  assert.equal(h.requests.some(item => /zoom|join-credentials|logout/.test(item.path)), false);
});

test('only the relevant controls are shown and a second Start does not ask for the screen again', async () => {
  const h = harness();
  await new Promise(setImmediate);
  h.events['dt:meeting-connected']();
  const hidden = id => h.nodes.get(id).hidden;
  assert.deepEqual(['recordStart', 'recordBreak', 'recordResume', 'recordStop'].map(hidden), [false, true, true, true]);
  assert.match(h.nodes.get('recordHint').textContent, /asks once/);
  await h.nodes.get('recordStart').listeners.click();
  assert.deepEqual(['recordStart', 'recordBreak', 'recordResume', 'recordStop'].map(hidden), [true, false, true, false]);
  assert.match(h.nodes.get('recordTimer').textContent, /REC 00:00/);
  await h.nodes.get('recordStop').listeners.click();
  assert.equal(h.nodes.get('recordTimer').textContent, '');
  h.state().status = 'completed'; h.state().output_name = 'DocTutorials-a.mp4';
  await h.intervals[0]();
  assert.equal(hidden('recordStart'), false);
  assert.match(h.nodes.get('recordHint').textContent, /already selected/);
  await h.nodes.get('recordStart').listeners.click();
  assert.equal(h.prompts.screen, 1);  // the screen picker appeared only for the first recording
  assert.equal(h.requests.filter(item => item.path.endsWith('/start')).length, 2);
  h.events.pagehide();
  assert.equal(h.tracks.every(track => track.stopped), true);
});

test('camera permission failure releases screen capture and never creates an office recording', async () => {
  const h = harness({cameraDenied: true});
  await new Promise(setImmediate);
  h.events['dt:meeting-connected']();
  await h.nodes.get('recordStart').listeners.click();
  assert.equal(h.tracks.every(track => track.stopped), true);
  assert.equal(h.requests.some(item => item.path.endsWith('/start')), false);
  assert.match(h.nodes.get('recordMessage').textContent, /Camera denied/);
});


test('office desktop recorder mode sends commands only and never captures on the faculty computer', async () => {
  const h = harness({officeCapture: true});
  await new Promise(setImmediate);
  h.events['dt:meeting-connected']();
  const hidden = id => h.nodes.get(id).hidden;
  assert.match(h.nodes.get('recordHint').textContent, /Nothing is captured on this computer/);
  await h.nodes.get('recordStart').listeners.click();
  assert.equal(h.prompts.screen, 0);
  assert.equal(h.tracks.length, 0);
  assert.equal(JSON.parse(h.requests.find(item => item.path.endsWith('/start')).options.body).mode, 'office');
  assert.match(h.nodes.get('recordStatus').textContent, /joining the webinar/);
  assert.deepEqual(['recordStart', 'recordBreak', 'recordResume', 'recordStop'].map(hidden), [true, true, true, false]);
  h.state().status = 'recording'; h.state().elapsed = 65;
  await h.intervals[0]();
  assert.deepEqual(['recordStart', 'recordBreak', 'recordResume', 'recordStop'].map(hidden), [true, false, true, false]);
  assert.match(h.nodes.get('recordTimer').textContent, /REC 01:05/);
  await h.nodes.get('recordBreak').listeners.click();
  assert.ok(h.requests.some(item => item.path === '/api/recordings/session/pause'));
  h.state().status = 'paused';
  await h.intervals[0]();
  assert.deepEqual(['recordBreak', 'recordResume', 'recordStop'].map(hidden), [true, false, false]);
  await h.nodes.get('recordResume').listeners.click();
  assert.ok(h.requests.some(item => item.path === '/api/recordings/session/resume'));
  h.state().status = 'recording';
  await h.intervals[0]();
  await h.nodes.get('recordStop').listeners.click();
  assert.ok(h.requests.some(item => item.path === '/api/recordings/session/stop'));
  h.events.pagehide();  // closing the tab must not abort an office recording
  assert.equal(h.requests.some(item => item.path.endsWith('/abort')), false);
});
