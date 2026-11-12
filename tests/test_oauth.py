"""Legacy URLs must never prompt faculty to sign in to Zoom."""
import pytest
from fastapi.testclient import TestClient
from app import main


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(main, 'DB_PATH', tmp_path / 'app.db')
    monkeypatch.setenv('SESSION_SECRET', 'a' * 68)
    monkeypatch.setenv('ZOOM_MEETING_SDK_CLIENT_ID', 'sdk-client')
    monkeypatch.setenv('ZOOM_MEETING_SDK_CLIENT_SECRET', 'sdk-secret')
    monkeypatch.setenv('ZOOM_OAUTH_REDIRECT_URI', 'https://test.trycloudflare.com/auth/zoom/callback')
    with TestClient(main.app) as c:
        with main.database() as con:
            for name, role in [('faculty', 'faculty'), ('admin', 'admin')]:
                con.execute('INSERT INTO users(id,username,display_name,email,zoom_user_id,password_hash,role) '
                            'VALUES(?,?,?,?,?,?,?)', (name, name, name, name + '@dt.test', name + '@dt.test',
                                                     main.PH.hash('test-password-123'), role))
        yield c


def login(c, user):
    assert c.post('/api/login', json={'username': user, 'password': 'test-password-123'}).status_code == 200


@pytest.mark.parametrize('path', ['/auth/zoom/start', '/auth/zoom/callback?code=unused&state=unused'])
def test_legacy_urls_return_to_app_without_zoom_authorization(client, path):
    assert client.get(path, follow_redirects=False).status_code == 401
    login(client, 'faculty')
    response = client.get(path, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers['location'] == '/'
    with main.database() as con:
        assert con.execute('SELECT COUNT(*) FROM zoom_oauth_states').fetchone()[0] == 0
        assert con.execute('SELECT COUNT(*) FROM zoom_oauth_links').fetchone()[0] == 0


def test_redirect_url_is_optional_and_only_visible_to_admin(client, monkeypatch):
    assert client.get('/api/admin/zoom-setup').status_code == 401
    login(client, 'faculty')
    assert client.get('/api/admin/zoom-setup').status_code == 403
    client.cookies.clear()
    login(client, 'admin')
    setup = client.get('/api/admin/zoom-setup').json()
    assert setup['faculty_zoom_login_required'] is False
    assert setup['redirect_uri'] == 'https://test.trycloudflare.com/auth/zoom/callback'
    assert 'sdk-secret' not in str(setup) and 'sdk-client' not in str(setup)
    monkeypatch.delenv('ZOOM_OAUTH_REDIRECT_URI')
    assert client.get('/api/admin/zoom-setup').json()['redirect_uri'] == ''


def test_server_auth_configuration_needs_no_faculty_oauth_link(client, monkeypatch):
    for name in ('ZOOM_ACCOUNT_ID', 'ZOOM_S2S_CLIENT_ID', 'ZOOM_S2S_CLIENT_SECRET'):
        monkeypatch.setenv(name, 'configured')
    monkeypatch.delenv('ZOOM_OAUTH_REDIRECT_URI')
    login(client, 'faculty')
    status = client.get('/api/zoom/status').json()
    assert status['auth_mode'] == 'server'
    assert status['faculty_zoom_login_required'] is False
    assert status['configured'] is True
    assert status['connected'] is False  # Configuration is not a live connection.
