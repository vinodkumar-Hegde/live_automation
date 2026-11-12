"""Authenticated faculty controls and a durable, outbound-only office recorder relay."""
from __future__ import annotations

import hashlib
import io
import json
import re
import secrets
import time
import uuid
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

from fastapi import Depends, HTTPException, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from office_recorder.compositor import Layout

ONLINE_SECONDS = 25
CHUNK_LIMIT = 8 * 1024 * 1024
SPOOL_LIMIT = 512 * 1024 * 1024
TERMINAL = ('completed', 'failed')


def init_database(database):
    with database() as con:
        con.executescript('''
        CREATE TABLE IF NOT EXISTS office_recorders(
          id TEXT PRIMARY KEY, name TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE,
          last_seen REAL NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS recording_sessions(
          id TEXT PRIMARY KEY, assignment_id TEXT NOT NULL, faculty_id TEXT NOT NULL,
          recorder_id TEXT NOT NULL, status TEXT NOT NULL, desired TEXT NOT NULL,
          segment INTEGER NOT NULL DEFAULT 0, layout TEXT NOT NULL,
          created_at REAL NOT NULL, updated_at REAL NOT NULL,
          error TEXT NOT NULL DEFAULT '', output_name TEXT NOT NULL DEFAULT '',
          output_bytes INTEGER NOT NULL DEFAULT 0, output_duration REAL NOT NULL DEFAULT 0);
        CREATE UNIQUE INDEX IF NOT EXISTS one_active_assignment_recording ON recording_sessions(assignment_id)
          WHERE status NOT IN ('completed','failed');
        CREATE TABLE IF NOT EXISTS recording_segments(
          session_id TEXT NOT NULL, segment INTEGER NOT NULL, sealed INTEGER NOT NULL DEFAULT 0,
          screen_chunks INTEGER, camera_chunks INTEGER, PRIMARY KEY(session_id,segment));
        CREATE TABLE IF NOT EXISTS recording_chunks(
          session_id TEXT NOT NULL, segment INTEGER NOT NULL, track TEXT NOT NULL,
          seq INTEGER NOT NULL, size INTEGER NOT NULL, digest TEXT NOT NULL,
          acked INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(session_id,segment,track,seq));
        ''')
        # Office desktop recorders capture the webinar themselves ("office" mode) and can
        # record several classes at once, so capacity is enforced per recorder in start().
        columns = [('office_recorders', 'captures', 'INTEGER NOT NULL DEFAULT 0'),
                   ('office_recorders', 'capacity', 'INTEGER NOT NULL DEFAULT 1'),
                   ('recording_sessions', 'mode', "TEXT NOT NULL DEFAULT 'faculty'"),
                   ('recording_sessions', 'rec_elapsed', 'REAL NOT NULL DEFAULT 0'),
                   ('recording_sessions', 'rec_since', 'REAL NOT NULL DEFAULT 0')]
        for table, column, definition in columns:
            if column not in {row[1] for row in con.execute(f'PRAGMA table_info({table})')}:
                con.execute(f'ALTER TABLE {table} ADD COLUMN {column} {definition}')
        con.execute('DROP INDEX IF EXISTS one_active_office_session')


class RecorderCreate(BaseModel):
    name: str = Field(min_length=2, max_length=80)


class Start(BaseModel):
    layout: dict = Field(default_factory=dict)
    # "faculty": this browser captures and relays media. "office": the office desktop
    # recorder joins the webinar and captures it; the faculty page only sends commands.
    mode: str = Field(default='faculty', pattern=r'^(faculty|office)$')


class Hello(BaseModel):
    capacity: int = Field(default=1, ge=1, le=8)


class Seal(BaseModel):
    screen_chunks: int = Field(ge=1, le=100000)
    camera_chunks: int = Field(ge=1, le=100000)
    action: str = Field(pattern=r'^(pause|stop)$')


class Report(BaseModel):
    status: str = Field(pattern=r'^(recording|paused|processing|completed|failed)$')
    error: str = Field(default='', max_length=300)
    output_name: str = Field(default='', max_length=180)
    output_bytes: int = Field(default=0, ge=0)
    output_duration: float = Field(default=0, ge=0, allow_inf_nan=False)


def install(app, p):
    ACTIVE = "status NOT IN ('completed','failed')"
    FREE = f"(SELECT COUNT(*) FROM recording_sessions s WHERE s.recorder_id=r.id AND s.{ACTIVE}) < r.capacity"

    def recorded(row, now=None):
        """Seconds actually recorded so far, excluding breaks (office mode)."""
        return row['rec_elapsed'] + ((now or time.time()) - row['rec_since'] if row['rec_since'] else 0)

    def public(row, online=True):
        return {k: row[k] for k in ('id', 'assignment_id', 'status', 'desired', 'segment', 'error', 'mode',
                                    'output_name', 'output_bytes', 'output_duration')} | {
            'office_online': online, 'elapsed': round(recorded(row), 1)}

    def session(con, sid):
        row = con.execute('SELECT * FROM recording_sessions WHERE id=?', (sid,)).fetchone()
        if row is None:
            raise HTTPException(404, 'Recording not found')
        return row

    def faculty_session(sid, user):
        with p.database() as con:
            row = session(con, sid)
        p.require_assignment(row['assignment_id'], user)
        return row

    def office(req: Request):
        token = req.headers.get('Authorization', '').removeprefix('Bearer ')
        if not token or len(token) > 200:
            raise HTTPException(401, 'Office recorder pairing required')
        digest = hashlib.sha256(token.encode()).hexdigest()
        with p.database() as con:
            row = con.execute('SELECT * FROM office_recorders WHERE token_hash=? AND active=1', (digest,)).fetchone()
        if row is None:
            raise HTTPException(401, 'Invalid office pairing code')
        return row

    def owned(con, sid, recorder):
        row = session(con, sid)
        if row['recorder_id'] != recorder['id']:
            raise HTTPException(403, 'Recording belongs to a different office recorder')
        return row

    def chunk_path(sid, segment, track, seq):
        # Values are either database UUIDs or constrained route parameters.
        if not re.fullmatch(r'[a-f0-9]{32}', sid) or track not in ('screen', 'camera') or not 0 <= seq <= 100000 or segment < 0:
            raise HTTPException(422, 'Invalid media chunk address')
        return p.DATA_DIR / 'recording_spool' / sid / f'{segment}-{track}-{seq}.bin'

    def all_saved(con, sid):
        segments = con.execute('SELECT * FROM recording_segments WHERE session_id=?', (sid,)).fetchall()
        if not segments or any(not s['sealed'] for s in segments):
            return False
        return not con.execute('SELECT 1 FROM recording_chunks WHERE session_id=? AND acked=0 LIMIT 1', (sid,)).fetchone()

    @app.get('/api/admin/recorders')
    def list_recorders(user=Depends(p.current_user)):
        p.require_admin(user)
        with p.database() as con:
            rows = con.execute('SELECT id,name,last_seen FROM office_recorders WHERE active=1').fetchall()
        return [dict(row) | {'online': time.time() - row['last_seen'] < ONLINE_SECONDS} for row in rows]

    @app.post('/api/admin/recorders', status_code=201)
    def pair(data: RecorderCreate, user=Depends(p.mutation_user)):
        p.require_admin(user)
        token, ident = secrets.token_urlsafe(40), uuid.uuid4().hex
        with p.database() as con:
            con.execute('INSERT INTO office_recorders(id,name,token_hash) VALUES(?,?,?)',
                        (ident, data.name.strip(), hashlib.sha256(token.encode()).hexdigest()))
        return {'id': ident, 'token': token, 'name': data.name.strip()}

    @app.delete('/api/admin/recorders/{rid}')
    def unpair(rid: str, user=Depends(p.mutation_user)):
        p.require_admin(user)
        with p.database() as con:
            con.execute('BEGIN IMMEDIATE')
            if con.execute("SELECT 1 FROM recording_sessions WHERE recorder_id=? AND status NOT IN ('completed','failed')", (rid,)).fetchone():
                raise HTTPException(409, 'This office recorder is saving a recording. Remove it after the recording finishes.')
            if not con.execute('UPDATE office_recorders SET active=0 WHERE id=? AND active=1', (rid,)).rowcount:
                raise HTTPException(404, 'Office recorder not found')
        return {'ok': True}

    @app.get('/api/admin/recorders/download')
    def download_installer(user=Depends(p.current_user)):
        p.require_admin(user)
        root = p.APP_ROOT.parent
        files = [root / 'Start Office Recorder.command', root / 'requirements-office.txt', root / 'RECORDING_SETUP.md']
        files += sorted((root / 'office_recorder').glob('*.py'))
        files += [root / 'office_recorder' / 'office.html']
        if any(not file.is_file() for file in files):
            raise HTTPException(503, 'Office installer files are missing from this deployment')
        buffer = io.BytesIO()
        with ZipFile(buffer, 'w', ZIP_DEFLATED) as archive:
            for file in files:
                archive.write(file, Path('DocTutorialsOfficeRecorder') / file.relative_to(root))
        return Response(buffer.getvalue(), media_type='application/zip',
                        headers={'Content-Disposition': 'attachment; filename="DocTutorialsOfficeRecorder-macOS.zip"',
                                 'Cache-Control': 'no-store'})

    @app.get('/api/assignments/{aid}/recording')
    def state(aid: str, user=Depends(p.current_user)):
        p.require_assignment(aid, user)
        with p.database() as con:
            row = con.execute('SELECT * FROM recording_sessions WHERE assignment_id=? ORDER BY created_at DESC LIMIT 1', (aid,)).fetchone()
            online = bool(con.execute('SELECT 1 FROM office_recorders WHERE active=1 AND last_seen>? LIMIT 1',
                                      (time.time() - ONLINE_SECONDS,)).fetchone())
            if row and row['status'] not in TERMINAL:
                agent = con.execute('SELECT last_seen FROM office_recorders WHERE id=?', (row['recorder_id'],)).fetchone()
                online = bool(agent and time.time() - agent['last_seen'] < ONLINE_SECONDS)
            capture = bool(con.execute(f'SELECT 1 FROM office_recorders r WHERE active=1 AND captures=1 AND last_seen>? AND {FREE} LIMIT 1',
                                       (time.time() - ONLINE_SECONDS,)).fetchone())
        # office_capture: an office desktop recorder is free to join and record this webinar itself.
        return (public(row, online) if row else {'id': None, 'status': 'idle', 'office_online': online}) | {'office_capture': capture}

    @app.post('/api/assignments/{aid}/recording/start', status_code=201)
    def start(aid: str, data: Start, user=Depends(p.mutation_user)):
        if user['role'] != 'faculty':
            raise HTTPException(403, 'Only assigned faculty can start recording')
        assignment = p.require_assignment(aid, user)
        if assignment['status'] != 'scheduled':
            raise HTTPException(409, 'This webinar is not scheduled')
        try:
            layout = Layout.from_dict(data.layout)
        except (TypeError, ValueError) as exc:
            raise HTTPException(422, str(exc)) from exc
        now = time.time()
        with p.database() as con:
            con.execute('BEGIN IMMEDIATE')
            if con.execute("SELECT 1 FROM recording_sessions WHERE assignment_id=? AND status NOT IN ('completed','failed')", (aid,)).fetchone():
                raise HTTPException(409, 'A recording is already active for this webinar')
            office = data.mode == 'office'
            # Least-loaded free recorder of the requested kind.
            recorder = con.execute(f'''SELECT r.id FROM office_recorders r WHERE r.active=1 AND r.last_seen>? AND r.captures=? AND {FREE}
                 ORDER BY (SELECT COUNT(*) FROM recording_sessions s WHERE s.recorder_id=r.id AND s.{ACTIVE}), r.last_seen DESC
                 LIMIT 1''', (now - ONLINE_SECONDS, 1 if office else 0)).fetchone()
            if recorder is None:
                raise HTTPException(503, 'Office recorder is offline or busy. Ask your administrator to open the office recorder.')
            sid = uuid.uuid4().hex
            con.execute('''INSERT INTO recording_sessions(id,assignment_id,faculty_id,recorder_id,status,desired,layout,created_at,updated_at,mode)
                           VALUES(?,?,?,?,?,?,?,?,?,?)''',
                        (sid, aid, user['id'], recorder['id'], 'starting' if office else 'awaiting_media', 'recording',
                         json.dumps(layout.to_dict()), now, now, data.mode))
            if not office:
                con.execute('INSERT INTO recording_segments(session_id,segment) VALUES(?,0)', (sid,))
            row = session(con, sid)
        return public(row)

    @app.post('/api/recordings/{sid}/chunks/{segment}/{track}/{seq}')
    async def upload(sid: str, segment: int, track: str, seq: int, req: Request, user=Depends(p.mutation_user)):
        row = faculty_session(sid, user)
        if user['role'] != 'faculty' or row['faculty_id'] != user['id']:
            raise HTTPException(403, 'Only this recording\'s faculty can send media')
        path = chunk_path(sid, segment, track, seq)
        blob = bytearray()
        async for data in req.stream():
            blob.extend(data)
            if len(blob) > CHUNK_LIMIT:
                raise HTTPException(413, 'Media chunk is too large')
        if not blob:
            raise HTTPException(422, 'Empty media chunk')
        digest = hashlib.sha256(blob).hexdigest()
        with p.database() as con:
            con.execute('BEGIN IMMEDIATE')
            row = session(con, sid)
            duplicate = con.execute('SELECT digest FROM recording_chunks WHERE session_id=? AND segment=? AND track=? AND seq=?',
                                    (sid, segment, track, seq)).fetchone()
            if duplicate:
                if duplicate['digest'] != digest:
                    raise HTTPException(409, 'Chunk retry does not match the original media')
                return {'ok': True}
            part = con.execute('SELECT * FROM recording_segments WHERE session_id=? AND segment=?', (sid, segment)).fetchone()
            if not part or part['sealed'] or row['status'] in TERMINAL or row['desired'] != 'recording' or row['segment'] != segment:
                raise HTTPException(409, 'Recording segment is closed')
            expected = con.execute('SELECT COUNT(*) FROM recording_chunks WHERE session_id=? AND segment=? AND track=?',
                                   (sid, segment, track)).fetchone()[0]
            if seq != expected:
                raise HTTPException(409, 'Media chunks must arrive in sequence')
            pending = con.execute('SELECT COALESCE(SUM(size),0) FROM recording_chunks WHERE acked=0').fetchone()[0]
            if pending + len(blob) > SPOOL_LIMIT:
                raise HTTPException(507, 'Office recorder is not keeping up. Recording stopped to protect server storage.')
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix('.pending')
            temporary.write_bytes(blob)
            temporary.replace(path)
            con.execute('INSERT INTO recording_chunks(session_id,segment,track,seq,size,digest) VALUES(?,?,?,?,?,?)',
                        (sid, segment, track, seq, len(blob), digest))
            con.execute('UPDATE recording_sessions SET updated_at=? WHERE id=?', (time.time(), sid))
        return {'ok': True}

    @app.post('/api/recordings/{sid}/segments/{segment}/seal')
    def seal(sid: str, segment: int, data: Seal, user=Depends(p.mutation_user)):
        row = faculty_session(sid, user)
        if row['faculty_id'] != user['id']:
            raise HTTPException(403, 'Only assigned faculty can control recording')
        with p.database() as con:
            con.execute('BEGIN IMMEDIATE')
            row = session(con, sid)
            if row['status'] in TERMINAL or row['segment'] != segment or row['desired'] != 'recording':
                raise HTTPException(409, 'Recording is not accepting this segment')
            for track in ('screen', 'camera'):
                count = con.execute('SELECT COUNT(*) FROM recording_chunks WHERE session_id=? AND segment=? AND track=?',
                                    (sid, segment, track)).fetchone()[0]
                if count != getattr(data, track + '_chunks'):
                    raise HTTPException(409, 'Some recording media has not finished uploading')
            con.execute('UPDATE recording_segments SET sealed=1,screen_chunks=?,camera_chunks=? WHERE session_id=? AND segment=?',
                        (data.screen_chunks, data.camera_chunks, sid, segment))
            con.execute('UPDATE recording_sessions SET desired=?,status=?,updated_at=? WHERE id=?',
                        ('paused' if data.action == 'pause' else 'stopped',
                         'pausing' if data.action == 'pause' else 'stopping', time.time(), sid))
        return {'ok': True}

    @app.post('/api/recordings/{sid}/{action}')
    def control(sid: str, action: str, user=Depends(p.mutation_user)):
        row = faculty_session(sid, user)
        if row['faculty_id'] != user['id'] or action not in ('pause', 'resume', 'stop', 'abort'):
            raise HTTPException(403, 'Invalid recording control')
        with p.database() as con:
            con.execute('BEGIN IMMEDIATE')
            row = session(con, sid)
            if row['status'] in TERMINAL:
                return public(row)
            if row['mode'] == 'office':
                # The office desktop recorder owns the capture; these only change what it should do.
                now = time.time()
                agent = con.execute('SELECT last_seen FROM office_recorders WHERE id=?', (row['recorder_id'],)).fetchone()
                online = bool(agent and now - agent['last_seen'] < ONLINE_SECONDS)
                if action == 'abort':
                    return public(row, online)  # closing the faculty tab must not stop an office recording
                if action == 'stop' and not online:
                    con.execute("UPDATE recording_sessions SET desired='stopped',status='failed',error=?,rec_elapsed=?,rec_since=0,updated_at=? WHERE id=?",
                                ('Office recorder went offline before it confirmed a saved recording. Check the office computer for the partial file.',
                                 recorded(row, now), now, sid))
                    return public(session(con, sid), online)
                allowed = {'pause': ('recording',), 'resume': ('paused',),
                           'stop': ('starting', 'recording', 'pausing', 'paused', 'resuming')}[action]
                if row['status'] not in allowed:
                    raise HTTPException(409, 'Wait for the office recorder to confirm the previous command')
                desired, status = {'pause': ('paused', 'pausing'), 'resume': ('recording', 'resuming'),
                                   'stop': ('stopped', 'stopping')}[action]
                con.execute('UPDATE recording_sessions SET desired=?,status=?,updated_at=? WHERE id=?', (desired, status, now, sid))
                return public(session(con, sid), online)
            if action == 'pause':
                raise HTTPException(409, 'Use Break in the tab that started the capture')
            if action == 'abort':
                con.execute("UPDATE recording_sessions SET desired='stopped',status='failed',error=?,updated_at=? WHERE id=?",
                            ('Capture or upload was interrupted. Office tracks are preserved for recovery.', time.time(), sid))
            elif row['status'] != 'paused':
                raise HTTPException(409, 'Wait for the office recorder to confirm the break')
            elif action == 'resume':
                number = row['segment'] + 1
                con.execute('INSERT INTO recording_segments(session_id,segment) VALUES(?,?)', (sid, number))
                con.execute("UPDATE recording_sessions SET desired='recording',status='awaiting_media',segment=?,updated_at=? WHERE id=?",
                            (number, time.time(), sid))
            else:
                con.execute("UPDATE recording_sessions SET desired='stopped',status='stopping',updated_at=? WHERE id=?", (time.time(), sid))
            row = session(con, sid)
        return public(row)

    @app.post('/api/office/heartbeat')
    def heartbeat(recorder=Depends(office)):
        with p.database() as con:
            con.execute('UPDATE office_recorders SET last_seen=? WHERE id=?', (time.time(), recorder['id']))
        return {'ok': True, 'name': recorder['name']}

    @app.post('/api/office/hello')
    def hello(data: Hello, recorder=Depends(office)):
        """Office desktop recorder announces that it captures webinars itself, and how many at once."""
        with p.database() as con:
            con.execute('UPDATE office_recorders SET last_seen=?,captures=1,capacity=? WHERE id=?',
                        (time.time(), data.capacity, recorder['id']))
        return {'ok': True, 'name': recorder['name']}

    @app.get('/api/office/captures')
    def captures(recorder=Depends(office)):
        with p.database() as con:
            con.execute('UPDATE office_recorders SET last_seen=? WHERE id=?', (time.time(), recorder['id']))
            rows = con.execute(f'''SELECT s.*, a.topic, a.subject FROM recording_sessions s JOIN assignments a ON a.id=s.assignment_id
                                   WHERE s.recorder_id=? AND s.mode='office' AND s.{ACTIVE} ORDER BY s.created_at''',
                               (recorder['id'],)).fetchall()
        return {'sessions': [public(row) | {'topic': row['topic'], 'subject': row['subject']} for row in rows]}

    @app.get('/api/office/recordings/{sid}/join')
    def office_join(sid: str, recorder=Depends(office)):
        """Attendee (view-only) join details so the office recorder can watch and record the webinar."""
        with p.database() as con:
            row = owned(con, sid, recorder)
            assignment = con.execute('SELECT webinar_id,passcode FROM assignments WHERE id=?', (row['assignment_id'],)).fetchone()
        if row['mode'] != 'office' or row['status'] in TERMINAL or assignment is None:
            raise HTTPException(409, 'This recording is not waiting for an office capture')
        if not p.setting('ZOOM_MEETING_SDK_CLIENT_ID') or not p.setting('ZOOM_MEETING_SDK_CLIENT_SECRET'):
            raise HTTPException(503, 'Admin must configure Zoom Meeting SDK credentials')
        issued_at = int(time.time()) - 30
        expiry = issued_at + 2 * 3600
        signature = p.jwt_hs256({'appKey': p.setting('ZOOM_MEETING_SDK_CLIENT_ID'), 'mn': assignment['webinar_id'], 'role': 0,
                                 'iat': issued_at, 'exp': expiry, 'tokenExp': expiry}, p.setting('ZOOM_MEETING_SDK_CLIENT_SECRET'))
        return Response(json.dumps({'meetingNumber': assignment['webinar_id'], 'passWord': assignment['passcode'] or '',
                                    'userName': 'DocTutorials Recorder',
                                    'userEmail': p.setting('ZOOM_RECORDER_EMAIL') or 'office-recorder@doctutorials.com',
                                    'signature': signature}),
                        media_type='application/json', headers={'Cache-Control': 'no-store', 'Pragma': 'no-cache'})

    @app.get('/api/office/work')
    def work(recorder=Depends(office)):
        with p.database() as con:
            con.execute('UPDATE office_recorders SET last_seen=? WHERE id=?', (time.time(), recorder['id']))
            row = con.execute("""SELECT s.* FROM recording_sessions s WHERE recorder_id=? AND mode='faculty' AND
                              (status NOT IN ('completed','failed') OR (status='failed' AND EXISTS
                              (SELECT 1 FROM recording_chunks c WHERE c.session_id=s.id AND c.acked=0)))
                              ORDER BY CASE WHEN status='failed' THEN 0 ELSE 1 END,created_at LIMIT 1""",
                              (recorder['id'],)).fetchone()
            if not row:
                return {'session': None}
            if row['status'] != 'failed' and row['desired'] == 'recording' and time.time() - row['updated_at'] > 60:
                con.execute("UPDATE recording_sessions SET status='failed',desired='stopped',error=? WHERE id=?",
                            ('Faculty capture stopped sending media. Office tracks are preserved.', row['id']))
                return {'session': None}
            segments = con.execute('SELECT * FROM recording_segments WHERE session_id=? ORDER BY segment', (row['id'],)).fetchall()
            chunks = con.execute('SELECT segment,track,seq,size,digest FROM recording_chunks WHERE session_id=? AND acked=0 ORDER BY segment,seq,track LIMIT 24',
                                 (row['id'],)).fetchall()
        return {'session': public(row) | {'layout': json.loads(row['layout']), 'segments': [dict(s) for s in segments],
                                          'chunks': [dict(c) for c in chunks]}}

    @app.get('/api/office/recordings/{sid}/chunks/{segment}/{track}/{seq}')
    def download(sid: str, segment: int, track: str, seq: int, recorder=Depends(office)):
        with p.database() as con:
            owned(con, sid, recorder)
        path = chunk_path(sid, segment, track, seq)
        if not path.exists():
            raise HTTPException(404, 'Media chunk not available')
        return FileResponse(path, media_type='application/octet-stream', headers={'Cache-Control': 'no-store'})

    @app.post('/api/office/recordings/{sid}/chunks/{segment}/{track}/{seq}/ack')
    def ack(sid: str, segment: int, track: str, seq: int, recorder=Depends(office)):
        path = chunk_path(sid, segment, track, seq)
        with p.database() as con:
            owned(con, sid, recorder)
            changed = con.execute('UPDATE recording_chunks SET acked=1 WHERE session_id=? AND segment=? AND track=? AND seq=?',
                                  (sid, segment, track, seq)).rowcount
            if not changed:
                raise HTTPException(404, 'Media chunk not found')
        path.unlink(missing_ok=True)
        return {'ok': True}

    @app.post('/api/office/recordings/{sid}/report')
    def report(sid: str, data: Report, recorder=Depends(office)):
        with p.database() as con:
            con.execute('BEGIN IMMEDIATE')
            row = owned(con, sid, recorder)
            if row['status'] in TERMINAL:
                return {'ok': True}
            if row['mode'] == 'office':
                now = time.time()
                if data.status in ('recording', 'paused') and row['desired'] != data.status:
                    raise HTTPException(409, 'The faculty has asked for a different recording state')
                if data.status == 'completed' and (not re.fullmatch(r'[A-Za-z0-9_.-]+\.mp4', data.output_name)
                                                    or data.output_bytes <= 0 or data.output_duration <= 0):
                    raise HTTPException(422, 'A verified saved MP4 is required before completion')
                # Count recorded time only while the office recorder confirms it is recording.
                since = (row['rec_since'] or now) if data.status == 'recording' else 0
                elapsed = row['rec_elapsed'] if data.status == 'recording' else recorded(row, now)
                desired = row['desired'] if data.status in ('recording', 'paused') else 'stopped'
                con.execute('''UPDATE recording_sessions SET status=?,desired=?,error=?,output_name=?,output_bytes=?,output_duration=?,
                               rec_elapsed=?,rec_since=?,updated_at=? WHERE id=?''',
                            (data.status, desired, data.error, data.output_name, data.output_bytes, data.output_duration,
                             elapsed, since, now, sid))
                return {'ok': True}
            if data.status == 'recording':
                count = con.execute('SELECT COUNT(DISTINCT track) FROM recording_chunks WHERE session_id=? AND segment=? AND acked=1',
                                    (sid, row['segment'])).fetchone()[0]
                if row['desired'] != 'recording' or count != 2:
                    raise HTTPException(409, 'Both media tracks must be saved on the office Mac first')
            elif data.status in ('paused', 'processing', 'completed'):
                expected = 'paused' if data.status == 'paused' else 'stopped'
                if row['desired'] != expected or not all_saved(con, sid):
                    raise HTTPException(409, 'Office media is not fully saved yet')
            if data.status == 'completed' and (not re.fullmatch(r'[A-Za-z0-9_.-]+\.mp4', data.output_name)
                                                or data.output_bytes <= 0 or data.output_duration <= 0):
                raise HTTPException(422, 'A verified saved MP4 is required before completion')
            con.execute('UPDATE recording_sessions SET status=?,error=?,output_name=?,output_bytes=?,output_duration=?,updated_at=? WHERE id=?',
                        (data.status, data.error, data.output_name, data.output_bytes, data.output_duration, time.time(), sid))
        return {'ok': True}
