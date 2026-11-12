"""DocTutorials faculty portal: staff authentication, assignment, Zoom host join authorization."""
from __future__ import annotations
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, InvalidHashError
from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from pydantic import BaseModel, Field, field_validator

APP_ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv('DT_DATA_DIR', str(APP_ROOT.parent / 'data')))
DB_PATH = DATA_DIR / 'app.db'
COOKIE_NAME = 'dt_faculty_session'
PH = PasswordHasher()


def setting(name: str) -> str:
    return os.getenv(name, '').strip()


def session_signer():
    key = setting('SESSION_SECRET')
    if len(key) < 32:
        raise RuntimeError('SESSION_SECRET must be at least 32 characters')
    return URLSafeTimedSerializer(key, salt='dt-faculty-v2')


@contextmanager
def database():
    connection = sqlite3.connect(DB_PATH, timeout=20)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


def init_database():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with database() as con:
        con.executescript('''
        CREATE TABLE IF NOT EXISTS users (
          id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE,
          display_name TEXT NOT NULL, email TEXT NOT NULL,
          zoom_user_id TEXT NOT NULL DEFAULT '',
          password_hash TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('admin','faculty')),
          active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS assignments (
          id TEXT PRIMARY KEY, webinar_id TEXT NOT NULL, passcode TEXT NOT NULL DEFAULT '',
          faculty_user_id TEXT NOT NULL REFERENCES users(id),
          course TEXT NOT NULL, category TEXT NOT NULL, subject TEXT NOT NULL,
          topic TEXT NOT NULL, starts_at TEXT NOT NULL DEFAULT '',
          status TEXT NOT NULL DEFAULT 'scheduled' CHECK(status IN ('scheduled','completed','cancelled')),
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          UNIQUE(webinar_id, faculty_user_id)
        );
        CREATE INDEX IF NOT EXISTS idx_assigned ON assignments(faculty_user_id, status);
        CREATE TABLE IF NOT EXISTS zoom_oauth_states (
            state TEXT PRIMARY KEY, user_id TEXT NOT NULL, created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS zoom_oauth_links (
            user_id TEXT PRIMARY KEY, zoom_id TEXT NOT NULL, zoom_email TEXT NOT NULL,
            refresh_token_encrypted TEXT NOT NULL, connected_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS zoom_join_failures (
            id TEXT PRIMARY KEY, assignment_id TEXT NOT NULL, faculty_id TEXT NOT NULL,
            stage TEXT NOT NULL, code TEXT NOT NULL, reason TEXT NOT NULL,
            tracking_id TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL
        );
        ''')
    from .recording import init_database as init_recording_database
    init_recording_database(database)


@asynccontextmanager
async def lifespan(application):
    session_signer()
    init_database()
    yield


app = FastAPI(title='DocTutorials Faculty Zoom Integration', docs_url=None, redoc_url=None, lifespan=lifespan)
app.mount('/static', StaticFiles(directory=APP_ROOT / 'static'), name='static')


class Login(BaseModel):
    username: str
    password: str


class FacultyCreate(BaseModel):
    username: str = Field(min_length=3, max_length=50)
    display_name: str = Field(min_length=2, max_length=120)
    email: str = Field(min_length=5, max_length=254)
    zoom_user_id: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=12, max_length=256)

    @field_validator('username', 'display_name', 'email', 'zoom_user_id', mode='before')
    @classmethod
    def trim_identity(cls, value):
        return value.strip() if isinstance(value, str) else value

    @field_validator('email')
    @classmethod
    def validate_email(cls, value):
        if not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', value):
            raise ValueError('Valid email is required')
        return value.lower()


class AssignmentCreate(BaseModel):
    faculty_username: str
    webinar_id: str
    passcode: str = Field(default='', max_length=128)
    course: str = Field(min_length=1, max_length=100)
    category: str = Field(min_length=1, max_length=100)
    subject: str = Field(min_length=1, max_length=120)
    topic: str = Field(min_length=1, max_length=180)
    starts_at: str = Field(default='', max_length=40)

    @field_validator('course', 'category', 'subject', 'topic', 'starts_at', mode='before')
    @classmethod
    def trim_metadata(cls, value):
        return value.strip() if isinstance(value, str) else value

    @field_validator('starts_at')
    @classmethod
    def validate_start(cls, value):
        if not value:
            return ''
        try:
            start = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError('Scheduled time must be an ISO 8601 date/time') from exc
        if start.tzinfo is None:
            raise ValueError('Scheduled time must include a timezone offset')
        return start.astimezone(timezone.utc).isoformat()


def current_user(req: Request):
    cookie = req.cookies.get(COOKIE_NAME, '')
    try:
        record = session_signer().loads(cookie, max_age=12 * 3600)
        if not isinstance(record.get('csrf'), str) or not record['csrf']:
            raise ValueError('Invalid session')
        with database() as con:
            user = con.execute('SELECT * FROM users WHERE id=? AND active=1', (record.get('uid'),)).fetchone()
        if not user:
            raise ValueError('Inactive session')
        req.state.csrf = record.get('csrf')
        req.state.user = dict(user)
        return dict(user)
    except (BadSignature, SignatureExpired, ValueError, TypeError, AttributeError):
        raise HTTPException(401, 'Please log in')


def mutation_user(req: Request, user=Depends(current_user)):
    candidate = req.headers.get('X-CSRF-Token', '')
    if not candidate or not hmac.compare_digest(candidate.encode(), req.state.csrf.encode()):
        raise HTTPException(403, 'Invalid CSRF token')
    return user


def require_admin(user):
    if user['role'] != 'admin':
        raise HTTPException(403, 'Administrator role required')


def require_assignment(assignment_id, user):
    with database() as con:
        row = con.execute('''SELECT a.*, u.display_name, u.email, u.zoom_user_id, u.active
              FROM assignments a JOIN users u ON u.id=a.faculty_user_id WHERE a.id=?''',
              (assignment_id,)).fetchone()
    if not row:
        raise HTTPException(404, 'Assignment not found')
    if user['role'] != 'admin' and row['faculty_user_id'] != user['id']:
        raise HTTPException(403, 'This webinar is not assigned to you')
    if not row['active']:
        raise HTTPException(403, 'Faculty account is disabled')
    return dict(row)


def public_assignment(row):
    return {k: row[k] for k in ('id', 'webinar_id', 'course', 'category', 'subject', 'topic', 'starts_at', 'status')}


def jwt_hs256(payload, secret):
    def b64(data):
        return base64.urlsafe_b64encode(json.dumps(data, separators=(',', ':')).encode()).rstrip(b'=').decode()
    msg = b64({'alg': 'HS256', 'typ': 'JWT'}) + '.' + b64(payload)
    sig = base64.urlsafe_b64encode(hmac.new(secret.encode(), msg.encode(), hashlib.sha256).digest()).rstrip(b'=').decode()
    return msg + '.' + sig


class ZoomError(Exception):
    pass


def matches_zoom_identity(user, profile_id, profile_email):
    """Match the host explicitly assigned by admin, independently of faculty email."""
    identity = user['zoom_user_id'].strip()
    email = profile_email.strip().casefold()
    if not profile_id or not email:
        return False
    return identity.casefold() == email if '@' in identity else identity == profile_id


async def zoom_access_token():
    required = ['ZOOM_ACCOUNT_ID', 'ZOOM_S2S_CLIENT_ID', 'ZOOM_S2S_CLIENT_SECRET']
    if any(not setting(x) for x in required):
        raise ZoomError('Zoom Server-to-Server OAuth is not configured by admin')
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            result = await client.post('https://zoom.us/oauth/token',
                params={'grant_type': 'account_credentials', 'account_id': setting('ZOOM_ACCOUNT_ID')},
                auth=(setting('ZOOM_S2S_CLIENT_ID'), setting('ZOOM_S2S_CLIENT_SECRET')))
            result.raise_for_status()
            token = result.json().get('access_token')
            if not token:
                raise ZoomError('Zoom did not return an OAuth access token')
            return token
    except (httpx.HTTPError, ValueError) as exc:
        raise ZoomError('Zoom OAuth authorization failed: ' + str(exc)) from exc


async def zoom_host_token(user_id: str, access_token: str):
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            result = await client.get(f'https://api.zoom.us/v2/users/{quote(user_id, safe="")}/token',
                params={'type': 'zak'}, headers={'Authorization': 'Bearer ' + access_token})
            result.raise_for_status()
            token = result.json().get('token')
            if not token:
                raise ZoomError('Zoom did not provide a ZAK token')
            return token
    except (httpx.HTTPError, ValueError) as exc:
        raise ZoomError('Administrator must enable user:read:token:admin on the Zoom '
                        'Server-to-Server OAuth app and ensure the assigned host belongs to that account.') from exc


async def verify_webinar_host(webinar_id: str, user: dict, access_token: str):
    """Fail closed if assignment is not hosted by the faculty Zoom user.
    Alternative hosts are only accepted when Zoom returns them explicitly.
    """
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            res = await client.get(f'https://api.zoom.us/v2/webinars/{webinar_id}',
                headers={'Authorization': 'Bearer ' + access_token})
            res.raise_for_status()
            detail = res.json()
            # Resolve the configured identity even when the webinar's host email matches.
            # A ZAK must belong to this same account, not just a matching contact email.
            resolved_response = await client.get(
                f'https://api.zoom.us/v2/users/{quote(user["zoom_user_id"], safe="")}',
                headers={'Authorization': 'Bearer ' + access_token})
            resolved_response.raise_for_status()
            resolved = resolved_response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise ZoomError('Cannot verify webinar host using Zoom API; check webinar read scopes: ' + str(exc)) from exc
    profile_id = str(resolved.get('id') or '')
    email = str(resolved.get('email') or '').strip().casefold()
    if not matches_zoom_identity(user, profile_id, email):
        raise ZoomError('Configured Zoom host identity does not match the Zoom user record')
    if str(resolved.get('account_id') or '') != setting('ZOOM_ACCOUNT_ID'):
        raise ZoomError('Assigned Zoom host must belong to the administrator-managed Zoom account')
    alternatives = str((detail.get('settings') or {}).get('alternative_hosts') or '')
    if (profile_id == detail.get('host_id')
            or email == str(detail.get('host_email') or '').strip().casefold()
            or email in {s.strip().casefold() for s in alternatives.split(';')}):
        return {'id': profile_id, 'email': email}
    raise ZoomError('Assigned faculty is not the Zoom host or an explicitly listed alternative host')


@app.get('/')
def homepage():
    return FileResponse(APP_ROOT / 'static/index.html')


@app.get('/meeting/{assignment_id}')
def meeting(assignment_id: str, user=Depends(current_user)):
    row = require_assignment(assignment_id, user)
    if row['status'] != 'scheduled':
        raise HTTPException(409, 'Webinar is not scheduled')
    return FileResponse(APP_ROOT / 'static/meeting.html')


@app.get('/health')
def health():
    return {'ok': True, 'app': 'DocTutorials Faculty', 'sdk_configured': bool(setting('ZOOM_MEETING_SDK_CLIENT_ID')),
            'zoom_api_configured': all(setting(k) for k in ('ZOOM_ACCOUNT_ID','ZOOM_S2S_CLIENT_ID','ZOOM_S2S_CLIENT_SECRET'))}


@app.post('/api/login')
def login(data: Login):
    with database() as con:
        user = con.execute('SELECT * FROM users WHERE username=? AND active=1',
                           (data.username.lower().strip(),)).fetchone()
    try:
        valid = user is not None and PH.verify(user['password_hash'], data.password)
    except (VerificationError, InvalidHashError):
        valid = False
    if not valid:
        raise HTTPException(401, 'Invalid username or password')
    csrf = secrets.token_urlsafe(32)
    signed = session_signer().dumps({'uid': user['id'], 'csrf': csrf})
    response = JSONResponse({'display_name': user['display_name'], 'role': user['role'], 'csrf': csrf})
    response.set_cookie(COOKIE_NAME, signed, httponly=True, secure=setting('SESSION_SECURE').lower() == 'true',
                        samesite='lax', max_age=12*3600, path='/')
    return response


@app.get('/api/me')
def me(req: Request, user=Depends(current_user)):
    return {'username': user['username'], 'display_name': user['display_name'], 'email': user['email'],
            'role': user['role'], 'csrf': req.state.csrf}


@app.post('/api/logout')
def logout(user=Depends(mutation_user)):
    response = JSONResponse({'ok': True})
    response.delete_cookie(COOKIE_NAME, path='/')
    return response


@app.get('/api/admin/faculty')
def faculty(user=Depends(current_user)):
    require_admin(user)
    with database() as con:
        rows = con.execute("SELECT username, display_name, email, zoom_user_id FROM users WHERE role='faculty' AND active=1 ORDER BY display_name").fetchall()
    return [dict(row) for row in rows]


@app.get('/api/admin/zoom-setup')
def zoom_setup(user=Depends(current_user)):
    require_admin(user)
    try:
        redirect_uri = oauth_redirect_uri()
    except (HTTPException, ValueError):
        redirect_uri = ''
    return {'redirect_uri': redirect_uri,
            'faculty_zoom_login_required': False,
            'sdk_configured': all(setting(k) for k in (
                'ZOOM_MEETING_SDK_CLIENT_ID', 'ZOOM_MEETING_SDK_CLIENT_SECRET')),
            'api_configured': all(setting(k) for k in (
                'ZOOM_ACCOUNT_ID', 'ZOOM_S2S_CLIENT_ID', 'ZOOM_S2S_CLIENT_SECRET'))}


@app.post('/api/admin/faculty', status_code=201)
def add_faculty(data: FacultyCreate, user=Depends(mutation_user)):
    require_admin(user)
    name = data.username.strip().lower()
    if not re.fullmatch(r'[a-z0-9._-]{3,50}', name):
        raise HTTPException(422, 'Username must contain 3-50 letters, numbers, dot, dash or underscore')
    if '@' not in data.email:
        raise HTTPException(422, 'Valid email is required')
    try:
        with database() as con:
            con.execute('''INSERT INTO users(id, username, display_name, email, zoom_user_id, password_hash, role)
                           VALUES(?,?,?,?,?,?,?)''', (uuid.uuid4().hex, name, data.display_name.strip(),
                            data.email.strip().lower(), data.zoom_user_id.strip(), PH.hash(data.password), 'faculty'))
    except sqlite3.IntegrityError:
        raise HTTPException(409, 'Username already exists')
    return {'ok': True, 'username': name}


@app.get('/api/assignments')
def assignments(user=Depends(current_user)):
    sql = '''SELECT a.*, u.display_name AS faculty_name, u.username AS faculty_username
             FROM assignments a JOIN users u ON u.id=a.faculty_user_id'''
    params = ()
    if user['role'] != 'admin':
        sql += ' WHERE a.faculty_user_id=?'
        params = (user['id'],)
    sql += ' ORDER BY a.starts_at, a.created_at'
    with database() as con:
        rows = con.execute(sql, params).fetchall()
    return [{**public_assignment(row), 'faculty_name': row['faculty_name'], 'faculty_username': row['faculty_username']} for row in rows]


@app.post('/api/admin/assignments', status_code=201)
def assign(data: AssignmentCreate, user=Depends(mutation_user)):
    require_admin(user)
    number = re.sub(r'[\s-]', '', data.webinar_id)
    if not re.fullmatch(r'[0-9]{9,13}', number):
        raise HTTPException(422, 'Enter a valid numeric Zoom Webinar ID')
    with database() as con:
        faculty = con.execute("SELECT id FROM users WHERE username=? AND role='faculty' AND active=1",
                              (data.faculty_username.lower().strip(),)).fetchone()
        if not faculty:
            raise HTTPException(404, 'Faculty account not found')
        try:
            ident = uuid.uuid4().hex
            con.execute('''INSERT INTO assignments(id, webinar_id, passcode, faculty_user_id, course, category, subject, topic, starts_at)
                           VALUES(?,?,?,?,?,?,?,?,?)''',
                        (ident, number, data.passcode, faculty['id'], data.course.strip(), data.category.strip(),
                         data.subject.strip(), data.topic.strip(), data.starts_at.strip()))
        except sqlite3.IntegrityError:
            raise HTTPException(409, 'This webinar is already assigned to this faculty')
    return {'id': ident, 'ok': True}


@app.get('/api/assignments/{assignment_id}')
def assignment(assignment_id: str, user=Depends(current_user)):
    row = require_assignment(assignment_id, user)
    return public_assignment(row)


class JoinOptions(BaseModel):
    # A documented Zoom compatibility flag; never supplied from the admin UI.
    compatibility: bool = False


@app.post('/api/assignments/{assignment_id}/join-credentials')
async def join_credentials(assignment_id: str, options: JoinOptions, user=Depends(mutation_user)):
    if user['role'] != 'faculty':
        raise HTTPException(403, 'Only assigned faculty can start webinars')
    row = require_assignment(assignment_id, user)
    if row['status'] != 'scheduled':
        raise HTTPException(409, 'Webinar is not scheduled')
    if not setting('ZOOM_MEETING_SDK_CLIENT_ID') or not setting('ZOOM_MEETING_SDK_CLIENT_SECRET'):
        raise HTTPException(503, 'Admin must configure Zoom Meeting SDK credentials')
    if not row['zoom_user_id']:
        raise HTTPException(409, 'Admin must link faculty to their Zoom host account')
    try:
        access_token = await zoom_access_token()
        host = await verify_webinar_host(row['webinar_id'], row, access_token)
        zak = await zoom_host_token(host['id'], access_token)
    except ZoomError as exc:
        raise HTTPException(502, str(exc))
    issued_at = int(time.time()) - 30
    expiry = issued_at + 2 * 3600
    claims = {'appKey': setting('ZOOM_MEETING_SDK_CLIENT_ID'), 'mn': row['webinar_id'],
              'role': 1, 'iat': issued_at, 'exp': expiry, 'tokenExp': expiry}
    if options.compatibility:
        # Supported by Zoom's Meeting SDK web JWT: disables WebRTC *video* only.
        # This does not disable WebRTC signaling/audio or bypass Zoom security.
        claims['video_webrtc_mode'] = 0
    signature = jwt_hs256(claims, setting('ZOOM_MEETING_SDK_CLIENT_SECRET'))
    # No secrets in logs; these values are ephemeral and only sent to the authorized faculty.
    return JSONResponse({'meetingNumber': row['webinar_id'], 'passWord': row['passcode'],
            'userName': row['display_name'], 'userEmail': host['email'],
            'signature': signature, 'zak': zak},
            headers={'Cache-Control':'no-store', 'Pragma':'no-cache'})


# The callback is retained for existing Marketplace configuration and old bookmarks.
# Faculty host authorization uses the administrator's S2S app, not user OAuth.
def redact_zoom_diagnostic(value):
    value = re.sub(r'\beyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b', '[redacted token]', value)
    return re.sub(r'''\b(zak|signature|password|access_token|refresh_token)(["']?\s*[:=]\s*["']?)[^\s"'&,}]+''',
                  r'\1\2[redacted]', value, flags=re.IGNORECASE)


class ZoomJoinDiagnostic(BaseModel):
    stage: str = Field(pattern=r'^(authorization|sdk_load|initialization|join)$')
    code: str = Field(pattern=r'^[A-Za-z0-9_-]{1,30}$')
    reason: str = Field(max_length=300)
    tracking_id: str = Field(default='', pattern=r'^[A-Za-z0-9_.;=:-]{0,200}$')


@app.post('/api/assignments/{assignment_id}/zoom-diagnostics', status_code=201)
def save_zoom_diagnostic(assignment_id: str, data: ZoomJoinDiagnostic, user=Depends(mutation_user)):
    require_assignment(assignment_id, user)
    if user['role'] != 'faculty':
        raise HTTPException(403, 'Only assigned faculty can report join failures')
    with database() as con:
        con.execute('INSERT INTO zoom_join_failures VALUES(?,?,?,?,?,?,?,?)',
                    (uuid.uuid4().hex, assignment_id, user['id'], data.stage, data.code,
                     redact_zoom_diagnostic(data.reason), redact_zoom_diagnostic(data.tracking_id), time.time()))
        con.execute('DELETE FROM zoom_join_failures WHERE assignment_id=? AND id NOT IN '
                    '(SELECT id FROM zoom_join_failures WHERE assignment_id=? ORDER BY created_at DESC LIMIT 20)',
                    (assignment_id, assignment_id))
    return {'ok': True}


@app.get('/api/admin/zoom-diagnostics')
def zoom_diagnostics(user=Depends(current_user)):
    require_admin(user)
    with database() as con:
        rows = con.execute('''SELECT d.*,a.webinar_id FROM zoom_join_failures d
                              JOIN assignments a ON a.id=d.assignment_id ORDER BY d.created_at DESC LIMIT 40''').fetchall()
    return [dict(row) for row in rows]


def oauth_redirect_uri():
    uri = setting('ZOOM_OAUTH_REDIRECT_URI')
    parsed = urlsplit(uri)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path != '/auth/zoom/callback'):
        raise HTTPException(503, 'Administrator must configure the HTTPS Zoom OAuth callback URL')
    return uri



@app.get('/api/zoom/status')
def zoom_connection_status(user=Depends(current_user)):
    configured = all(setting(k) for k in (
        'ZOOM_MEETING_SDK_CLIENT_ID', 'ZOOM_MEETING_SDK_CLIENT_SECRET',
        'ZOOM_ACCOUNT_ID', 'ZOOM_S2S_CLIENT_ID', 'ZOOM_S2S_CLIENT_SECRET'))
    return {'auth_mode': 'server', 'faculty_zoom_login_required': False,
            'configured': configured, 'connected': False, 'zoom_email': None}


@app.get('/auth/zoom/start')
@app.get('/auth/zoom/callback')
def legacy_zoom_oauth(user=Depends(current_user)):
    # Never send faculty to a Zoom sign-in or exchange old authorization codes.
    return RedirectResponse('/', status_code=303,
                            headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'})


from .recording import install as install_recording
import sys
install_recording(app, sys.modules[__name__])
