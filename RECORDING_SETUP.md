# Office Mac recorder — no OBS

Faculty sign in only to DocTutorials. Recording uses a separate browser screen/camera capture; it does not require faculty Zoom login, Zoom RTMS credits, or an office Zoom login.

## Set up the office Mac once

1. On the office Mac, sign in to the DocTutorials admin dashboard and click **Download office Mac recorder**. Alternatively, copy `dist/DocTutorialsOfficeRecorder-macOS.zip` to that Mac. Extract the ZIP and keep the extracted folder together.
2. Install Python 3.11+ and FFmpeg if needed. With Homebrew: `brew install python ffmpeg`.
3. Open **Start Office Recorder.command** in the extracted folder. It creates its own Python environment, installs the three listed dependencies, and opens the office setup/status window at `http://127.0.0.1:8804`.
4. In the main DocTutorials **admin dashboard**, click **Generate office pairing code** under Office Mac recording.
5. In the office recorder window, enter the application URL and pairing code, choose a local save folder (default `~/Movies/DocTutorials`), then click **Connect office recorder**.
6. Keep this application and the office Mac running and connected to the internet. The recorder reconnects using its saved settings on the next launch. It uses outbound HTTPS; no office router port forwarding is needed.

Only copy the office bundle to the office Mac. It contains no server `.env`, faculty database, Zoom secrets, recordings, or pairing codes. The office app stores its pairing settings in `~/Library/Application Support/DocTutorialsRecorder/config.json` with owner-only file permissions.

If the temporary public app address changes, open the office setup window and pair using the new application URL and the existing or newly generated office pairing code.

## Office desktop recorder (recommended): records the webinar itself

Use this when faculty may share from any device (for example an iPad for the screen and a laptop for video), or when several classes run at once. Nothing is captured on the faculty computer and faculty get no screen-share prompt.

How it works: when faculty press **Start recording**, the desktop app on the office computer opens a window, joins that Zoom webinar as a view-only attendee named "DocTutorials Recorder", and records the window (shared screen, faculty video and webinar audio) to the office disk. **Break**, **Resume** and **Stop & save** on the faculty page control it. Each class gets its own window, so several classes are recorded at once (default 3, set under **Classes at once**).

Set up the office computer once:

1. Copy `dist/DocTutorialsOfficeDesktopRecorder.zip` to it and extract it. Install Node.js and FFmpeg (`brew install node ffmpeg`).
2. Open **Start Office Desktop Recorder.command** (right-click → Open the first time). The first run downloads its runtime (about 100 MB).
3. In the DocTutorials admin dashboard click **Generate office pairing code**. In the app, enter the application address (the LAN address printed by `./scripts/run.sh --lan`, or the HTTPS address), paste the code and click **Connect**.
4. In the admin dashboard, **Remove** any older office recorder you no longer want used. When a desktop recorder is online and free, the faculty page uses it automatically; otherwise it falls back to capturing on the faculty computer.

Recordings are saved as `Movies/DocTutorials/<recording-id>/DocTutorials-<recording-id>.mp4`; the source parts stay beside the MP4. Each break ends one part, so breaks are never in the video.

Limits of this version:

- The video is what a Zoom attendee sees (Zoom's own layout and quality), not the custom picture-in-picture layout of the faculty-capture mode.
- Keep the recording windows open and not minimised. Closing a window ends that recording and saves what was captured.
- The webinar must allow attendees to join without registration; the recorder joins with the address in `ZOOM_RECORDER_EMAIL` (default `office-recorder@doctutorials.com`).
- If the webinar has not started yet, the recorder keeps retrying for ten minutes.
- Run the app with `DT_CAPTURE_TEST=1` to record a generated test scene instead of Zoom, to check a computer without a live class.

## Prototype: record on another laptop over the same LAN

Use this when the faculty application runs on one computer and recordings must be saved on a different laptop on the same Wi-Fi/LAN.

1. On the application computer, start the backend with `./scripts/run.sh --lan`. It prints the pairing address, for example `http://192.168.1.20:8792`. Allow incoming connections if macOS Firewall asks.
2. Copy `dist/DocTutorialsOfficeRecorder-macOS.zip` to the other laptop (AirDrop/USB, or the admin dashboard download), extract it and open **Start Office Recorder.command** (needs `brew install python ffmpeg`).
3. In the admin dashboard, click **Generate office pairing code**. On the other laptop, enter the LAN address from step 1 as the application URL, paste the code and click **Connect office recorder**.
4. In the admin dashboard, once the new recorder shows **online**, click **Remove** next to any recorder on the application computer itself, and quit the office recorder there. With only the other laptop paired, Start recording always saves there (`~/Movies/DocTutorials` on that laptop).

Plain `http://` is accepted only for private LAN addresses, `.local` names and this computer; any other address must be HTTPS. On a LAN the pairing code and media are unencrypted, so use this on a trusted network only. If the application computer's IP address changes, pair again with the new address. Faculty still open the application through its HTTPS address or `http://127.0.0.1:8792` on the application computer, because browsers allow camera/screen capture only on secure pages.

## Faculty controls

After the Zoom webinar connects, an **Office recording** bar appears over the webinar:

- **Start recording:** select the slides/presentation window and allow camera and microphone access. Select the presentation window, rather than the Zoom window, to keep Zoom controls out of the video.
- **Break:** save the current recording segment and pause recording only. The Zoom webinar, camera and microphone continue normally in Zoom.
- **Resume:** start a new recording segment using the already-approved capture streams. The break interval is excluded from the finished video.
- **Stop & save MP4:** finish sending media to the office Mac, then automatically create the customized MP4 there.

The recorder captures its own microphone stream independently of Zoom's mute control. Use **Break** to pause recording. It records the selected screen, faculty camera and faculty microphone; audio from other Zoom participants or the shared screen is not included in this version.

Browsers require a screen-selection prompt on each new recording session. Break/Resume retain capture permissions while that page remains open. Keep the faculty webinar tab open until capture has stopped. Closing it or revoking capture permission marks the recording interrupted and preserves media already received on the office Mac.

The office Mac must be online and free before Start is enabled. One office recorder handles one active class at a time. Multiple office Macs can be paired for concurrent classes.

## Customized format

The default matches the reference arrangement:

- 1920 × 1080, 30 fps.
- Shared screen fitted to the main canvas without distortion.
- Faculty camera at the lower right: 576 × 324, 24-pixel margin.
- H.264 video and AAC microphone audio in an MP4.

Expand **Customize recording layout** in the office setup window to change output dimensions, camera size, corner and margin before recording. The office Mac controls the final layout. The camera's actual background is retained; background removal and custom logo assets are not included.

Screen/camera media is saved continuously on the office disk. The customized MP4 is encoded **after Stop**, rather than during the lecture. The faculty app displays **processing** until the Mac verifies that the output contains video, audio and a positive duration. Long classes may take time to export.

Each recording has a unique folder containing its separate source tracks, segment MP4s, encoder logs and final `DocTutorials-<recording-id>.mp4`. Source tracks are retained for recovery; manage office disk space according to your storage policy.

## Reliability and status

Media is temporarily relayed through the application server over authenticated HTTPS. Each chunk is ordered and checksummed. The server deletes its relay copy only after the office Mac has written the media and its recovery cursor to disk and acknowledged receipt. After reconnecting, the office app also recovers pending media from interrupted sessions, clearing the relay buffer without falsely marking an incomplete recording successful. The pending server buffer is capped at 512 MiB; an offline or slow office recorder cannot silently fill the server disk indefinitely.

**Recording** and **paused** statuses require acknowledgement from the office app. A network failure, unwritable office disk, missing media, permission denial or encoding error does not produce a fake successful recording. Interrupted office tracks and encoder logs remain available for recovery; a partial recording is not reported as a completed MP4.

An integration test exercises the actual API, office receiver and FFmpeg using real WebM tracks, a break, resume and final MP4 verification. A live faculty camera/screen test on the actual office Mac is still required, including confirming that the chosen browser/camera can be accessed alongside Zoom.

## Developer checks

```bash
.venv/bin/python -m pytest -q tests
node --test tests/test_frontend.cjs tests/test_recorder_frontend.cjs
.venv/bin/python scripts/package_office_recorder.py
```
