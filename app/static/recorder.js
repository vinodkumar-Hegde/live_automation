'use strict';
(() => {
  const get = id => document.getElementById(id);
  const aid = decodeURIComponent(location.pathname.split('/').pop());
  const prefix = '/api/assignments/' + encodeURIComponent(aid) + '/recording';
  let csrf = '', current = null, capture = null, busy = false, joined = false;
  // Screen chosen for an earlier recording on this page; reused so Start asks only once.
  let armed = null;
  let polledAt = 0;  // when `current.elapsed` was last read from the server (office mode)
  let polling = false;
  const buttons = ['recordStart', 'recordBreak', 'recordResume', 'recordStop'];

  async function api(path, body, binary = false) {
    const response = await fetch(path, {
      method: body === undefined ? 'GET' : 'POST', credentials: 'same-origin', cache: 'no-store',
      headers: body === undefined ? {} : {'X-CSRF-Token': csrf,
        'Content-Type': binary ? 'application/octet-stream' : 'application/json'},
      body: body === undefined ? undefined : (binary ? body : JSON.stringify(body)),
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : `Recording request failed (${response.status})`);
    return result;
  }
  function render() {
    if (!current) return;
    const active = !['idle', 'completed', 'failed'].includes(current.status);
    const hasRecorder = capture && capture.recorders;
    get('recordStart').disabled = busy || active || !joined || !current.office_online;
    get('recordBreak').disabled = busy || !hasRecorder || !['recording', 'awaiting_media'].includes(current.status);
    get('recordResume').disabled = busy || !capture || current.status !== 'paused';
    get('recordStop').disabled = busy || !capture || (!hasRecorder && current.status !== 'paused');
    // Show only the controls that apply right now.
    const mine = active && !!capture, saving = ['stopping', 'processing'].includes(current.status);
    // Office mode: the office desktop recorder captures the webinar; this page only sends commands.
    const office = active && current.mode === 'office';
    get('recordStart').hidden = active;
    get('recordBreak').hidden = office ? current.status !== 'recording' : (!mine || saving || current.status === 'paused');
    get('recordResume').hidden = office ? current.status !== 'paused' : (!mine || current.status !== 'paused');
    get('recordStop').hidden = office ? saving : (!mine || saving);
    if (office) for (const id of ['recordBreak', 'recordResume', 'recordStop']) get(id).disabled = busy;
    get('recordHint').textContent = !joined ? 'Recording becomes available once the webinar has connected.'
      : office ? 'Recorded by the office recorder from the webinar itself, so you can share from any device. Muting in Zoom does not pause it; use Break.'
      : !active && current.office_capture ? 'The office recorder joins this webinar and records the shared screen, your video and the audio. Nothing is captured on this computer.'
      : active ? 'Recording your screen, camera and microphone. Muting in Zoom does not pause it; use Break.'
      : armed ? 'Your screen is already selected, so Start begins recording straight away.'
      : 'Start asks once which screen to record: choose "Entire screen", then Share. '
        + 'This is separate from Zoom\'s screen share and keeps Zoom\'s controls out of the video.';
    tick();
    const labels = {
      idle: 'Ready to record on office Mac', awaiting_media: 'Sending screen and faculty video to office Mac…',
      recording: 'Recording on office Mac', pausing: 'Saving this segment before the break…',
      paused: 'Break — recording paused; webinar continues', stopping: 'Saving final recording segment…',
      processing: 'Office Mac is creating your customized MP4…', completed: 'Saved on office Mac: ' + current.output_name,
      failed: current.error || 'Recording interrupted. Saved office tracks are preserved.',
      starting: 'Office recorder is joining the webinar…', resuming: 'Resuming the recording…',
    };
    if (office) Object.assign(labels, {recording: 'Recording on the office recorder', pausing: 'Pausing the recording…',
      stopping: 'Stopping the recording…', processing: 'Office recorder is saving your MP4…'});
    get('recordStatus').textContent = !current.office_online && active
      ? (office ? 'Office recorder is not responding. It keeps recording locally if it is only the network.' : 'Office Mac disconnected. Media is buffering while the Mac reconnects.')
      : (!current.office_online ? 'Office Mac offline — ask your administrator to open the recorder' : labels[current.status]);
    if (active && !office && !capture && current.status !== 'processing' && current.status !== 'stopping')
      get('recordStatus').textContent += ' Recording controls are in the tab that started capture.';
  }
  function tick() {
    const value = capture;
    const office = current && current.mode === 'office' && ['recording', 'pausing', 'paused', 'resuming'].includes(current.status);
    const running = office ? current.status === 'recording' : !!(value && value.since);
    const total = office ? (current.elapsed || 0) * 1000 + (running ? Date.now() - polledAt : 0)
      : value ? value.elapsed + (value.since ? Date.now() - value.since : 0) : 0;
    const seconds = Math.floor(total / 1000);
    const clock = String(Math.floor(seconds / 60)).padStart(2, '0') + ':' + String(seconds % 60).padStart(2, '0');
    get('recordTimer').textContent = !value && !office ? '' : (running ? '\u25CF REC ' : '\u275A\u275A Break ') + clock;
  }
  async function poll() {
    if (polling) return;
    polling = true;
    try {current = await api(prefix); polledAt = Date.now(); render();}
    catch (error) {get('recordStatus').textContent = error.message;}
    finally {polling = false;}
  }
  function release(value, keepScreen = false) {
    if (!value) return;
    value.releasing = true;
    for (const recorder of Object.values(value.recorders || {}))
      if (recorder.state !== 'inactive') recorder.stop();
    value.camera.getTracks().forEach(track => track.stop());
    // After a normal Stop keep the chosen screen so the next Start does not ask again.
    // Camera and microphone are released; the browser re-grants them without a prompt.
    if (keepScreen && value.screen.getTracks().every(track => track.readyState !== 'ended')) armed = value.screen;
    else value.screen.getTracks().forEach(track => track.stop());
  }
  async function interrupted(message) {
    const value = capture;
    if (!value || value.aborted) return;
    value.aborted = true; release(value); capture = null;
    get('recordMessage').textContent = message;
    if (current && current.id) {
      try {await api('/api/recordings/' + current.id + '/abort', {});} catch { /* Preserve the original failure. */ }
    }
    busy = false; await poll();
  }
  function mimeType() {
    if (!window.MediaRecorder) throw new Error('Use a current Chrome or Edge browser for recording.');
    const types = ['video/webm;codecs=vp8,opus', 'video/webm;codecs=vp9,opus', 'video/webm', 'video/mp4'];
    const type = types.find(value => MediaRecorder.isTypeSupported(value));
    if (!type) throw new Error('This browser cannot encode the required camera and screen tracks.');
    return type;
  }
  async function upload(path, blob) {
    let failure;
    for (let attempt = 0; attempt < 3; attempt++) {
      try {return await api(path, blob, true);} catch (error) {failure = error;}
      await new Promise(resolve => setTimeout(resolve, (attempt + 1) * 500));
    }
    throw failure;
  }
  function beginSegment() {
    const value = capture;
    value.counts = {screen: 0, camera: 0};
    value.queues = {screen: Promise.resolve(), camera: Promise.resolve()};
    value.recorders = {};
    value.since = Date.now();
    const sid = current.id, segment = current.segment;
    for (const track of ['screen', 'camera']) {
      const recorder = new MediaRecorder(value[track], {
        mimeType: value.mime, videoBitsPerSecond: track === 'screen' ? 2500000 : 1200000, audioBitsPerSecond: 128000,
      });
      value.recorders[track] = recorder;
      recorder.ondataavailable = event => {
        if (!event.data.size || value.aborted) return;
        const number = value.counts[track]++;
        const path = `/api/recordings/${sid}/chunks/${segment}/${track}/${number}`;
        value.queues[track] = value.queues[track].then(() => upload(path, event.data));
        // Attach immediately, including while the recording is still running.
        value.queues[track].catch(error => interrupted(error.message));
      };
      recorder.onerror = () => interrupted('Browser capture failed. Office media is preserved; restart the recording.');
      recorder.start(1000);
    }
  }
  async function finish(action) {
    const value = capture;
    if (!value || busy) return;
    busy = true; render();
    try {
      if (value.recorders) {
        await Promise.all(Object.values(value.recorders).map(recorder => new Promise(resolve => {
          if (recorder.state === 'inactive') return resolve();
          recorder.addEventListener('stop', resolve, {once: true}); recorder.stop();
        })));
        if (value.since) {value.elapsed += Date.now() - value.since; value.since = null;}
        await Promise.all(Object.values(value.queues));
        if (value.aborted) return;
        await api(`/api/recordings/${current.id}/segments/${current.segment}/seal`, {
          screen_chunks: value.counts.screen, camera_chunks: value.counts.camera, action,
        });
        value.recorders = null;
      } else if (action === 'stop') await api(`/api/recordings/${current.id}/stop`, {});
      if (action === 'stop') {release(value, true); capture = null;}
      await poll();
    } catch (error) {await interrupted(error.message);}
    finally {busy = false; render();}
  }
  // Office mode: ask the office desktop recorder to pause, resume or stop its own capture.
  async function command(action) {
    if (busy) return;
    busy = true; render(); get('recordMessage').textContent = '';
    try {current = await api(`/api/recordings/${current.id}/${action}`, {}); polledAt = Date.now();}
    catch (error) {get('recordMessage').textContent = error.message;}
    finally {busy = false; render();}
  }
  const officeMode = () => current && current.mode === 'office' && !['idle', 'completed', 'failed'].includes(current.status);
  get('recordStart').addEventListener('click', async () => {
    if (busy || capture) return;
    busy = true; render(); get('recordMessage').textContent = '';
    if (current && current.office_capture) {
      // The office recorder joins the webinar and captures it: no screen or camera prompt here.
      try {current = await api(prefix + '/start', {layout: {}, mode: 'office'}); polledAt = Date.now();}
      catch (error) {get('recordMessage').textContent = error.message;}
      finally {busy = false; render();}
      return;
    }
    let screen, camera;
    const reused = armed;
    try {
      const mime = mimeType();
      if (!navigator.mediaDevices?.getDisplayMedia) throw new Error('Open the HTTPS app in a current Chrome or Edge browser.');
      // Must be called directly from the Start click: browsers require this permission prompt.
      // Preselect "Entire screen" and leave this tab out of the picker so the choice is obvious.
      screen = reused || await navigator.mediaDevices.getDisplayMedia({
        video: {frameRate: {ideal: 30, max: 30}, displaySurface: 'monitor'}, audio: false,
        selfBrowserSurface: 'exclude', monitorTypeSurfaces: 'include'});
      armed = null;
      camera = await navigator.mediaDevices.getUserMedia({video: {width: {ideal: 1280}, height: {ideal: 720}}, audio: true});
      if (!screen.getVideoTracks().length || !camera.getVideoTracks().length || !camera.getAudioTracks().length)
        throw new Error('Screen, camera and microphone access are all required for this recording.');
      current = await api(prefix + '/start', {layout: {}});
      capture = {screen, camera, mime, recorders: null, aborted: false, releasing: false, elapsed: 0, since: null};
      const value = capture;
      for (const stream of [screen, camera]) for (const track of stream.getTracks()) {
        track.addEventListener('ended', () => {
          // "Stop sharing" in the browser between recordings: just forget the saved screen.
          if (armed === screen) {armed = null; render(); return;}
          if (!value.releasing && !value.aborted) interrupted('Screen, camera or microphone capture ended. Office tracks are preserved; start a new recording.');
        });
      }
      beginSegment();
    } catch (error) {
      if (capture) await interrupted(error.message);
      else {
        if (camera) camera.getTracks().forEach(track => track.stop());
        if (screen && screen === reused) armed = screen;  // keep the earlier screen choice
        else if (screen) screen.getTracks().forEach(track => track.stop());
        get('recordMessage').textContent = error.name === 'NotAllowedError'
          ? 'Recording was not started. Allow screen, camera and microphone access when you are ready.' : error.message;
      }
    } finally {busy = false; render();}
  });
  get('recordBreak').addEventListener('click', () => officeMode() ? command('pause') : finish('pause'));
  get('recordStop').addEventListener('click', () => officeMode() ? command('stop') : finish('stop'));
  get('recordResume').addEventListener('click', async () => {
    if (officeMode()) return command('resume');
    if (busy || !capture) return;
    busy = true; render();
    try {
      current = await api(`/api/recordings/${current.id}/resume`, {});
      beginSegment();
    } catch (error) {await interrupted(error.message);}
    finally {busy = false; render();}
  });
  window.addEventListener('dt:meeting-connected', () => {joined = true; render();});
  window.addEventListener('pagehide', () => {
    if (capture && current?.id) {
      fetch(`/api/recordings/${current.id}/abort`, {method: 'POST', credentials: 'same-origin', keepalive: true,
        headers: {'X-CSRF-Token': csrf, 'Content-Type': 'application/json'}, body: '{}'}).catch(() => {});
      release(capture);
    }
    if (armed) {armed.getTracks().forEach(track => track.stop()); armed = null;}
  });
  async function boot() {
    try {
      const person = await api('/api/me'); csrf = person.csrf;
      if (person.role !== 'faculty') return;
      get('officeRecording').hidden = false;
      buttons.forEach(id => {get(id).disabled = true;});
      await poll(); setInterval(poll, 2000); setInterval(tick, 1000);
    } catch (error) {get('recordMessage').textContent = error.message;}
  }
  boot();
})();
