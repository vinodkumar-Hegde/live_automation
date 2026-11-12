"""Local-only setup/status window for the office Mac recorder."""
import argparse
import json
import os
import secrets
import threading
import webbrowser
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
import uvicorn

from .agent import OfficeAgent, atomic_json
from .compositor import require_ffmpeg, RecordingError


class Pair(BaseModel):
    server: str
    token: str
    output_dir: str
    layout: dict = Field(default_factory=dict)


def create_local_app(config_path: Path, port=8804):
    app = FastAPI(docs_url=None, redoc_url=None)
    secret = secrets.token_urlsafe(32)
    current = {'agent': None, 'thread': None, 'error': ''}

    def start(config):
        require_ffmpeg()
        old = current['agent']
        if old:
            if old.future or old.info['status'] not in ('ready', 'connecting', 'offline', 'completed', 'failed'):
                raise HTTPException(409, 'Finish the current recording before changing office settings')
        agent = OfficeAgent(config['server'], config['token'], config['output_dir'], layout=config.get('layout'))
        try:
            agent.request('POST', '/api/office/heartbeat')
        except Exception:
            agent.close()
            raise
        if old:
            old.close()
        current['agent'] = agent
        thread = threading.Thread(target=agent.run, daemon=True)
        current['thread'] = thread
        thread.start()

    if config_path.exists():
        try:
            start(json.loads(config_path.read_text()))
        except Exception:
            current['error'] = 'Could not reconnect. Check the app URL, pairing code and FFmpeg installation.'

    @app.get('/', response_class=HTMLResponse)
    def page():
        html = (Path(__file__).parent / 'office.html').read_text()
        return HTMLResponse(html.replace('__LOCAL_CSRF__', secret), headers={'Cache-Control': 'no-store'})

    @app.get('/status')
    def status():
        agent = current['agent']
        return agent.info.copy() if agent else {'status': 'unpaired', 'message': current['error'] or 'Pair this Mac with your faculty application.', 'output': ''}

    @app.post('/pair')
    def pair(data: Pair, req: Request):
        if req.headers.get('X-Local-CSRF') != secret:
            raise HTTPException(403, 'Open the office recorder window before pairing')
        origin = req.headers.get('Origin')
        if origin and origin not in (f'http://127.0.0.1:{port}', f'http://localhost:{port}'):
            raise HTTPException(403, 'Pairing must be performed from this Mac')
        config = data.model_dump()
        if not config['output_dir'].strip():
            config['output_dir'] = str(Path.home() / 'Movies' / 'DocTutorials')
        try:
            start(config)
        except HTTPException:
            raise
        except (RecordingError, ValueError) as exc:
            raise HTTPException(400, str(exc))
        except OSError:
            raise HTTPException(400, 'Cannot write to the recording folder. Select a writable folder on this Mac.')
        except Exception:
            raise HTTPException(400, 'Unable to pair. Check your app URL, pairing code and FFmpeg installation.')
        atomic_json(config_path, config)
        os.chmod(config_path, 0o600)
        return {'ok': True}

    return app


def main():
    parser = argparse.ArgumentParser(description='DocTutorials office Mac recorder')
    parser.add_argument('--port', type=int, default=8804)
    parser.add_argument('--config', type=Path, default=Path.home() / 'Library' / 'Application Support' / 'DocTutorialsRecorder' / 'config.json')
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args()
    app = create_local_app(args.config, args.port)
    if not args.no_browser:
        threading.Timer(1, lambda: webbrowser.open(f'http://127.0.0.1:{args.port}')).start()
    uvicorn.run(app, host='127.0.0.1', port=args.port, access_log=False)


if __name__ == '__main__':
    main()
