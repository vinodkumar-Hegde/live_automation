"""Office Mac client: durable local media writes, acknowledgements and MP4 export."""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import threading
import time
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .compositor import Layout, RecordingError, compose, concatenate, probe, require_ffmpeg


def lan_host(hostname):
    """True for this machine, a private/link-local LAN address or an mDNS (.local) name."""
    if not hostname:
        return False
    if hostname == 'localhost' or hostname.endswith('.local'):
        return True
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return False
    return address.is_loopback or address.is_private or address.is_link_local


def atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.pending')
    with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'w') as stream:
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


class OfficeAgent:
    def __init__(self, server, token, output_dir, transport=None, layout=None):
        server = server.rstrip('/')
        url = urlsplit(server)
        if url.username or url.password or url.query or url.fragment or url.path not in ('', '/'):
            raise ValueError('Enter the application origin, without a path or credentials')
        if url.scheme != 'https' and not (url.scheme == 'http' and lan_host(url.hostname)):
            raise ValueError('Use the HTTPS application URL, or http:// with the server\'s LAN address (for example http://192.168.1.20:8792)')
        if not url.hostname or not token:
            raise ValueError('Application URL and office pairing code are required')
        self.server, self.token = server, token
        self.output_dir = Path(output_dir).expanduser().resolve()
        self.layout = Layout.from_dict(layout) if layout else None
        self.output_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=self.output_dir):
            pass  # Confirm that this Mac can actually write to the selected folder.
        self.client = httpx.Client(base_url=server, headers={'Authorization': 'Bearer ' + token},
                                   timeout=30, transport=transport)
        self.transport = transport
        self.stop_event = threading.Event()
        self.pool = ThreadPoolExecutor(max_workers=1)
        self.future = None
        self.encoding_sid = None
        self.info = {'status': 'connecting', 'message': 'Connecting to faculty application…', 'output': ''}
        self._states = {}
        self.current_sid = None

    def request(self, method, path, **kwargs):
        response = self.client.request(method, path, **kwargs)
        response.raise_for_status()
        return response

    def folder(self, sid):
        if not re.fullmatch(r'[a-f0-9]{32}', sid):
            raise RecordingError('Invalid recording identifier received from server')
        return self.output_dir / sid

    def local_state(self, sid):
        if sid not in self._states:
            file = self.folder(sid) / 'state.json'
            try:
                self._states[sid] = json.loads(file.read_text()) if file.exists() else {'tracks': {}}
            except ValueError as exc:
                raise RecordingError('Office recording state is damaged. Original media is preserved; choose a new session.') from exc
        return self._states[sid]

    def ingest(self, sid, chunk):
        number, track, sequence = chunk['segment'], chunk['track'], chunk['seq']
        if type(number) is not int or number < 0 or track not in ('screen', 'camera') or type(sequence) is not int or sequence < 0:
            raise RecordingError('Invalid media manifest')
        state = self.local_state(sid)
        key = f'{number}-{track}'
        cursor = state['tracks'].setdefault(key, {'next': 0, 'bytes': 0})
        route = f'/api/office/recordings/{sid}/chunks/{number}/{track}/{sequence}'
        if sequence > cursor['next']:
            raise RecordingError('A media chunk is missing; original tracks have been preserved')
        if sequence == cursor['next']:
            data = self.request('GET', route).content
            if len(data) != chunk['size'] or hashlib.sha256(data).hexdigest() != chunk['digest']:
                raise RecordingError('Media integrity check failed; refusing to save damaged data')
            folder = self.folder(sid)
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / (key + '.webm')
            with target.open('r+b' if target.exists() else 'w+b') as stream:
                if stream.seek(0, 2) < cursor['bytes']:
                    raise RecordingError('Previously saved office media is missing; restore the recording folder')
                # Recover a crash after writing media but before saving the cursor.
                stream.seek(cursor['bytes'])
                stream.truncate()
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            cursor['next'] += 1
            cursor['bytes'] += len(data)
            atomic_json(folder / 'state.json', state)
        # Acknowledge only after both media and cursor are durable on the office disk.
        self.request('POST', route + '/ack')

    def saved(self, sid, segments):
        state = self.local_state(sid)
        return bool(segments) and all(
            segment['sealed'] and all(state['tracks'].get(f"{segment['segment']}-{track}", {}).get('next', 0)
                                     == segment[track + '_chunks'] for track in ('screen', 'camera'))
            for segment in segments)

    def export(self, session):
        sid = session['id']
        folder = self.folder(sid)
        layout = self.layout or Layout.from_dict(session['layout'])
        parts = []
        for segment in session['segments']:
            number = segment['segment']
            part = folder / f'part-{number:04d}.mp4'
            if not part.exists():
                compose(folder / f'{number}-screen.webm', folder / f'{number}-camera.webm', part, layout)
            parts.append(part)
        output = folder / f'DocTutorials-{sid}.mp4'
        if not output.exists():
            concatenate(parts, output)
        metadata = probe(output)
        codecs = {s.get('codec_type') for s in metadata.get('streams', [])}
        duration = float(metadata.get('format', {}).get('duration') or 0)
        if not {'video', 'audio'} <= codecs or duration <= 0:
            raise RecordingError('Final MP4 verification failed; original recordings are preserved')
        self.info['output'] = str(output)
        return {'status': 'completed', 'output_name': output.name, 'output_bytes': output.stat().st_size,
                'output_duration': duration}

    def step(self):
        work = self.request('GET', '/api/office/work').json()['session']
        if self.future and self.future.done():
            try:
                result = self.future.result()
            except Exception:
                result = {'status': 'failed', 'error': 'MP4 export failed. Original tracks are preserved on the office Mac; check the encoder log.'}
            self.request('POST', f'/api/office/recordings/{self.encoding_sid}/report', json=result)
            self.info.update(status=result['status'], message='Recording saved on office Mac' if result['status'] == 'completed' else result['error'])
            self.future, self.encoding_sid = None, None
            return
        if work is None:
            if not self.future:
                self.info.update(status='ready', message='Ready. Waiting for faculty recording controls.')
            return
        sid = work['id']
        self.current_sid = sid
        for chunk in work['chunks']:
            self.ingest(sid, chunk)
        if work['status'] == 'failed':
            self.info.update(status='failed', message='Interrupted recording media recovered to office disk. No complete MP4 has been claimed.')
            return
        if self.future:
            return
        if work['desired'] == 'recording':
            state = self.local_state(sid)
            receiving = all(state['tracks'].get(f"{work['segment']}-{track}", {}).get('next', 0) > 0 for track in ('screen', 'camera'))
            if receiving and work['status'] != 'recording':
                self.request('POST', f'/api/office/recordings/{sid}/report', json={'status': 'recording'})
            self.info.update(status='recording' if receiving else 'awaiting_media',
                             message='Saving screen, faculty camera and microphone on office disk' if receiving else 'Waiting for faculty media…')
        elif self.saved(sid, work['segments']):
            if work['desired'] == 'paused':
                if work['status'] != 'paused':
                    self.request('POST', f'/api/office/recordings/{sid}/report', json={'status': 'paused'})
                self.info.update(status='paused', message='On break. Recording paused; webinar is still live.')
            elif work['desired'] == 'stopped':
                self.request('POST', f'/api/office/recordings/{sid}/report', json={'status': 'processing'})
                self.info.update(status='processing', message='Creating customized MP4. Breaks are excluded.')
                self.encoding_sid = sid
                self.future = self.pool.submit(self.export, work)

    def heartbeat(self):
        with httpx.Client(base_url=self.server, headers={'Authorization': 'Bearer ' + self.token},
                          timeout=10, transport=self.transport) as client:
            while not self.stop_event.is_set():
                try:
                    client.post('/api/office/heartbeat').raise_for_status()
                except httpx.HTTPError:
                    self.info.update(status='offline', message='Cannot reach the faculty application. Local recording files are preserved.')
                self.stop_event.wait(5)

    def run(self):
        require_ffmpeg()
        # No webcam, microphone or screen access is requested on the office Mac.
        threading.Thread(target=self.heartbeat, daemon=True).start()
        while not self.stop_event.is_set():
            try:
                self.step()
            except (httpx.HTTPError, ValueError):
                self.info.update(status='offline', message='Connection interrupted. Retrying; saved office files are preserved.')
            except (RecordingError, OSError) as exc:
                message = str(exc) if isinstance(exc, RecordingError) else 'Office disk is full or unavailable. Received media is preserved.'
                self.info.update(status='failed', message=message)
                if self.current_sid:
                    try:
                        self.request('POST', f'/api/office/recordings/{self.current_sid}/report',
                                     json={'status': 'failed', 'error': message[:300]})
                    except httpx.HTTPError:
                        pass
            self.stop_event.wait(1)

    def close(self):
        self.stop_event.set()
        self.pool.shutdown(wait=True)
        self.client.close()
