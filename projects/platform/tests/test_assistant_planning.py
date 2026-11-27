import asyncio
from datetime import timedelta
import json

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from control_plane.models import AIAuthoringCatalog, AIProjectBinding, AgentTask, now
from control_plane.assistant_authoring import prepare
from control_plane.assistant_operations import execute
from control_plane.project_connector import invoke as invoke_connector, registered
from control_plane.settings import get_settings
from test_assistant_operations import invoke, scenario
from test_project_versions import versions


def test_plan_persists_across_catalog_refresh_and_rejects_stale_or_forged_inputs(scenario, monkeypatch):
    monkeypatch.setenv('PLATFORM_ASSISTANT_PLANNING_ENABLED', 'true')
    get_settings.cache_clear()
    client, auth, path, initial = scenario
    catalog = {'native_owner_id': 'a'*24, 'calculator': True}
    assert client.post(path+'/authoring-catalog', headers=auth, json=catalog).status_code == 200
    reuse = {'requirement': '计算换算', 'method': 'reuse', 'reference': 'calculator',
             'rationale': '本地计算', 'prerequisite': '验证换算示例', 'access_scope': '仅数字'}
    gap = {'requirement': '查询最新水位', 'method': 'tool', 'rationale': '需要实时观测值',
           'prerequisite': '确认开放数据和刷新频率', 'access_scope': '只读获准公开数据'}
    args = {'expected_revision': 0, 'goal': '水位监测', 'needs': [reuse, gap]}
    forged = {**args, 'needs': [{**reuse, 'reference': 'private'}]}
    assert invoke(scenario, 'save_capability_plan', forged)[0]['code'] == 'capability_unavailable'
    no_path = {**args, 'needs': [{**gap, 'method': 'clarify'}]}
    assert invoke(scenario, 'save_capability_plan', no_path)[0]['code'] == 'invalid_arguments'
    result, digest, reply = invoke(scenario, 'save_capability_plan', args)
    assert result['plan']['revision'] == 1 and digest == initial['digest']
    assert '复用候选' in reply and '开发工具' in reply
    assert invoke(scenario, 'save_capability_plan', args)[0]['code'] == 'plan_changed'
    assert invoke(scenario, 'propose_tool_build', {})[0]['code'] == 'unknown_operation'
    assert client.post(path+'/authoring-catalog', headers=auth,
                       json={**catalog, 'calculator': False}).status_code == 200
    assert client.post(path+'/authoring-catalog', headers={**auth, 'X-CodeChat-Subject': 'other'},
                       json=catalog).status_code == 404
    result = invoke(scenario, 'list_capabilities', {})[0]
    assert result['plan']['goal'] == '水位监测' and not result['directory']
    assert invoke(scenario, 'save_capability_plan', {**args, 'expected_revision': 1})[0]['code'] == 'capability_unavailable'
    with client.session_factory() as db:
        binding = db.get(AIProjectBinding, path.split('/')[-1])
        with pytest.raises(HTTPException) as error:
            execute(db, 'other-environment', get_settings(), binding.project_id, digest,
                    'list_capabilities', '{}')
        assert error.value.status_code == 404
        payload, _ = prepare(db, binding, get_settings(), {
            'model': 'test-model', 'messages': [{'role': 'user', 'content': '补充要求'}],
        })
        assert {tool['function']['name'] for tool in payload['tools']} == {'list_capabilities', 'save_capability_plan'}
        assert '水位监测' in payload['messages'][0]['content']
        assert not list(db.scalars(select(AgentTask)))
        db.get(AIAuthoringCatalog, binding.project_id).updated_at = now() - timedelta(minutes=6)
        db.commit()
    result = invoke(scenario, 'list_capabilities', {})[0]
    assert result['catalog_stale'] and result['plan']['revision'] == 1
    assert client.get(path+'/trials', headers=auth).json()['digest'] == initial['digest']


def test_registered_connector_is_project_scoped(scenario, monkeypatch):
    client, _, path, _ = scenario
    project = path.split('/')[-1]
    config = {project: {'name': '项目工具', 'description': '查询公开资料',
                       'endpoint': 'http://project-tool.platform-system.svc.cluster.local:8080/research'}}
    monkeypatch.setenv('PLATFORM_ASSISTANT_PLANNING_ENABLED', 'true')
    monkeypatch.setenv('PLATFORM_ASSISTANT_PROJECT_CONNECTORS', json.dumps(config))
    get_settings.cache_clear()
    settings = get_settings()
    # Operator-registered URLs cannot bypass project ownership and tool approval.
    assert registered(settings, project) is None
    assert registered(settings, 'another-project') is None
    with client.session_factory() as db:
        with pytest.raises(HTTPException) as error:
            asyncio.run(invoke_connector(db, settings, 'foreign-owner', project, '{"query":"public"}'))
        assert error.value.status_code == 404
    config[project]['endpoint'] = 'http://127.0.0.1:8080/research'
    monkeypatch.setenv('PLATFORM_ASSISTANT_PROJECT_CONNECTORS', json.dumps(config))
    get_settings.cache_clear()
    assert registered(get_settings(), project) is None


def test_connector_schema_matches_approved_protocol():
    from types import SimpleNamespace
    from control_plane.project_connector import arguments
    from control_plane.assistant_operations import configure
    legacy = SimpleNamespace(descriptor={'generated': {'program': {}}})
    split = SimpleNamespace(descriptor={'generated': {'program': {'protocol': 2}}})
    assert arguments(legacy, '{"query":"manual"}') == {'query': 'manual', 'recent_days': 30}
    assert arguments(split, '{"operation":"search","query":"manual"}')['operation'] == 'search'
    assert arguments(split, '{"operation":"read","url":"https://example.org/doc"}')['url'] == 'https://example.org/doc'
    with pytest.raises(ValueError):
        arguments(split, '{"query":"manual","recent_days":30}')
    for protocol in (1, 2):
        configured = configure({'messages': [{'role': 'user', 'content': 'help'}]},
            {'planning': {'project_connector': True, 'project_connector_protocol': protocol}})
        schema = next(tool['function']['parameters'] for tool in configured['tools']
                      if tool['function']['name'] == 'query_project_connector')
        assert ('operation' in schema['properties']) == (protocol == 2)
