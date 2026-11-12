import json
import re
import stat

from fastapi.testclient import TestClient

from office_recorder import __main__ as office


def test_office_pairing_is_local_csrf_protected_and_token_is_private(tmp_path, monkeypatch):
    class Agent:
        def __init__(self, *args, **kwargs):
            self.info = {'status': 'ready', 'message': 'Ready', 'output': ''}
            self.future = None
        def request(self, *args, **kwargs):
            pass
        def run(self):
            pass
        def close(self):
            pass

    monkeypatch.setattr(office, 'OfficeAgent', Agent)
    monkeypatch.setattr(office, 'require_ffmpeg', lambda: None)
    config = tmp_path / 'private' / 'config.json'
    with TestClient(office.create_local_app(config)) as client:
        page = client.get('/')
        assert page.status_code == 200
        secret = re.search(r"'X-Local-CSRF':'([^']+)'", page.text).group(1)
        values = {'server': 'https://example.test', 'token': 'private-pairing-token', 'output_dir': str(tmp_path / 'movies')}
        assert client.post('/pair', json=values).status_code == 403
        assert client.post('/pair', json=values, headers={'X-Local-CSRF': secret, 'Origin': 'https://untrusted.test'}).status_code == 403
        assert client.post('/pair', json=values, headers={'X-Local-CSRF': secret, 'Origin': 'http://127.0.0.1:8804'}).status_code == 200
        assert json.loads(config.read_text())['token'] == values['token']
        assert stat.S_IMODE(config.stat().st_mode) == 0o600
        assert values['token'] not in client.get('/status').text
        assert values['token'] not in client.get('/').text
