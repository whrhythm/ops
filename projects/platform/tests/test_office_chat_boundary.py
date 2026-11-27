"""Office baseline: chat uses the existing user key and fails closed upstream."""
import certifi
import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from control_plane import programming
from control_plane.settings import Settings


@pytest.mark.parametrize('failure,expected', [(401, 401), (403, 403), (422, 503), (429, 429), (500, 503), ('timeout', 503), ('budget', 402)])
def test_chat_failure_never_starts_development_or_retries(monkeypatch, failure, expected):
    calls = []
    async def space(db, settings, key, client, *, require_runtime, environment=None):
        assert not require_runtime
        assert key == 'sk-synthetic-user'
        return 'unused', 'unused'

    def provider(request):
        calls.append(request)
        assert str(request.url) == 'http://models/v1/chat/completions'
        assert request.headers['Authorization'] == 'Bearer sk-synthetic-user'
        if failure == 'timeout':
            raise httpx.ReadTimeout('sensitive upstream detail', request=request)
        if failure == 'budget':
            return httpx.Response(422, json={'error': {'type': 'budget_exceeded', 'message': 'sensitive upstream detail'}})
        return httpx.Response(failure, json={'error': {'message': 'sensitive upstream detail'}})

    original = httpx.AsyncClient
    monkeypatch.setattr(programming.httpx, 'AsyncClient', lambda **kwargs: original(transport=httpx.MockTransport(provider)))
    monkeypatch.setattr(programming, 'resolve_space', space)
    class Database:
        def get(self, model, identifier):
            return None
        def rollback(self):
            pass
    app = FastAPI()
    app.include_router(programming.router)
    app.dependency_overrides[programming.get_db] = Database
    app.dependency_overrides[programming.get_settings] = lambda: Settings(
        litellm_models='model', litellm_url='http://models', litellm_master_key='unused-master',
        workspace_service_ca=certifi.where())
    response = TestClient(app).post('/agent/v1/chat/completions',
        headers={'Authorization': 'Bearer sk-synthetic-user'},
        json={'model': 'model', 'stream': True, 'messages': [{'role': 'user', 'content': 'hello'}]})
    assert response.status_code == expected
    assert len(calls) == 1
    assert 'sensitive' not in response.text
    assert 'sk-synthetic-user' not in response.text
