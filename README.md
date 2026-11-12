# DocTutorials Faculty Live

Faculty sign in to DocTutorials and enter their assigned Zoom webinar inside the web app, without typing a Zoom ID or password. An office recorder joins the same webinar as a viewer and records it, controlled by Start / Break / Resume / Stop on the faculty page.

This is a pilot. See [Status and limitations](#status-and-limitations) before relying on it.

## Components

| Path | What it is |
|---|---|
| `app/main.py` | FastAPI backend: sign-in, faculty and assignment admin, Zoom host join credentials |
| `app/recording.py` | Recorder pairing, recording sessions and state machine, office recorder API |
| `app/static/` | Admin and faculty pages (plain HTML/JS) and the embedded Zoom Meeting SDK client |
| `office_desktop/` | Office desktop recorder (Electron): joins each webinar as an attendee and records it; several classes at once |
| `office_recorder/` | Older Python recorder, kept as a fallback: the faculty browser captures screen and camera and relays them |
| `scripts/` | Setup, run and packaging scripts |
| `tests/` | Backend (pytest) and frontend (node:test) tests |

```
Faculty browser ── HTTPS ──> FastAPI + SQLite <── polling ── Office desktop recorder
   embedded Zoom webinar        control plane                 one window per class:
   Start/Break/Resume/Stop      (desired vs confirmed state)  joins as attendee, records
                                                              the window, exports MP4
```

## Requirements

- Python 3.11+ for the backend.
- Node.js 20+ and FFmpeg on the office recorder computer.
- A Zoom account with a Meeting SDK app and a Server-to-Server OAuth app. See [ZOOM_SETUP.md](ZOOM_SETUP.md).

## Run the backend

```bash
./scripts/setup.sh        # creates .env and .venv, then asks for the admin email and password
# add the Zoom credentials to .env (see .env.example and ZOOM_SETUP.md)
./scripts/run.sh          # http://127.0.0.1:8792, this computer only
./scripts/run.sh --lan    # also reachable from other computers on the same LAN
```

Faculty browsers need HTTPS (or `127.0.0.1`), because browsers allow camera, microphone and the Zoom client only on secure pages. Put the app behind an HTTPS reverse proxy or tunnel for other devices.

Docker alternative: `./scripts/init.sh`, add the Zoom credentials to `.env`, then `docker compose up -d --build` (port 8791).

## Run the office desktop recorder

```bash
cd office_desktop && npm install && npm start
```

On macOS you can instead open `Start Office Desktop Recorder.command`. In the admin dashboard click **Generate office pairing code**, then enter the application address and the code in the app. Recordings are saved under `Movies/DocTutorials/<recording-id>/`.

`python scripts/package_office_recorder.py` builds ZIP bundles for the office computer in `dist/`. Setup details, the fallback recorder and LAN notes are in [RECORDING_SETUP.md](RECORDING_SETUP.md).

## Tests

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q tests
node --test tests/test_frontend.cjs tests/test_recorder_frontend.cjs
```

To check an office computer without a live class, start the desktop recorder with `DT_CAPTURE_TEST=1`: it records a generated test scene instead of joining Zoom.

## Configuration

All settings are environment variables read from `.env` (never committed). `.env.example` lists them: the session secret, the Zoom Meeting SDK and Server-to-Server OAuth credentials, and optional values.

## Security notes

- Zoom secrets stay on the server. The browser receives only a short-lived join signature and, for the host, a short-lived host token.
- Passwords are stored as Argon2 hashes; sessions are signed HttpOnly cookies; state-changing requests need a CSRF header.
- Office recorders authenticate with a pairing code of which the server stores only a hash. The desktop recorder keeps it in its main process, not in the page that loads Zoom.
- Plain `http://` between the recorder and the backend is accepted only for LAN addresses. Use HTTPS anywhere else.

## Status and limitations

- The desktop recorder's control loop and capture pipeline are verified with a generated test scene (two simultaneous recordings, break excluded, verified MP4). Joining a real Zoom webinar from the desktop recorder still needs to be confirmed in your environment.
- The recording is what a Zoom attendee sees: Zoom's layout and quality, not a custom composition.
- Recording windows must stay open and unminimised, and the webinar must allow attendees without registration.
- Each faculty member who teaches at the same time as another needs their own Zoom host account.
- Coordination uses polling and SQLite, which suits a pilot, not production scale. Production also needs a stable HTTPS domain, backups and a retention policy for recordings.
- The faculty page contains a small diagnostic (`watchZoomJoinResult` in `app/static/meeting.js`) that surfaces Zoom's real join error code; remove it once joins are stable.
