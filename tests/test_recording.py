import json
import base64
import hashlib
import shutil
import subprocess
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from app import main
from office_recorder.agent import OfficeAgent


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(main, 'DB_PATH', tmp_path / 'app.db')
    monkeypatch.setenv('SESSION_SECRET', 's' * 64)
    with TestClient(main.app) as c:
        with main.database() as con:
            for name, role in [('admin', 'admin'), ('alpha', 'faculty'), ('beta', 'faculty')]:
                con.execute('INSERT INTO users(id,username,display_name,email,zoom_user_id,password_hash,role) VALUES(?,?,?,?,?,?,?)',
                            (name, name, name, name + '@test.local', name + '@test.local', main.PH.hash('long-password-123'), role))
            con.execute("INSERT INTO assignments(id,webinar_id,faculty_user_id,course,category,subject,topic) VALUES('class-a','12345678901','alpha','Course','Live','Subject','Topic')")
        yield c


def login(client, name):
    return client.post('/api/login', json={'username': name, 'password': 'long-password-123'}).json()['csrf']


def setup(client):
    csrf = login(client, 'admin')
    paired = client.post('/api/admin/recorders', json={'name': 'Office Mac'}, headers={'X-CSRF-Token': csrf}).json()
    office = {'Authorization': 'Bearer ' + paired['token']}
    client.post('/api/office/heartbeat', headers=office)
    csrf = login(client, 'alpha')
    faculty = {'X-CSRF-Token': csrf}
    started = client.post('/api/assignments/class-a/recording/start', json={}, headers=faculty)
    assert started.status_code == 201, started.text
    return started.json()['id'], faculty, office


def send(client, sid, faculty, number=0, screen=b'screen', camera=b'camera'):
    for track, data in [('screen', screen), ('camera', camera)]:
        assert client.post(f'/api/recordings/{sid}/chunks/{number}/{track}/0', content=data, headers=faculty).status_code == 200


def acknowledge(client, sid, office, number=0):
    for track in ('screen', 'camera'):
        assert client.post(f'/api/office/recordings/{sid}/chunks/{number}/{track}/0/ack', headers=office).status_code == 200


def test_pairing_authentication_and_no_fake_office_connection(client):
    assert client.get('/api/office/work').status_code == 401
    assert client.post('/api/admin/recorders', json={'name': 'Mac'}).status_code == 401
    csrf = login(client, 'alpha')
    assert client.post('/api/admin/recorders', json={'name': 'Mac'}, headers={'X-CSRF-Token': csrf}).status_code == 403
    assert client.post('/api/assignments/class-a/recording/start', json={}, headers={'X-CSRF-Token': csrf}).status_code == 503
    assert client.get('/api/assignments/class-a/recording').json()['office_online'] is False
    login(client, 'beta')
    assert client.get('/api/assignments/class-a/recording').status_code == 403


def test_mac_installer_download_is_admin_only_and_contains_no_server_data(client):
    import io
    from zipfile import ZipFile
    route = '/api/admin/recorders/download'
    assert client.get(route).status_code == 401
    login(client, 'alpha')
    assert client.get(route).status_code == 403
    login(client, 'admin')
    response = client.get(route)
    assert response.status_code == 200
    with ZipFile(io.BytesIO(response.content)) as archive:
        names = archive.namelist()
    assert len(names) == 8
    assert not any('/.env' in name or '/data/' in name or 'app.db' in name for name in names)


def test_recording_commands_require_durable_office_acknowledgement(client):
    sid, faculty, office = setup(client)
    assert client.post('/api/assignments/class-a/recording/start', json={}, headers=faculty).status_code == 409
    report = f'/api/office/recordings/{sid}/report'
    assert client.post(report, json={'status': 'recording'}, headers=office).status_code == 409
    send(client, sid, faculty)
    assert client.post(report, json={'status': 'recording'}, headers=office).status_code == 409
    acknowledge(client, sid, office)
    assert client.post(report, json={'status': 'recording'}, headers=office).status_code == 200
    seal = f'/api/recordings/{sid}/segments/0/seal'
    assert client.post(seal, json={'screen_chunks': 2, 'camera_chunks': 1, 'action': 'pause'}, headers=faculty).status_code == 409
    assert client.post(seal, json={'screen_chunks': 1, 'camera_chunks': 1, 'action': 'pause'}, headers=faculty).status_code == 200
    assert client.post(f'/api/recordings/{sid}/resume', json={}, headers=faculty).status_code == 409
    assert client.post(report, json={'status': 'paused'}, headers=office).status_code == 200
    resumed = client.post(f'/api/recordings/{sid}/resume', json={}, headers=faculty).json()
    assert resumed['segment'] == 1 and resumed['status'] == 'awaiting_media'
    send(client, sid, faculty, 1)
    assert client.post(f'/api/recordings/{sid}/segments/1/seal',
                       json={'screen_chunks': 1, 'camera_chunks': 1, 'action': 'stop'}, headers=faculty).status_code == 200
    assert client.post(report, json={'status': 'processing'}, headers=office).status_code == 409
    acknowledge(client, sid, office, 1)
    assert client.post(report, json={'status': 'processing'}, headers=office).status_code == 200
    assert client.post(report, json={'status': 'completed'}, headers=office).status_code == 422


def test_media_retry_order_and_recorder_isolation(client):
    sid, faculty, office = setup(client)
    route = f'/api/recordings/{sid}/chunks/0/screen/'
    assert client.post(route + '1', content=b'out-of-order', headers=faculty).status_code == 409
    assert client.post(route + '0', content=b'first', headers=faculty).status_code == 200
    assert client.post(route + '0', content=b'first', headers=faculty).status_code == 200
    assert client.post(route + '0', content=b'changed', headers=faculty).status_code == 409
    csrf = login(client, 'admin')
    second = client.post('/api/admin/recorders', json={'name': 'Other Mac'}, headers={'X-CSRF-Token': csrf}).json()
    wrong = {'Authorization': 'Bearer ' + second['token']}
    assert client.get(f'/api/office/recordings/{sid}/chunks/0/screen/0', headers=wrong).status_code == 403
    assert client.post(f'/api/office/recordings/{sid}/report', json={'status': 'failed'}, headers=wrong).status_code == 403
    with main.database() as con:
        hashes = [r[0] for r in con.execute('SELECT token_hash FROM office_recorders')]
    assert hashlib.sha256(second['token'].encode()).hexdigest() in hashes and second['token'] not in hashes


def test_aborted_session_can_drain_relay_media_after_office_reconnects(client, tmp_path):
    sid, faculty, office = setup(client)
    send(client, sid, faculty)
    client.post(f'/api/recordings/{sid}/abort', json={}, headers=faculty)
    def forward(request):
        response = client.request(request.method, request.url.raw_path.decode(),
                                  content=request.content, headers=dict(request.headers))
        return httpx.Response(response.status_code, content=response.content, headers=response.headers)
    agent = OfficeAgent('http://localhost', office['Authorization'].removeprefix('Bearer '),
                        tmp_path / 'recovery', transport=httpx.MockTransport(forward))
    try:
        agent.step()
        assert (tmp_path / 'recovery' / sid / '0-screen.webm').read_bytes() == b'screen'
        assert not list((main.DATA_DIR / 'recording_spool' / sid).glob('*.bin'))
        assert client.get('/api/office/work', headers=office).json()['session'] is None
        assert client.get('/api/assignments/class-a/recording').json()['status'] == 'failed'
    finally:
        agent.close()


@pytest.mark.skipif(not shutil.which('ffmpeg'), reason='FFmpeg required for media integration')
def test_office_agent_saves_real_mp4_and_excludes_breaks(client, tmp_path):
    sid, faculty, office = setup(client)
    screen, camera = tmp_path / 'screen.webm', tmp_path / 'camera.webm'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=white:s=640x360:r=30',
                    '-t', '0.5', '-c:v', 'libvpx', '-deadline', 'realtime', '-live', '1', str(screen)], check=True)
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=blue:s=320x180:r=30',
                    '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000', '-t', '0.5',
                    '-c:v', 'libvpx', '-deadline', 'realtime', '-c:a', 'libopus', str(camera)], check=True)

    def forward(request):
        response = client.request(request.method, request.url.raw_path.decode(),
                                  content=request.content, headers=dict(request.headers))
        return httpx.Response(response.status_code, content=response.content, headers=response.headers)

    agent = OfficeAgent('http://localhost', office['Authorization'].removeprefix('Bearer '),
                        tmp_path / 'office', transport=httpx.MockTransport(forward))
    try:
        for number in (0, 1):
            # Split each real WebM file across two upload requests, as MediaRecorder does.
            for track, source in [('screen', screen), ('camera', camera)]:
                blob = source.read_bytes()
                midpoint = len(blob) // 2
                for sequence, chunk in enumerate((blob[:midpoint], blob[midpoint:])):
                    response = client.post(f'/api/recordings/{sid}/chunks/{number}/{track}/{sequence}',
                                           content=chunk, headers=faculty)
                    assert response.status_code == 200, response.text
            agent.step()
            assert client.get('/api/assignments/class-a/recording').json()['status'] == 'recording'
            action = 'pause' if number == 0 else 'stop'
            client.post(f'/api/recordings/{sid}/segments/{number}/seal',
                        json={'screen_chunks': 2, 'camera_chunks': 2, 'action': action}, headers=faculty)
            agent.step()
            if number == 0:
                assert client.get('/api/assignments/class-a/recording').json()['status'] == 'paused'
                assert client.post(f'/api/recordings/{sid}/resume', json={}, headers=faculty).status_code == 200
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            agent.step()
            result = client.get('/api/assignments/class-a/recording').json()
            if result['status'] in ('completed', 'failed'):
                break
            time.sleep(0.05)
        assert result['status'] == 'completed', result
        assert 0.9 <= result['output_duration'] <= 1.4  # Two half-second segments; no break interval.
        assert result['output_bytes'] > 0
        assert (tmp_path / 'office' / sid / result['output_name']).is_file()
        assert not list((main.DATA_DIR / 'recording_spool' / sid).glob('*.bin'))
    finally:
        agent.close()


def test_office_agent_accepts_lan_http_but_not_public_http(tmp_path):
    for server in ('http://192.168.1.20:8792', 'http://172.16.4.9:8792', 'http://10.0.0.5:8792',
                   'http://faculty-mac.local:8792', 'http://127.0.0.1:8792', 'https://app.example.com'):
        OfficeAgent(server, 'token', tmp_path / 'ok').close()
    for server in ('http://app.example.com', 'http://8.8.8.8:8792', 'ftp://192.168.1.20'):
        with pytest.raises(ValueError):
            OfficeAgent(server, 'token', tmp_path / 'bad')


def test_removed_recorder_is_rejected_and_recordings_use_the_other_laptop(client):
    csrf = login(client, 'admin')
    admin = {'X-CSRF-Token': csrf}
    tokens = [client.post('/api/admin/recorders', json={'name': name}, headers=admin).json() for name in ('This Mac', 'Other laptop')]
    this_mac, other = [{'Authorization': 'Bearer ' + item['token']} for item in tokens]
    for office in (other, this_mac):  # this Mac has the most recent heartbeat
        assert client.post('/api/office/heartbeat', headers=office).status_code == 200
    assert client.delete('/api/admin/recorders/' + tokens[0]['id']).status_code == 403  # CSRF required
    assert client.delete('/api/admin/recorders/' + tokens[0]['id'], headers=admin).status_code == 200
    assert client.delete('/api/admin/recorders/' + tokens[0]['id'], headers=admin).status_code == 404
    assert client.post('/api/office/heartbeat', headers=this_mac).status_code == 401
    assert [r['name'] for r in client.get('/api/admin/recorders').json()] == ['Other laptop']
    faculty = {'X-CSRF-Token': login(client, 'alpha')}
    started = client.post('/api/assignments/class-a/recording/start', json={}, headers=faculty)
    assert started.status_code == 201, started.text
    assert client.get('/api/office/work', headers=other).json()['session']['id'] == started.json()['id']
    # A recorder that is saving a class cannot be removed mid-recording.
    admin = {'X-CSRF-Token': login(client, 'admin')}
    assert client.delete('/api/admin/recorders/' + tokens[1]['id'], headers=admin).status_code == 409


def test_office_desktop_recorder_captures_several_classes_under_faculty_control(client, monkeypatch):
    monkeypatch.setenv('ZOOM_MEETING_SDK_CLIENT_ID', 'sdk-id')
    monkeypatch.setenv('ZOOM_MEETING_SDK_CLIENT_SECRET', 'sdk-secret')
    with main.database() as con:
        con.execute("INSERT INTO assignments(id,webinar_id,faculty_user_id,course,category,subject,topic) VALUES('class-b','22345678901','beta','Course','Live','Subject','Second')")
    admin = {'X-CSRF-Token': login(client, 'admin')}
    office = {'Authorization': 'Bearer ' + client.post('/api/admin/recorders', json={'name': 'Desktop'}, headers=admin).json()['token']}
    alpha = {'X-CSRF-Token': login(client, 'alpha')}
    # A relay-only recorder cannot serve an office capture, and nothing is offered before the desktop app says hello.
    assert client.post('/api/office/heartbeat', headers=office).status_code == 200
    assert client.get('/api/assignments/class-a/recording').json()['office_capture'] is False
    assert client.post('/api/assignments/class-a/recording/start', json={'mode': 'office'}, headers=alpha).status_code == 503
    assert client.post('/api/office/hello', json={'capacity': 2}, headers=office).status_code == 200
    assert client.get('/api/assignments/class-a/recording').json()['office_capture'] is True

    first = client.post('/api/assignments/class-a/recording/start', json={'mode': 'office'}, headers=alpha).json()
    assert (first['mode'], first['status']) == ('office', 'starting')
    beta = {'X-CSRF-Token': login(client, 'beta')}
    second = client.post('/api/assignments/class-b/recording/start', json={'mode': 'office'}, headers=beta).json()
    work = client.get('/api/office/captures', headers=office).json()['sessions']
    assert [item['id'] for item in work] == [first['id'], second['id']] and work[1]['topic'] == 'Second'
    # Both slots are used: no third class is offered this recorder.
    assert client.get('/api/assignments/class-b/recording').json()['office_capture'] is False

    join = client.get(f"/api/office/recordings/{first['id']}/join", headers=office).json()
    assert join['meetingNumber'] == '12345678901' and 'zak' not in join
    claims = json.loads(base64.urlsafe_b64decode(join['signature'].split('.')[1] + '=='))
    assert claims['role'] == 0 and claims['mn'] == '12345678901'

    sid, report = second['id'], lambda body: client.post(f"/api/office/recordings/{second['id']}/report", json=body, headers=office)
    control = lambda action: client.post(f'/api/recordings/{sid}/{action}', headers=beta)
    assert control('pause').status_code == 409  # not confirmed as recording yet
    assert report({'status': 'recording'}).status_code == 200
    assert control('abort').json()['status'] == 'recording'  # closing the faculty tab does not stop it
    assert control('pause').json()['status'] == 'pausing'
    assert report({'status': 'recording'}).status_code == 409
    assert report({'status': 'paused'}).status_code == 200
    assert control('resume').json()['status'] == 'resuming'
    assert report({'status': 'recording'}).status_code == 200
    assert control('stop').json()['status'] == 'stopping'
    assert report({'status': 'processing'}).status_code == 200
    assert report({'status': 'completed'}).status_code == 422
    assert report({'status': 'completed', 'output_name': 'DocTutorials-b.mp4', 'output_bytes': 10, 'output_duration': 2.5}).status_code == 200
    state = client.get('/api/assignments/class-b/recording').json()
    assert (state['status'], state['output_name'], state['office_capture']) == ('completed', 'DocTutorials-b.mp4', True)
    # Another recorder's token cannot read join details for this recorder's class.
    other = {'Authorization': 'Bearer ' + client.post('/api/admin/recorders', json={'name': 'Other'},
                                                     headers={'X-CSRF-Token': login(client, 'admin')}).json()['token']}
    assert client.get(f"/api/office/recordings/{first['id']}/join", headers=other).status_code == 403
