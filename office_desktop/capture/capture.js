'use strict';
/* Runs inside one recording window of the office desktop recorder.
 * Joins the Zoom webinar as a view-only attendee, then records this window
 * (video plus webinar audio) and streams the media to the app, which writes it to disk.
 */
(() => {
  const note = document.getElementById('note');
  let stream = null, recorder = null, queue = Promise.resolve(), settings = {};

  const say = text => { note.textContent = text; };
  const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
  function describe(reason) {
    // Never stringify SDK objects: they can contain tokens.
    if (reason instanceof Error) return reason.message;
    if (!reason || typeof reason !== 'object') return String(reason || 'Unknown Zoom error');
    return ['errorCode', 'errorMessage', 'reason', 'type'].filter(key => ['string', 'number'].includes(typeof reason[key]))
      .map(key => `${key}: ${String(reason[key]).slice(0, 120)}`).join(' | ') || 'Zoom returned no details';
  }
  function loadScript(url) {
    return new Promise((resolve, reject) => {
      const script = document.createElement('script');
      script.src = url; script.onload = resolve;
      script.onerror = () => reject(new Error('Zoom SDK could not be downloaded. Check this computer\'s internet connection.'));
      document.head.appendChild(script);
    });
  }
  function zoomCall(method, options) {
    return new Promise((resolve, reject) => {
      try {
        const result = method({...options, success: resolve, error: reject});
        if (result && typeof result.then === 'function') result.then(resolve, reject);
      } catch (error) { reject(error); }
    });
  }

  async function joinZoom(join, version) {
    const vendor = `https://source.zoom.us/${version}/lib/vendor/`;
    for (const file of ['react.min.js', 'react-dom.min.js', 'redux.min.js', 'redux-thunk.min.js', 'react-redux.min.js', 'lodash.min.js'])
      await loadScript(vendor + file);
    await loadScript(`https://source.zoom.us/zoom-meeting-${version}.min.js`);
    const ZoomMtg = window.ZoomMtg;
    if (!ZoomMtg) throw new Error('Zoom Meeting SDK did not initialize');
    ZoomMtg.setZoomJSLib(`https://source.zoom.us/${version}/lib`, '/av');
    ZoomMtg.preLoadWasm();
    ZoomMtg.prepareWebSDK();
    await zoomCall(ZoomMtg.init.bind(ZoomMtg), {leaveUrl: location.origin + '/left.html', disablePreview: true, patchJsMedia: true});
    await zoomCall(ZoomMtg.join.bind(ZoomMtg), {signature: join.signature, meetingNumber: join.meetingNumber,
      passWord: join.passWord || '', userName: join.userName, userEmail: join.userEmail});
    try {
      ZoomMtg.inMeetingServiceListener('onMeetingStatus', data => { if (data && data.meetingStatus === 3) window.dt.ended(); });
    } catch { /* Older SDKs: leaving navigates to /left.html, which the app also treats as ended. */ }
    note.hidden = true;
    tidyZoom();
  }

  // Best effort, repeated because Zoom draws its dialogs late: accept "Join Audio by Computer"
  // so the webinar audio is recorded, and hide Zoom's toolbar so it is not in the video.
  function tidyZoom() {
    let audioJoined = false;
    const timer = setInterval(() => {
      for (const button of document.querySelectorAll('button')) {
        const label = (button.textContent || '').trim();
        if (/join-audio-by-voip__join-btn/.test(button.className) || /^Join Audio by Computer$/i.test(label) || /^Computer Audio$/i.test(label)) {
          button.click(); audioJoined = true; window.dt.note('joined computer audio');
        }
      }
    }, 1500);
    setTimeout(() => {
      clearInterval(timer);
      if (!audioJoined) window.dt.note('no audio prompt seen (attendee audio usually starts by itself)');
      if (settings.hideZoomControls) {
        const style = document.createElement('style');
        style.textContent = '#wc-footer,.meeting-info-container,.notification-manager,.zm-btn--chat{display:none!important}';
        document.head.appendChild(style);
      }
    }, 20000);
  }

  // Stand-in for a webinar when the app is run in test mode: moving picture and a tone.
  function testScene() {
    const canvas = document.getElementById('scene');
    canvas.hidden = false; note.hidden = true;
    canvas.width = innerWidth; canvas.height = innerHeight;
    const context = canvas.getContext('2d'), started = performance.now();
    (function draw() {
      const t = (performance.now() - started) / 1000;
      context.fillStyle = '#12305c'; context.fillRect(0, 0, canvas.width, canvas.height);
      context.fillStyle = '#ffd24a';
      context.fillRect(40 + ((t * 160) % Math.max(1, canvas.width - 240)), canvas.height / 2 - 60, 160, 120);
      context.fillStyle = '#fff'; context.font = '48px Arial';
      context.fillText('Test scene  ' + t.toFixed(1) + 's', 40, 80);
      requestAnimationFrame(draw);
    })();
    const audio = new AudioContext(), tone = audio.createOscillator(), gain = audio.createGain();
    tone.frequency.value = 440; gain.gain.value = 0.2;
    tone.connect(gain).connect(audio.destination); tone.start();
  }

  function mime() {
    return ['video/webm;codecs=vp8,opus', 'video/webm;codecs=vp9,opus', 'video/webm'].find(type => MediaRecorder.isTypeSupported(type));
  }
  // Each stretch between Start/Resume and Break/Stop is its own file part, so breaks are never in the video.
  async function beginPart() {
    await window.dt.partBegin();
    const options = {mimeType: mime(), videoBitsPerSecond: 3500000, audioBitsPerSecond: 128000};
    const current = recorder = new MediaRecorder(stream, options);
    current.ondataavailable = event => {
      if (!event.data.size) return;
      queue = queue.then(() => event.data.arrayBuffer()).then(buffer => window.dt.chunk(buffer)).catch(() => {});
    };
    current.start(1000);
  }
  function endPart(reason) {
    const current = recorder;
    recorder = null;
    if (!current || current.state === 'inactive') return window.dt.partEnd(reason);
    return new Promise(resolve => {
      current.addEventListener('stop', () => queue.then(() => window.dt.partEnd(reason)).then(resolve), {once: true});
      current.stop();
    });
  }

  // Called by the app (as a user gesture) once the webinar is on screen.
  window.dtStartCapture = async () => {
    // Pin the size: otherwise the capture is scaled up to the screen's resolution, which varies by computer.
    const width = 2 * Math.round(Math.min(1920, innerWidth * (devicePixelRatio || 1)) / 2);
    const height = 2 * Math.round(width * innerHeight / innerWidth / 2);
    stream = await navigator.mediaDevices.getDisplayMedia({audio: true, video: {
      frameRate: {ideal: 30, max: 30}, width: {ideal: width, max: width}, height: {ideal: height, max: height}}});
    if (!stream.getVideoTracks().length) throw new Error('This window could not be captured');
    if (!stream.getAudioTracks().length) window.dt.note('no audio track offered for this window');
    stream.getVideoTracks()[0].addEventListener('ended', () => { endPart('stop').then(() => window.dt.ended()); });
    await beginPart();
  };
  window.dt.onCommand(action => {
    if (action === 'pause') endPart('pause');
    else if (action === 'resume') beginPart().catch(error => window.dt.failed(error.message));
    else if (action === 'stop') endPart('stop').then(() => { if (stream) stream.getTracks().forEach(track => track.stop()); });
  });

  (async () => {
    try {
      settings = await window.dt.config();
      document.title = 'Recording: ' + settings.topic;
      if (settings.test) testScene(); else await joinZoom(settings.join, settings.sdkVersion);
      await wait(settings.test ? 300 : 4000);  // let Zoom paint the webinar before capture begins
      window.dt.joined();
    } catch (error) {
      const message = describe(error);
      say('Could not join the webinar: ' + message);
      window.dt.failed(message);
    }
  })();
})();
