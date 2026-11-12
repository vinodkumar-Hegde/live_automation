import asyncio

import httpx
import pytest

from app import main


@pytest.mark.parametrize('identity,profile,webinar,allowed', [
    ('faculty@dt.test', {'id': 'Zoom-ID', 'email': 'faculty@dt.test'},
     {'host_id': 'Zoom-ID'}, True),
    ('Zoom-ID', {'id': 'Zoom-ID', 'email': 'FACULTY@dt.test'},
     {'host_id': 'another-id', 'settings': {'alternative_hosts': 'other@dt.test; faculty@dt.test'}}, True),
    ('wrong-id', {'id': 'wrong-id', 'email': 'other@dt.test'},
     {'host_email': 'faculty@dt.test'}, False),
    ('Zoom-ID', {'id': 'Zoom-ID', 'email': 'faculty@dt.test'},
     {'host_id': 'zoom-id'}, False),
    ('faculty@dt.test', {'email': 'faculty@dt.test'},
     {'host_email': 'faculty@dt.test'}, False),
    ('faculty@dt.test', {'id': 'Zoom-ID', 'email': 'faculty@dt.test'},
     {'host_id': 'different-id', 'settings': None}, False),
    ('faculty@dt.test', {'id': 'Zoom-ID', 'email': 'faculty@dt.test', 'account_id': 'external-account'},
     {'host_id': 'Zoom-ID'}, False),
    ('company-host@dt.test', {'id': 'Company-ID', 'email': 'company-host@dt.test'},
     {'host_id': 'Company-ID'}, True),
])
def test_webinar_host_requires_resolved_faculty_identity(monkeypatch, identity, profile, webinar, allowed):
    calls = []
    monkeypatch.setenv('ZOOM_ACCOUNT_ID', 'managed-account')
    profile = {'account_id': 'managed-account', **profile}

    def respond(request):
        calls.append(request.url.path)
        return httpx.Response(200, json=webinar if '/webinars/' in request.url.path else profile)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(main.httpx, 'AsyncClient',
                        lambda **kwargs: real_client(transport=httpx.MockTransport(respond), **kwargs))
    faculty = {'zoom_user_id': identity, 'email': 'faculty@dt.test'}
    if allowed:
        asyncio.run(main.verify_webinar_host('12345678901', faculty, 'test-access'))
    else:
        with pytest.raises(main.ZoomError):
            asyncio.run(main.verify_webinar_host('12345678901', faculty, 'test-access'))
    assert len(calls) == 2
