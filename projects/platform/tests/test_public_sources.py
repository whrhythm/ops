import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from control_plane import public_sources as sources
from control_plane.agent_tasks import seal
from control_plane.assistant_operations import execute
from control_plane.models import AIAuthoringCatalog, AIProjectBinding, now
from control_plane.settings import get_settings
from test_assistant_operations import scenario
from test_project_versions import versions


@pytest.mark.parametrize('url', ['http://example.org/data', 'https://u:p@example.org/data',
    'https://example.org:8443/data', 'https://example.org/data?api_key=secret', 'https://example.org/a#b'])
def test_source_url_boundaries(url):
    with pytest.raises(ValueError): sources.public_url(url)


def test_receipt_is_bound_to_project_user_and_expiry():
    settings = SimpleNamespace(librechat_service_token='s'*40)
    result = sources.inspection({'ok': True, 'url': 'https://example.org/data', 'sha256': 'a'*64,
        'fetched_at': '2026-11-10T00:00:00Z', 'text': '{"items":[]}'}, settings, 'owner', 'project')
    token = result['source_receipt']
    assert sources.verify_receipt(token, settings, 'owner', 'project')['format'] == 'json'
    for owner, project in [('other', 'project'), ('owner', 'other')]:
        with pytest.raises(ValueError): sources.verify_receipt(token, settings, owner, project)


def test_network_adapter_observes_current_data_and_rejects_schema_changes(monkeypatch):
    adapter = {'url': 'https://example.org/data', 'records_path': ['items'], 'fields': {'score': ['score']}}
    async def read(settings, url):
        return {'ok': True, 'url': url, 'fetched_at': 'now', 'text': '{"items":[{"score":9}]}' }
    monkeypatch.setattr(sources, 'read_source', read)
    result = json.loads(asyncio.run(sources.execute_network(None, {'adapter': adapter}, {})))
    assert result['records'] == [{'score': 9}] and result['source'] == adapter['url']
    with pytest.raises(ValueError): asyncio.run(sources.execute_network(None, {'adapter': adapter}, {'url':'https://other.org'}))
    with pytest.raises(ValueError): sources.project(sources.Adapter(**adapter), '{"items":[{"points":1}]}')
    assert sources.project(sources.Adapter(**adapter), '{"items":[]}')['records'] == []
    async def unavailable(settings, url): return {'ok':False, 'code':'source_unavailable'}
    monkeypatch.setattr(sources, 'read_source', unavailable)
    with pytest.raises(ValueError): asyncio.run(sources.execute_network(None, {'adapter': adapter}, {}))


def test_public_transport_blocks_private_dns_before_connection(monkeypatch):
    async def resolve(*args, **kwargs): return [(2, 1, 6, '', ('127.0.0.1', 443))]
    async def run():
        monkeypatch.setattr(asyncio.get_running_loop(), 'getaddrinfo', resolve)
        with pytest.raises(ValueError, match='address_not_public'):
            await sources.fetch_public('https://example.org/data')
    asyncio.run(run())


def test_source_gateway_auth_and_response_cap(monkeypatch):
    from control_plane.mcp_gateway import app
    monkeypatch.setenv('PLATFORM_MCP_GATEWAY_TOKEN', 't'*40)
    with TestClient(app) as client:
        assert client.post('/public-read', json={'url':'https://example.org'}).status_code == 403
        assert client.post('/public-read', headers={'Authorization':'Bearer '+'t'*40}, content='x'*2049).status_code == 413


def test_native_catalog_scope_and_expiry(scenario):
    client, auth, path, initial = scenario
    body = {'native_owner_id':'a'*24, 'actions':[{'reference':f'action:owned{i}:'+'a'*64,'name':f'owned{i}'} for i in range(24)],
            'skills':[{'reference':'b'*24,'name':'skill'}], 'calculator':True, 'mcp':False}
    assert client.post(path+'/authoring-catalog', headers={**auth,'X-CodeChat-Subject':'other'}, json=body).status_code == 404
    assert client.post(path+'/authoring-catalog', headers=auth, json=body).status_code == 200
    with client.session_factory() as db:
        binding = db.get(AIProjectBinding, path.split('/')[-1])
        result, _, _ = execute(db, binding.environment_id, get_settings(), binding.project_id, initial['digest'], 'list_capabilities', '{}')
        assert {x['reference'] for x in result['tools']} == {'calculator', *(item['reference'] for item in body['actions'])}
        assert result['skills'][0]['reference'] == body['skills'][0]['reference']
        row = db.get(AIAuthoringCatalog, binding.project_id)
        row.updated_at = now()-timedelta(minutes=6)
        db.commit()
        result, _, _ = execute(db, binding.environment_id, get_settings(), binding.project_id, initial['digest'], 'list_capabilities', '{}')
        assert result['catalog_stale'] and not result['skills'] and not result['tools']


def test_network_tool_review_trial_and_live_source_failure(versions, harness, monkeypatch):
    from hashlib import sha256
    from control_plane import generated_tools
    from control_plane.models import AgentTask, UserEnvironment
    from test_ai_releases import prepared
    from test_ai_projects import OWNER, PREFIX
    monkeypatch.setenv('PLATFORM_AGENT_TASKS_ENABLED', 'true')
    monkeypatch.setenv('PLATFORM_NETWORK_TOOLS_ENABLED', 'true')
    client, auth, path, previous, config = prepared(versions, monkeypatch)
    project_id = path.rsplit('/', 1)[-1]
    owner = {'native_owner_id': OWNER}
    adapter = {'url':'https://example.org/events','records_path':['events'], 'fields':{'name':['name'], 'score':['score']}}
    raw = json.dumps(adapter).encode()
    observed = {'ok':True,'url':adapter['url'],'text':'{"events":[{"name":"sample","score":8}]}',
                'sha256':'a'*64, 'fetched_at':'2026-11-10T00:00:00Z'}
    receipt = sources.inspection(observed, get_settings(), versions[2], project_id)['source_receipt']
    build = generated_tools.Build(engine='public-json-v1',source_receipt=receipt,name='events',title='Events',requirements='Read events')
    task_id = str(uuid4())
    with client.session_factory.begin() as db:
        db.add(AgentTask(id=task_id, environment_id=versions[2], conversation_id='tool_'+build.request_id.hex,
            request_key='network-review-test',config={'project_path':'test/tools'},status='completed',encrypted_prompt='unused',
            encrypted_events=seal([{'kind':'file','operation':'write','path':'tool.json','source_digest':sha256(raw).hexdigest()}],get_settings()),
            resources={'tool_project':project_id,'tool_build':seal(build.model_dump(mode='json'),get_settings())}))
    monkeypatch.setattr(generated_tools, 'call_files', lambda *a, **k: SimpleNamespace(content=raw))
    async def read(settings, url): return observed
    monkeypatch.setattr(sources,'read_source',read)
    review_path = path+'/generated-tools/'+task_id
    response = client.post(review_path,headers=auth,json=owner)
    assert response.status_code == 200, response.text
    reference = response.json()['review']['reference']
    body = {**owner,'expected_digest':previous['expected_digest'],'config':{**config,'tool_reference':reference}}
    assert client.post(path+'/draft',headers=auth,json=body).status_code == 409
    from control_plane.ai_tool_reviews import review_id
    _, token = harness
    admin={'Authorization':'Bearer '+token('operator', groups=['platform-admins'])}
    assert client.post('/v1/admin/assistant-tool-reviews/'+review_id(project_id, reference),headers=admin,
        json={'revision':1,'state':'approved','acknowledged':True}).status_code == 200
    saved=client.post(path+'/draft',headers=auth,json=body).json()
    trial=client.post(path+'/trials',headers=auth,json={**owner,'request_id':str(uuid4()),'expected_digest':saved['digest']}).json()
    target=path+'/trials/'+trial['id']
    assert client.post(target+'/confirm',headers=auth,json=owner).status_code == 200
    run={**owner,'conversation_id':str(uuid4()),'message_id':str(uuid4())}
    assert client.post(target+'/resolve',headers=auth,json=run).status_code == 200
    invocation=PREFIX+'/trials/'+trial['id']+'/tool'
    response=client.post(invocation,headers=auth,json={**run,'arguments':{}})
    assert response.status_code == 200,response.text
    assert json.loads(response.json()['text'])['records'][0]['score'] == 8
    observed['text']='{"events":[{"name":"sample","score":10}]}'
    response=client.post(invocation,headers=auth,json={**run,'arguments':{}})
    assert json.loads(response.json()['text'])['records'][0]['score'] == 10
    observed['ok']=False
    assert client.post(invocation,headers=auth,json={**run,'arguments':{}}).status_code == 503


def test_expired_and_tampered_receipt_rejected():
    import jwt
    settings=SimpleNamespace(librechat_service_token='s'*40)
    claims={'aud':'source-probe','sub':'owner','project':'project','url':'https://example.org/a','format':'json','exp':1}
    token=jwt.encode(claims,settings.librechat_service_token,algorithm='HS256')
    with pytest.raises(ValueError): sources.verify_receipt(token,settings,'owner','project')
    with pytest.raises(ValueError): sources.verify_receipt(token[:-8]+'tampered',settings,'owner','project')
