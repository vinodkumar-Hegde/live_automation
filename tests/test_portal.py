import base64
import json
import os

os.environ['SESSION_SECRET']='m'*68
os.environ.pop('ZOOM_MEETING_SDK_CLIENT_ID',None)
os.environ.pop('ZOOM_MEETING_SDK_CLIENT_SECRET',None)

import pytest
from fastapi.testclient import TestClient
from app import main


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(main, 'DB_PATH', tmp_path / 'app.db')
    with TestClient(main.app) as c:
        with main.database() as con:
            for name, role in [('admin','admin'), ('alpha','faculty'), ('beta','faculty')]:
                con.execute('''INSERT INTO users(id,username,display_name,email,zoom_user_id,password_hash,role)
                            VALUES(?,?,?,?,?,?,?)''',
                            (name, name, name.title(), name+'@dt.test', name+'@dt.test', main.PH.hash('test-password-123'), role))
        yield c


def login(c,name):
    response=c.post('/api/login',json={'username':name,'password':'test-password-123'})
    assert response.status_code==200
    return response.json()['csrf']


def payload(faculty):
    return {'faculty_username':faculty,'webinar_id':'12345678901','passcode':'secret','course':'NEET PG','category':'Live','subject':'Medicine','topic':'Parkinson','starts_at':''}


def test_login_and_assign_and_access_separation(client):
    assert client.get('/api/assignments').status_code==401
    csrf=login(client,'admin')
    result=client.post('/api/admin/assignments',json=payload('alpha'),headers={'X-CSRF-Token':csrf})
    assert result.status_code==201, result.text
    aid=result.json()['id']
    assert len(client.get('/api/assignments').json())==1
    assert 'passcode' not in json.dumps(client.get('/api/assignments').json())
    assert client.post('/api/logout',json={},headers={'X-CSRF-Token':csrf}).status_code==200
    login(client,'beta')
    assert client.get('/api/assignments').json()==[]
    assert client.get('/api/assignments/'+aid).status_code==403
    assert client.get('/meeting/'+aid).status_code==403
    assert client.post('/api/assignments/'+aid+'/join-credentials',json={}).status_code==403
    client.cookies.clear()
    login(client,'alpha')
    assert len(client.get('/api/assignments').json())==1
    assert client.get('/meeting/'+aid).status_code==200


def test_csrf_and_role_check(client):
    csrf=login(client,'alpha')
    assert client.post('/api/admin/assignments',json=payload('alpha'),headers={'X-CSRF-Token':csrf}).status_code==403
    assert client.post('/api/logout',json={}).status_code==403
    assert client.post('/api/logout',json={},headers={'X-CSRF-Token':csrf}).status_code==200


def test_missing_zoom_configuration_no_fake_success(client):
    csrf=login(client,'admin')
    aid=client.post('/api/admin/assignments',json=payload('alpha'),headers={'X-CSRF-Token':csrf}).json()['id']
    client.cookies.clear()
    csrf=login(client,'alpha')
    res=client.post('/api/assignments/'+aid+'/join-credentials',json={},headers={'X-CSRF-Token':csrf})
    assert res.status_code==503
    assert 'configure' in res.json()['detail'].lower()


def test_authorized_join_credentials_uses_zak_and_role_one(client,monkeypatch):
    csrf=login(client,'admin')
    aid=client.post('/api/admin/assignments',json=payload('alpha'),headers={'X-CSRF-Token':csrf}).json()['id']
    client.cookies.clear()
    csrf=login(client,'alpha')
    monkeypatch.setenv('ZOOM_MEETING_SDK_CLIENT_ID','sdk-id')
    monkeypatch.setenv('ZOOM_MEETING_SDK_CLIENT_SECRET','sdk-secret')
    async def access():return 'fake-oauth'
    async def verify(number,user,access):
        assert number=='12345678901' and user['zoom_user_id']=='alpha@dt.test' and access=='fake-oauth'
        return {'id': 'canonical-zoom-id', 'email': 'alpha@dt.test'}
    async def zak(host_id, access):
        assert host_id == 'canonical-zoom-id' and access == 'fake-oauth'
        return 'fake-zak-token'
    monkeypatch.setattr(main,'zoom_access_token',access)
    monkeypatch.setattr(main,'verify_webinar_host',verify)
    monkeypatch.setattr(main,'zoom_host_token',zak)
    result=client.post('/api/assignments/'+aid+'/join-credentials',json={},headers={'X-CSRF-Token':csrf})
    assert result.status_code==200, result.text
    data=result.json()
    assert data['passWord']=='secret' and data['zak']=='fake-zak-token'
    encoded=data['signature'].split('.')[1]
    decoded_payload=json.loads(base64.urlsafe_b64decode(encoded+'='*(-len(encoded)%4)))
    assert decoded_payload['role']==1 and decoded_payload['appKey']=='sdk-id' and decoded_payload['mn']=='12345678901'


def test_faculty_creation_admin_only(client):
    csrf=login(client,'admin')
    request={'username':'faculty3','display_name':'Dr Demo','email':'demo@dt.test','zoom_user_id':'demo@dt.test','password':'long-enough-123'}
    assert client.post('/api/admin/faculty',json=request).status_code==403
    r=client.post('/api/admin/faculty',json=request,headers={'X-CSRF-Token':csrf})
    assert r.status_code==201
    listing=client.get('/api/admin/faculty').json()
    assert any(x['username']=='faculty3' for x in listing)


@pytest.mark.parametrize('field,value', [
    ('course', '   '), ('category', '\t'), ('subject', ' '), ('topic', ' '),
    ('starts_at', 'not-a-date'), ('starts_at', '2026-11-12T10:00'),
    ('webinar_id', '１２３４５６７８９０１'),
])
def test_invalid_assignment_rejected(client, field, value):
    csrf = login(client, 'admin')
    data = {**payload('alpha'), field: value}
    assert client.post('/api/admin/assignments', json=data,
                       headers={'X-CSRF-Token': csrf}).status_code == 422


def test_schedule_preserves_instant_across_timezones(client):
    csrf = login(client, 'admin')
    data = {**payload('alpha'), 'starts_at': '2026-11-12T10:00:00+05:30'}
    result = client.post('/api/admin/assignments', json=data, headers={'X-CSRF-Token': csrf})
    assert result.status_code == 201
    assigned = client.get('/api/assignments/' + result.json()['id']).json()
    assert assigned['starts_at'] == '2026-11-12T04:30:00+00:00'


@pytest.mark.parametrize('field,value', [
    ('display_name', '   '), ('zoom_user_id', '   '), ('email', 'a @dt.test'),
])
def test_invalid_faculty_identity_rejected(client, field, value):
    csrf = login(client, 'admin')
    data = {'username': 'faculty3', 'display_name': 'Dr Demo', 'email': 'demo@dt.test',
            'zoom_user_id': 'demo@dt.test', 'password': 'long-enough-123', field: value}
    assert client.post('/api/admin/faculty', json=data,
                       headers={'X-CSRF-Token': csrf}).status_code == 422


def test_session_without_csrf_is_rejected(client):
    client.cookies.set(main.COOKIE_NAME, main.session_signer().dumps({'uid': 'alpha'}))
    assert client.get('/api/me').status_code == 401
    assert client.post('/api/logout', json={}, headers={'X-CSRF-Token': 'candidate'}).status_code == 401


def test_non_ascii_csrf_rejected_without_server_error(client):
    login(client, 'alpha')
    assert client.post('/api/logout', json={},
                       headers={'X-CSRF-Token': b'\xff'}).status_code == 403


def test_join_diagnostics_are_authorized_and_redact_credentials(client):
    csrf = login(client, 'admin')
    aid = client.post('/api/admin/assignments', json=payload('alpha'), headers={'X-CSRF-Token': csrf}).json()['id']
    client.cookies.clear()
    csrf = login(client, 'alpha')
    url = '/api/assignments/' + aid + '/zoom-diagnostics'
    report = {'stage': 'join', 'code': '1', 'reason': 'Join refused password=private-pass zak=eyJhbGciOiJIUzI1NiJ9.eyJkYXRhIjoiU0VDUkVUIn0.c2lnbmF0dXJl',
              'tracking_id': 'v=2.0;clid=us06;rid=WEB_test'}
    assert client.post(url, json=report).status_code == 403
    assert client.post(url, json=report, headers={'X-CSRF-Token': csrf}).status_code == 201
    assert client.get('/api/admin/zoom-diagnostics').status_code == 403
    client.cookies.clear()
    csrf = login(client, 'beta')
    assert client.post(url, json=report, headers={'X-CSRF-Token': csrf}).status_code == 403
    client.cookies.clear()
    login(client, 'admin')
    records = client.get('/api/admin/zoom-diagnostics').json()
    assert len(records) == 1 and records[0]['code'] == '1'
    assert records[0]['tracking_id'].endswith('WEB_test')
    assert 'private-pass' not in json.dumps(records) and 'eyJhbGciOi' not in json.dumps(records)


@pytest.mark.parametrize('token_allowed', [True, False])
def test_server_host_join_needs_no_faculty_oauth_or_callback(client, monkeypatch, token_allowed):
    import httpx
    monkeypatch.setenv('ZOOM_MEETING_SDK_CLIENT_ID', 'sdk-id')
    monkeypatch.setenv('ZOOM_MEETING_SDK_CLIENT_SECRET', 'sdk-secret')
    monkeypatch.setenv('ZOOM_ACCOUNT_ID', 'managed-account')
    monkeypatch.setenv('ZOOM_S2S_CLIENT_ID', 's2s-id')
    monkeypatch.setenv('ZOOM_S2S_CLIENT_SECRET', 's2s-secret')
    monkeypatch.delenv('ZOOM_OAUTH_REDIRECT_URI', raising=False)
    calls = []

    def respond(request):
        calls.append(request.url.path)
        if request.url.path == '/oauth/token':
            assert request.url.params['grant_type'] == 'account_credentials'
            return httpx.Response(200, json={'access_token': 'server-access'})
        assert request.headers['Authorization'] == 'Bearer server-access'
        if request.url.path == '/v2/webinars/12345678901':
            return httpx.Response(200, json={'host_id': 'canonical-id'})
        if request.url.path == '/v2/users/alpha@dt.test':
            return httpx.Response(200, json={'id': 'canonical-id', 'email': 'alpha@dt.test', 'account_id': 'managed-account'})
        assert request.url.path == '/v2/users/canonical-id/token'
        assert request.url.params['type'] == 'zak'
        return httpx.Response(200, json={'token': 'server-issued-zak'}) if token_allowed else httpx.Response(403)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(main.httpx, 'AsyncClient',
                        lambda **kwargs: real_client(transport=httpx.MockTransport(respond), **kwargs))
    csrf = login(client, 'admin')
    aid = client.post('/api/admin/assignments', json=payload('alpha'), headers={'X-CSRF-Token': csrf}).json()['id']
    # A company host can be explicitly assigned to faculty with a different contact email.
    with main.database() as con:
        con.execute("UPDATE users SET email='faculty-contact@dt.test' WHERE id='alpha'")
    client.cookies.clear()
    csrf = login(client, 'alpha')
    result = client.post('/api/assignments/' + aid + '/join-credentials', json={}, headers={'X-CSRF-Token': csrf})
    assert result.status_code == (200 if token_allowed else 502)
    assert 'connect_url' not in result.json()
    if token_allowed:
        assert result.json()['zak'] == 'server-issued-zak'
        assert result.json()['userEmail'] == 'alpha@dt.test'
        assert result.headers['Cache-Control'] == 'no-store'
    else:
        assert 'user:read:token:admin' in result.json()['detail']
    with main.database() as con:
        assert con.execute('SELECT COUNT(*) FROM zoom_oauth_links').fetchone()[0] == 0
        assert con.execute('SELECT COUNT(*) FROM zoom_oauth_states').fetchone()[0] == 0
    assert len(calls) == 4
