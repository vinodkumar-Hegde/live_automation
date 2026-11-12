'use strict';
/* DocTutorials office desktop recorder.
 *
 * For every class the faculty starts recording, this app opens one window, joins
 * the Zoom webinar in it as a view-only attendee and records that window (video and
 * webinar audio) to this computer. Faculty Start / Break / Resume / Stop arrive
 * through the DocTutorials server; nothing is captured on the faculty computer.
 * Several classes can be recorded at once, one window each.
 */
const {app, BrowserWindow, ipcMain, session, shell} = require('electron');
const fs = require('node:fs');
const http = require('node:http');
const net = require('node:net');
const os = require('node:os');
const path = require('node:path');
const {spawn} = require('node:child_process');

const TEST = process.env.DT_CAPTURE_TEST === '1';  // record a generated test scene instead of Zoom
const SDK_VERSION = '6.10.0';
const CONFIG = process.env.DT_OFFICE_CONFIG || path.join(app.getPath('userData'), 'config.json');
const JOIN_RETRY_SECONDS = 10, JOIN_ATTEMPTS = 60;  // keep trying for ten minutes if the webinar has not started

// Keep rendering and capturing while a recording window is covered or in the background.
app.commandLine.appendSwitch('autoplay-policy', 'no-user-gesture-required');
app.commandLine.appendSwitch('disable-renderer-backgrounding');
app.commandLine.appendSwitch('disable-backgrounding-occluded-windows');
app.commandLine.appendSwitch('disable-background-timer-throttling');
app.commandLine.appendSwitch('disable-features', 'CalculateNativeWinOcclusion');

let config = null, ui = null, pageOrigin = '', ffmpegPath = '', ffprobePath = '';
const info = {status: 'unpaired', message: 'Pair this computer with the DocTutorials application.', name: ''};
const live = new Map();     // recording id -> local capture
const byWindow = new Map(); // webContents id -> recording id
const finished = [];        // recently saved recordings, newest first

function log(...parts) { console.log(new Date().toISOString(), ...parts); }

function lanHost(host) {
  if (!host) return false;
  if (host === 'localhost' || host.endsWith('.local')) return true;
  if (net.isIPv4(host)) {
    const [a, b] = host.split('.').map(Number);
    return a === 127 || a === 10 || (a === 172 && b >= 16 && b <= 31) || (a === 192 && b === 168) || (a === 169 && b === 254);
  }
  return false;
}
function validate(value) {
  const server = String(value.server || '').trim().replace(/\/+$/, '');
  let url;
  try { url = new URL(server); } catch { throw new Error('Enter the application address, for example http://192.168.1.20:8792'); }
  if (url.username || url.password || url.search || url.hash || !['', '/'].includes(url.pathname))
    throw new Error('Enter only the application address, without a path');
  if (url.protocol !== 'https:' && !(url.protocol === 'http:' && lanHost(url.hostname)))
    throw new Error('Use the HTTPS address, or http:// with the application computer\'s LAN address');
  const token = String(value.token || '').trim();
  if (!token) throw new Error('Paste the office pairing code from the admin dashboard');
  const outputDir = path.resolve(String(value.outputDir || '').trim() || path.join(os.homedir(), 'Movies', 'DocTutorials'));
  fs.mkdirSync(outputDir, {recursive: true});
  fs.accessSync(outputDir, fs.constants.W_OK);
  const number = (raw, low, high, fallback) => Number.isInteger(Number(raw)) && raw >= low && raw <= high ? Number(raw) : fallback;
  return {server: url.origin, token, outputDir, capacity: number(value.capacity, 1, 8, 3),
          width: number(value.width, 640, 3840, 1280), height: number(value.height, 360, 2160, 720),
          hideZoomControls: value.hideZoomControls !== false};
}
function loadConfig() {
  try { config = validate(JSON.parse(fs.readFileSync(CONFIG, 'utf8'))); }
  catch { config = null; }
}
function saveConfig(value) {
  fs.mkdirSync(path.dirname(CONFIG), {recursive: true});
  fs.writeFileSync(CONFIG, JSON.stringify(value), {mode: 0o600});
  fs.chmodSync(CONFIG, 0o600);
}

async function api(method, route, body, settings = config) {
  const response = await fetch(settings.server + route, {
    method, signal: AbortSignal.timeout(15000),
    headers: {'Authorization': 'Bearer ' + settings.token, ...(body ? {'Content-Type': 'application/json'} : {})},
    body: body ? JSON.stringify(body) : undefined,
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(typeof payload.detail === 'string' ? payload.detail : `Request failed (${response.status})`);
    error.status = response.status;
    throw error;
  }
  return payload;
}
const report = (sid, body) => api('POST', `/api/office/recordings/${sid}/report`, body);

function findTool(name) {
  const folders = ['/opt/homebrew/bin', '/usr/local/bin', '/usr/bin', ...(process.env.PATH || '').split(path.delimiter)];
  for (const folder of folders) {
    const candidate = path.join(folder, process.platform === 'win32' ? name + '.exe' : name);
    try { fs.accessSync(candidate, fs.constants.X_OK); return candidate; } catch { /* next */ }
  }
  return '';
}
function run(tool, args) {
  return new Promise((resolve, reject) => {
    const child = spawn(tool, args, {stdio: ['ignore', 'pipe', 'pipe']});
    let out = '', err = '';
    child.stdout.on('data', data => { out += data; });
    child.stderr.on('data', data => { err = (err + data).slice(-4000); });
    child.on('error', reject);
    child.on('close', code => code === 0 ? resolve(out) : reject(new Error(err || `${path.basename(tool)} failed (${code})`)));
  });
}

// The capture page is served from this computer only, so it runs in a secure context
// (needed for Zoom audio) and never needs the pairing code.
function startPageServer() {
  const files = {'/capture.html': 'text/html; charset=utf-8', '/capture.js': 'text/javascript; charset=utf-8',
                 '/left.html': 'text/html; charset=utf-8'};
  const server = http.createServer((request, response) => {
    const route = (request.url || '').split('?')[0];
    if (!files[route]) { response.writeHead(404); response.end(); return; }
    response.writeHead(200, {'Content-Type': files[route], 'Cache-Control': 'no-store'});
    response.end(route === '/left.html' ? '<!doctype html><title>Webinar ended</title>'
      : fs.readFileSync(path.join(__dirname, 'capture', route.slice(1))));
  });
  return new Promise(resolve => server.listen(0, '127.0.0.1', () => {
    pageOrigin = `http://127.0.0.1:${server.address().port}`;
    resolve();
  }));
}

function folder(sid) {
  if (!/^[a-f0-9]{32}$/.test(sid)) throw new Error('Invalid recording identifier');
  return path.join(config.outputDir, sid);
}
function parts(sid) {
  try {
    return fs.readdirSync(folder(sid)).filter(name => /^part-\d{4}\.webm$/.test(name)).sort()
      .map(name => path.join(folder(sid), name)).filter(file => fs.statSync(file).size > 0);
  } catch { return []; }
}

function begin(remote) {
  const sid = remote.id;
  const item = {sid, topic: remote.topic || 'Webinar', state: 'joining', message: 'Joining the webinar…', attempts: 0,
                stream: null, bytes: 0, elapsed: 0, since: 0, win: null, pending: null, ending: false, timer: null};
  live.set(sid, item);
  fs.mkdirSync(folder(sid), {recursive: true});
  openWindow(item);
}
function openWindow(item) {
  const partition = 'capture-' + item.sid;  // separate in-memory profile for every class
  const store = session.fromPartition(partition);
  store.setDisplayMediaRequestHandler((request, callback) => callback({video: request.frame, audio: request.frame}));
  // The recorder only watches and listens: it never uses this computer's camera or microphone.
  store.setPermissionRequestHandler((_contents, permission, callback) => callback(permission === 'display-capture'));
  const win = new BrowserWindow({
    width: config.width, height: config.height, useContentSize: true, show: true, title: 'Recording: ' + item.topic,
    backgroundColor: '#000000', autoHideMenuBar: true, enableLargerThanScreen: true,
    webPreferences: {preload: path.join(__dirname, 'capture-preload.js'), partition, backgroundThrottling: false,
                     contextIsolation: true, nodeIntegration: false},
  });
  item.win = win;
  // Present as ordinary Chrome so Zoom's web client takes its normal, supported path.
  win.webContents.setUserAgent(win.webContents.getUserAgent().replace(/\s(Electron|doctutorials-office-recorder)\/\S+/g, ''));
  const id = win.webContents.id;
  byWindow.set(id, item.sid);
  win.on('page-title-updated', event => event.preventDefault());
  win.webContents.setWindowOpenHandler(() => ({action: 'deny'}));
  win.webContents.on('did-navigate', (_event, url) => { if (url.endsWith('/left.html')) ended(item, 'The webinar ended.'); });
  win.on('closed', () => {
    byWindow.delete(id);
    if (item.win === win) { item.win = null; if (!item.ending) ended(item, 'The recording window was closed.'); }
  });
  win.loadURL(pageOrigin + '/capture.html');
}
function closeWindow(item) {
  const win = item.win;
  item.win = null;
  if (win && !win.isDestroyed()) win.destroy();
}
function endPart(item) {
  if (item.since) { item.elapsed += Date.now() - item.since; item.since = 0; }
  const stream = item.stream;
  item.stream = null;
  return stream ? new Promise(resolve => stream.end(resolve)) : Promise.resolve();
}
function command(item, action) {
  if (item.pending === action || !item.win || item.win.isDestroyed()) return;
  item.pending = action;
  item.win.webContents.send('capture:command', action);
}

// The webinar ended, the window was closed, or the faculty pressed Stop: save what was recorded.
async function ended(item, reason) {
  if (item.ending) return;
  item.ending = true;
  clearTimeout(item.timer);
  item.message = reason;
  if (item.win && !item.win.isDestroyed() && item.stream) {
    // Let the page flush its last chunk before the window goes away.
    command(item, 'stop');
    await new Promise(resolve => { item.flushed = resolve; setTimeout(resolve, 5000); });
  }
  await endPart(item);
  closeWindow(item);
  await finalize(item);
}
async function finalize(item) {
  const sid = item.sid, files = parts(sid);
  item.state = 'processing'; item.message = 'Creating the MP4…';
  try {
    if (!files.length) throw new Error('No video was captured from the webinar. ' + (item.reason || ''));
    await report(sid, {status: 'processing'}).catch(() => {});
    const output = path.join(folder(sid), `DocTutorials-${sid}.mp4`);
    const even = 'scale=trunc(iw/2)*2:trunc(ih/2)*2,fps=30';
    let inputs;
    if (files.length === 1) inputs = ['-i', files[0]];
    else {
      const list = path.join(folder(sid), 'parts.txt');
      fs.writeFileSync(list, files.map(file => `file '${file.replace(/'/g, "'\\''")}'`).join('\n'));
      inputs = ['-f', 'concat', '-safe', '0', '-i', list];
    }
    await run(ffmpegPath, ['-y', '-hide_banner', '-loglevel', 'error', ...inputs, '-vf', even, '-c:v', 'libx264', '-preset', 'veryfast',
                           '-crf', '21', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '160k', '-movflags', '+faststart', output]);
    const probe = JSON.parse(await run(ffprobePath, ['-v', 'error', '-show_entries', 'format=duration:stream=codec_type', '-of', 'json', output]));
    const duration = Number(probe.format && probe.format.duration) || 0;
    const kinds = (probe.streams || []).map(stream => stream.codec_type);
    if (!kinds.includes('video') || duration <= 0) throw new Error('The saved MP4 could not be verified. Source parts are preserved.');
    const result = {status: 'completed', output_name: path.basename(output), output_bytes: fs.statSync(output).size, output_duration: duration};
    item.state = 'completed'; item.output = output;
    item.message = kinds.includes('audio') ? 'Saved' : 'Saved without audio (the webinar audio was not available)';
    await deliver(sid, result);
  } catch (error) {
    log('finalize failed', sid, error.message);
    item.state = 'failed'; item.message = error.message.slice(0, 280);
    await deliver(sid, {status: 'failed', error: item.message});
  }
  finished.unshift({sid, topic: item.topic, state: item.state, message: item.message, output: item.output || '',
                    seconds: Math.round(item.elapsed / 1000), at: Date.now()});
  finished.length = Math.min(finished.length, 20);
  live.delete(sid);
}
async function deliver(sid, body) {
  // The file is already on disk; keep telling the server until it hears us.
  for (let attempt = 0; attempt < 30; attempt++) {
    try { await report(sid, body); return; }
    catch (error) { if (error.status && error.status < 500) { log('report rejected', sid, error.message); return; } }
    await new Promise(resolve => setTimeout(resolve, 4000));
  }
}

async function reconcile(remote) {
  const item = live.get(remote.id);
  if (!item) {
    if (remote.desired === 'stopped') {
      // This app was restarted during a recording: save whatever reached the disk.
      const lost = {sid: remote.id, topic: remote.topic || 'Webinar', elapsed: 0, bytes: 0, ending: true, state: 'processing',
                    reason: 'The office recorder was restarted during this class.'};
      live.set(remote.id, lost);
      finalize(lost);
    } else if (live.size < config.capacity) begin(remote);
    return;
  }
  if (item.ending) return;
  if (remote.desired === 'stopped') { ended(item, 'Stopped by faculty.'); return; }
  if (item.state === 'recording' && remote.desired === 'paused') command(item, 'pause');
  else if (item.state === 'paused' && remote.desired === 'recording') command(item, 'resume');
  else if (['recording', 'paused'].includes(item.state) && remote.desired === item.state && remote.status !== item.state)
    report(item.sid, {status: item.state}).catch(() => {});
}
async function tick() {
  if (!config) return;
  try {
    if (info.status !== 'ready') {
      const hello = await api('POST', '/api/office/hello', {capacity: config.capacity});
      info.name = hello.name;
    }
    const {sessions} = await api('GET', '/api/office/captures');
    info.status = 'ready';
    info.message = sessions.length ? `Recording ${sessions.length} of ${config.capacity} classes.` : 'Ready. Waiting for faculty to start a recording.';
    const wanted = new Set(sessions.map(remote => remote.id));
    for (const remote of sessions) await reconcile(remote);
    for (const item of live.values())  // the server no longer lists it (for example, marked failed there)
      if (!wanted.has(item.sid) && !item.ending) ended(item, 'The application closed this recording.');
  } catch (error) {
    info.status = error.status === 401 ? 'unpaired' : 'offline';
    info.message = error.status === 401 ? 'The pairing code is no longer valid. Pair again from the admin dashboard.'
      : 'Cannot reach the DocTutorials application. Recordings in progress keep saving on this computer.';
  }
}

// ---- messages from a recording window -------------------------------------------------
const owner = event => live.get(byWindow.get(event.sender.id));

ipcMain.handle('capture:config', async event => {
  const item = owner(event);
  if (!item) throw new Error('This window is not attached to a recording');
  const base = {test: TEST, sdkVersion: SDK_VERSION, hideZoomControls: config.hideZoomControls, topic: item.topic};
  return TEST ? base : {...base, join: await api('GET', `/api/office/recordings/${item.sid}/join`)};
});
ipcMain.on('capture:joined', event => {
  const item = owner(event);
  if (!item || item.ending) return;
  item.message = 'Joined. Starting capture…';
  // Screen capture must start from a user gesture; this runs it as one.
  event.sender.executeJavaScript('window.dtStartCapture()', true).catch(error => failedJoin(item, 'Capture could not start: ' + error.message));
});
ipcMain.on('capture:failed', (event, message) => { const item = owner(event); if (item) failedJoin(item, String(message || 'Could not join the webinar')); });
function failedJoin(item, message) {
  if (item.ending) return;
  item.attempts += 1;
  item.reason = message.slice(0, 200);
  log('join failed', item.sid, item.attempts, item.reason);
  if (item.attempts >= JOIN_ATTEMPTS || parts(item.sid).length) { ended(item, item.reason); return; }
  item.state = 'joining';
  item.message = `Waiting for the webinar (${item.reason}). Trying again…`;
  item.timer = setTimeout(() => { if (!item.ending && item.win && !item.win.isDestroyed()) item.win.reload(); }, JOIN_RETRY_SECONDS * 1000);
}
ipcMain.handle('capture:part-begin', event => {
  const item = owner(event);
  if (!item || item.ending) throw new Error('Recording is closing');
  const number = fs.readdirSync(folder(item.sid)).filter(name => /^part-\d{4}\.webm$/.test(name)).length;
  item.stream = fs.createWriteStream(path.join(folder(item.sid), `part-${String(number).padStart(4, '0')}.webm`));
  item.since = Date.now(); item.state = 'recording'; item.pending = null; item.message = 'Recording';
  report(item.sid, {status: 'recording'}).catch(() => {});
  return true;
});
ipcMain.on('capture:chunk', (event, data) => {
  const item = owner(event);
  if (!item || !item.stream) return;
  const buffer = Buffer.from(data);
  item.bytes += buffer.length;
  item.stream.write(buffer);
});
ipcMain.handle('capture:part-end', async (event, reason) => {
  const item = owner(event);
  if (!item) return true;
  await endPart(item);
  if (item.flushed) item.flushed();
  if (!item.ending && reason === 'pause') {
    item.state = 'paused'; item.pending = null; item.message = 'On a break';
    report(item.sid, {status: 'paused'}).catch(() => {});
  }
  return true;
});
ipcMain.on('capture:ended', event => { const item = owner(event); if (item) ended(item, 'The webinar ended.'); });
ipcMain.on('capture:note', (event, text) => { const item = owner(event); if (item && !item.ending) log('note', item.sid, String(text).slice(0, 200)); });

// ---- status window ------------------------------------------------------------------
ipcMain.handle('ui:state', () => ({
  info, paired: !!config, ffmpeg: !!ffmpegPath, test: TEST,
  settings: config ? {server: config.server, outputDir: config.outputDir, capacity: config.capacity, width: config.width, height: config.height} : null,
  live: [...live.values()].map(item => ({sid: item.sid, topic: item.topic, state: item.state, message: item.message, megabytes: Math.round(item.bytes / 1048576),
    seconds: Math.round(((item.elapsed || 0) + (item.since ? Date.now() - item.since : 0)) / 1000)})),
  finished,
}));
ipcMain.handle('ui:pair', async (_event, value) => {
  if (live.size) throw new Error('Finish the current recordings before changing settings');
  const next = validate({...value, token: value.token || (config && config.token)});
  const hello = await api('POST', '/api/office/hello', {capacity: next.capacity}, next).catch(error => {
    throw new Error(error.status === 401 ? 'The pairing code was not accepted' : 'Could not reach the application at that address');
  });
  saveConfig(next);
  config = next; info.name = hello.name; info.status = 'ready'; info.message = 'Ready. Waiting for faculty to start a recording.';
  return true;
});
ipcMain.handle('ui:open', (_event, target) => shell.openPath(target && fs.existsSync(target) ? path.dirname(target) : (config ? config.outputDir : os.homedir())));

function openUi() {
  ui = new BrowserWindow({width: 760, height: 720, title: 'DocTutorials Office Recorder',
    webPreferences: {preload: path.join(__dirname, 'ui-preload.js'), contextIsolation: true, nodeIntegration: false}});
  ui.setMenuBarVisibility(false);
  ui.loadFile(path.join(__dirname, 'ui', 'index.html'));
  ui.on('closed', () => { ui = null; });
}

if (!app.requestSingleInstanceLock()) app.quit();
else {
  app.on('second-instance', () => { if (ui) ui.focus(); else openUi(); });
  app.whenReady().then(async () => {
    ffmpegPath = findTool('ffmpeg'); ffprobePath = findTool('ffprobe');
    loadConfig();
    await startPageServer();
    openUi();
    if (!ffmpegPath || !ffprobePath) { info.status = 'blocked'; info.message = 'FFmpeg is missing. Install it (brew install ffmpeg) and reopen this app.'; return; }
    tick();
    setInterval(tick, 2000);
  });
  // Recording continues with the status window closed; quit from the Dock or menu to stop the app.
  app.on('window-all-closed', () => { if (!live.size && process.platform !== 'darwin') app.quit(); });
  app.on('activate', () => { if (!ui) openUi(); });
}
