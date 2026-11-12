'use strict';
/* DocTutorials Meeting SDK client-view adapter.
 * Zoom 6.10.0: Oct 22 2026 WebRTC reliability update. See release notes.
 * No Zoom secrets are stored in this file or written to the console.
 */
const version = '6.10.0';
const assignmentId = decodeURIComponent(window.location.pathname.split('/').pop());
const compatibilityMode = new URLSearchParams(window.location.search).get('compat') === '1';
const heading = document.getElementById('heading');
const status = document.getElementById('status');
const error = document.getElementById('error');
const retry = document.getElementById('retry');
let csrf = '';
let entering = false;
let joinStage = 'authorization';
let transportFailure = null;

function hideSdk() {
  document.querySelectorAll('#zmmtg-root, .ReactModalPortal').forEach(el => {
    el.style.setProperty('display', 'none', 'important');
  });
}
function showSdk() {
  document.querySelectorAll('#zmmtg-root, .ReactModalPortal').forEach(el => {
    el.style.removeProperty('display');
  });
}
function safeReason(reason) {
  // Never stringify SDK objects: they may contain ZAK, signature or other credentials.
  if (!reason || typeof reason !== 'object') return redactDiagnostic(reason || 'Unknown SDK error').slice(0, 180);
  const fields = ['errorCode', 'logCode', 'type', 'reason', 'exception', 'status', 'errorMessage', 'message'];
  const detail = fields.filter(k => ['string', 'number', 'boolean'].includes(typeof reason[k]))
    .map(k => `${k}: ${redactDiagnostic(reason[k]).slice(0, 180)}`);
  const tid = reason.result?.tid || reason.tid;
  if (typeof tid === 'string') detail.push('tracking ID: ' + redactDiagnostic(tid).slice(0, 200));
  return detail.join(' | ') || 'SDK join returned no diagnostic fields';
}
function redactDiagnostic(value) {
  return String(value)
    .replace(/\beyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b/g, '[redacted token]')
    .replace(/\b(zak|signature|password|passWord|access_token|refresh_token)(["']?\s*[:=]\s*["']?)[^\s"'&,}]+/gi, '$1$2[redacted]');
}
function explainZoomJoinCode(code) {
  if (String(code) === '3038') {
    return 'This Zoom webinar has expired (Zoom code 3038). Ask the administrator to edit the webinar in Zoom '
      + 'and reschedule it to a future date/time, or assign a new webinar ID. Then return here and retry.';
  }
  return '';
}
function saveJoinFailure(err) {
  if (!csrf) return Promise.resolve(false);
  const raw = err.zoomFailure || {};
  const code = String(transportFailure?.code ?? raw.errorCode ?? 'unknown').replace(/[^A-Za-z0-9_-]/g, '_').slice(0, 30);
  const tracking = raw.result?.tid || raw.tid || '';
  return fetch('/api/assignments/' + encodeURIComponent(assignmentId) + '/zoom-diagnostics', {
    method: 'POST', credentials: 'same-origin', keepalive: true,
    headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf},
    body: JSON.stringify({stage: joinStage, code,
      reason: redactDiagnostic(err.message || safeReason(raw)).slice(0, 300),
      tracking_id: /^[A-Za-z0-9_.;=:-]{0,200}$/.test(tracking) ? tracking : ''}),
  }).then(response => response.ok).catch(() => false);
}
function fail(err) {
  hideSdk();
  const message = explainZoomJoinCode(transportFailure?.code ?? err.zoomFailure?.errorCode)
    || redactDiagnostic(err instanceof Error ? err.message : String(err));
  status.textContent = 'Unable to enter webinar';
  error.textContent = message + (transportFailure ? `\nZoom transport result: ${transportFailure.code}` : '')
    + '\n\nZoom could not complete this connection.';
  saveJoinFailure(err).then(saved => {
    if (saved) error.textContent += '\nDiagnostic saved for your administrator; tokens and passcodes are excluded.';
  });
  retry.hidden = false;
  retry.disabled = false;
}
async function api(path, options = {}) {
  const response = await fetch(path, {
    credentials: 'same-origin',
    ...options,
    headers: {'Content-Type':'application/json', 'X-CSRF-Token':csrf, ...(options.headers || {})},
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const err = new Error(typeof payload.detail === 'string' ? payload.detail : `HTTP ${response.status}`);
    throw err;
  }
  return payload;
}
function loadScript(url) {
  return new Promise((resolve, reject) => {
    const script = document.createElement('script');
    script.src = url;
    script.onload = resolve;
    script.onerror = () => reject(new Error(`Zoom SDK resource failed to load (${version}). Check Zoom CDN connectivity.`));
    document.head.appendChild(script);
  });
}
// Diagnostic only. Zoom displays every join refusal it has no text for as
// "Error Code: 1". Its transport worker hands the decoded join response
// (event 4098) to the page; read just the numeric result so the real code can
// be shown. No tokens or message bodies are kept or logged.
function watchZoomJoinResult() {
  const Native = window.Worker;
  if (!Native || Native.dtWatched) return;
  const inspect = event => {
    const data = event && event.data;
    if (!data || data.type !== 'message' || typeof data.data !== 'string' || !/"evt"\s*:\s*4098\b/.test(data.data)) return;
    try {
      const body = JSON.parse(data.data).body || {};
      if (typeof body.res !== 'number' || body.res === 0) return;
      transportFailure = {code: body.res};
      const explanation = explainZoomJoinCode(body.res);
      const text = explanation || (`Zoom refused the join. Real Zoom code: ${body.res}`
        + (typeof body.resStr === 'string' && body.resStr ? ` (${redactDiagnostic(body.resStr).slice(0, 160)})` : ''));
      const refusal = new Error(text);
      refusal.zoomFailure = {errorCode: body.res};
      if (explanation) {
        // Show a known terminal refusal even when the SDK never calls its error callback.
        fail(refusal);
        return;
      }
      saveJoinFailure(refusal);
      console.warn('[DocTutorials] ' + text);
      let banner = document.getElementById('zoomJoinCode');
      if (!banner) {
        banner = document.createElement('p');
        banner.id = 'zoomJoinCode';
        banner.setAttribute('role', 'alert');
        banner.style.cssText = 'position:fixed;left:16px;bottom:16px;z-index:2147483001;margin:0;padding:10px 14px;'
          + 'background:#fff;color:#b42318;border:1px solid #f0b4ad;border-radius:8px;font:600 14px -apple-system,Arial;max-width:70vw';
        document.body.appendChild(banner);
      }
      banner.textContent = text;
    } catch { /* Not a JSON join response; ignore. */ }
  };
  class Watched extends Native {
    constructor(...args) { super(...args); this.addEventListener('message', inspect); }
  }
  Watched.dtWatched = true;
  window.Worker = Watched;
}
async function sdk() {
  if (window.ZoomMtg) return window.ZoomMtg;
  watchZoomJoinResult();
  const vendor = `https://source.zoom.us/${version}/lib/vendor/`;
  for (const file of ['react.min.js','react-dom.min.js','redux.min.js','redux-thunk.min.js','react-redux.min.js','lodash.min.js']) {
    await loadScript(vendor + file);
  }
  await loadScript(`https://source.zoom.us/zoom-meeting-${version}.min.js`);
  if (!window.ZoomMtg) throw new Error('Zoom Meeting SDK did not initialize. Inspect Network for the SDK JavaScript request.');
  window.ZoomMtg.setZoomJSLib(`https://source.zoom.us/${version}/lib`, '/av');
  window.ZoomMtg.preLoadWasm();
  window.ZoomMtg.prepareWebSDK();
  return window.ZoomMtg;
}
function zoomCall(method, options, label) {
  return new Promise((resolve, reject) => {
    const failed = reason => {
      const err = new Error(label + ': ' + safeReason(reason));
      err.zoomFailure = reason && typeof reason === 'object' ? reason : {};
      reject(err);
    };
    const succeeded = result => {
      // Some SDK API wrappers fulfill with a failure response instead of rejecting.
      const hasErrorCode = result && result.errorCode != null && !['', '0'].includes(String(result.errorCode));
      if (result && (result.status === false || (hasErrorCode && result.status !== true))) failed(result);
      else resolve(result);
    };
    try {
      const result = method({...options, success: succeeded, error: failed});
      // Both init and join can use callbacks, Promises, or both depending on SDK version.
      if (result && typeof result.then === 'function') result.then(succeeded, failed);
    } catch (e) { reject(e); }
  });
}
async function startZoom(ZoomMtg, info) {
  status.textContent = 'Connecting to assigned Zoom Webinar…';
  joinStage = 'initialization';
  await zoomCall(ZoomMtg.init.bind(ZoomMtg), {
    leaveUrl: window.location.origin + '/',
    isShowJoiningErrorDialog: false,
    patchJsMedia: true,
  }, 'Zoom initialization failed');
  joinStage = 'join';
  await zoomCall(ZoomMtg.join.bind(ZoomMtg), {
    signature: info.signature,
    meetingNumber: info.meetingNumber,
    passWord: info.passWord || '',
    userName: info.userName,
    userEmail: info.userEmail,
    zak: info.zak,
  }, 'Zoom join failed');
  status.textContent = 'Connected. Camera, audio, screen sharing and Zoom cloud recording are available in the Zoom toolbar subject to host permissions.';
  if (window.dispatchEvent) window.dispatchEvent(new Event('dt:meeting-connected'));
}
async function enter() {
  if (entering) return;
  entering = true;
  retry.hidden = true;
  retry.disabled = true;
  error.textContent = '';
  // Do not show the opaque Zoom overlay before the Zoom host is verified.
  hideSdk();
  try {
    if (window.isSecureContext === false) {
      throw new Error('Zoom camera and microphone access require HTTPS. Open the public HTTPS DocTutorials link, '
        + 'or http://127.0.0.1:8792 when using this Mac. A plain HTTP LAN address cannot host the faculty webinar.');
    }
    const person = await api('/api/me');
    csrf = person.csrf;
    if (person.role !== 'faculty') throw new Error('Only assigned faculty can start webinars.');
    const assigned = await api('/api/assignments/' + encodeURIComponent(assignmentId));
    heading.textContent = assigned.topic;
    document.getElementById('metadata').textContent = `${assigned.course} · ${assigned.category} · ${assigned.subject}`;
    status.textContent = 'Verifying assigned Zoom host…';
    // The backend checks authorization and webinar ownership, then creates a fresh ZAK/signature.
    const join = await api('/api/assignments/' + encodeURIComponent(assignmentId) + '/join-credentials', {
      method:'POST', body: JSON.stringify({compatibility: compatibilityMode})
    });
    if (!join.signature || !join.zak || !join.meetingNumber || !join.userEmail) {
      throw new Error('The backend returned incomplete webinar join credentials.');
    }
    status.textContent = `Loading Zoom Meeting SDK ${version}…`;
    joinStage = 'sdk_load';
    const zoom = await sdk();
    showSdk();
    await startZoom(zoom, join);
  } catch (e) {
    fail(e);
  } finally {
    entering = false;
  }
}
retry.addEventListener('click', () => {
  // Request fresh authorization after the administrator fixes the Zoom webinar.
  window.location.replace(window.location.pathname);
});
enter();
