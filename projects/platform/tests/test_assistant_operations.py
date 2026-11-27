import asyncio
import json
from uuid import uuid4

import pytest
import httpx
from fastapi import HTTPException
from sqlalchemy import select

from control_plane.assistant_authoring import prepare
from control_plane.assistant_operations import execute, relay
from control_plane.models import AIProjectBinding, AIProjectDraft, AgentTask, AuditEvent, UserEnvironment
from control_plane.project_routing import resolve_project
from control_plane.settings import get_settings
from test_ai_trials import initialized
from test_project_versions import versions


@pytest.fixture
def scenario(versions, monkeypatch):
    client, auth, path, config, initial = initialized(versions, monkeypatch)
    monkeypatch.setenv('PLATFORM_ASSISTANT_OPERATIONS_ENABLED', 'true')
    get_settings.cache_clear()
    return client, auth, path, initial


def invoke(scenario, name, arguments, digest=None):
    client, _, path, initial = scenario
    with client.session_factory() as db:
        binding = db.get(AIProjectBinding, path.split('/')[-1])
        return execute(db, binding.environment_id, get_settings(), binding.project_id,
                       digest or initial['digest'], name, json.dumps(arguments))


def test_typed_save_survives_without_browser_and_old_trial_unchanged(scenario):
    client, auth, path, initial = scenario
    trial = client.post(path+'/trials', headers=auth, json={
        'native_owner_id': 'a'*24, 'request_id': str(uuid4()), 'expected_digest': initial['digest'],
    }).json()
    instructions = '查询实时赛事，缺少真实来源时明确说明无法确认比分。'
    result, digest, receipt = invoke(scenario, 'update_agent_draft', {'instructions': instructions})
    assert result['saved'] and digest != initial['digest'] and 'assistant-saved' in receipt
    current = client.get(path+'/trials', headers=auth).json()
    assert current['config']['instructions'] == instructions
    assert current['revision'] == initial['revision'] + 1
    assert current['trials'][0]['digest'] == trial['digest']
    stale, _, _ = invoke(scenario, 'update_agent_draft', {'instructions': 'stale'})
    assert stale['code'] == 'draft_changed'
    with client.session_factory() as db:
        assert not list(db.scalars(select(AgentTask)))
        assert len(list(db.scalars(select(AuditEvent).where(
            AuditEvent.action == 'ai_trial.draft_updated')))) == 1


@pytest.mark.parametrize('name,arguments,code', [
    ('develop_project', {'instruction': 'write files'}, 'unknown_operation'),
    ('update_agent_draft', {'instructions': 'x', 'tool_reference': 'evil'}, 'invalid_arguments'),
    ('update_agent_draft', {'instructions': ''}, 'invalid_arguments'),
    ('propose_tool_binding', {'instructions': 'x', 'reference': 'missing'}, 'tool_unavailable'),
    ('propose_tool_build', {'instructions': 'x', 'specification': {'url': 'http://private'}}, 'invalid_arguments'),
])
def test_invalid_or_unauthorized_operations_do_not_mutate(scenario, name, arguments, code):
    result, _, text = invoke(scenario, name, arguments)
    assert result['code'] == code and not text
    client, auth, path, initial = scenario
    assert client.get(path+'/trials', headers=auth).json()['digest'] == initial['digest']


def test_configuration_reads_current_truth_and_replaces_client_tool_list(scenario):
    client, auth, path, initial = scenario
    with client.session_factory() as db:
        binding = db.get(AIProjectBinding, path.split('/')[-1])
        payload, digest = prepare(db, binding, get_settings(), {
            'model': 'test-model', 'messages': [{'role': 'user', 'content': 'NBA 比分'}],
            'tools': [{'type': 'function', 'function': {'name': 'develop_project'}}],
        })
        names = {tool['function']['name'] for tool in payload['tools']}
        assert 'update_agent_draft' in names and 'develop_project' not in names
        assert digest == initial['digest']
        assert 'assistant-draft Markdown' not in payload['messages'][0]['content']
    result, _, _ = invoke(scenario, 'list_capabilities', {})
    assert result['development_types'] == ['integer-rules-v1']
    assert result['network_development_available'] is False


def test_user_confirmed_build_is_proposal_not_execution(scenario):
    result, _, text = invoke(scenario, 'propose_tool_build', {
        'instructions': '计算箱数费用。',
        'specification': {'request_id': str(uuid4()), 'name': 'boxes', 'title': '箱数费用',
            'requirements': '每箱 100 分', 'parameters': ['boxes'],
            'cases': [{'inputs': {'boxes': 1}, 'result': 100}, {'inputs': {'boxes': 2}, 'result': 200}]},
    })
    assert result['awaiting_owner_confirmation'] and not result['saved']
    assert 'assistant-tool-build' in text
    with scenario[0].session_factory() as db:
        assert not list(db.scalars(select(AgentTask)))


def test_persisted_project_route_and_unavailable_binding_never_fall_back(scenario):
    client, _, path, _ = scenario
    with client.session_factory() as db:
        binding = db.get(AIProjectBinding, path.split('/')[-1])
        assert resolve_project(db, binding.environment_id, binding.project_id)[0] == 'assistant'
        with pytest.raises(HTTPException):
            resolve_project(db, 'different-owner', binding.project_id)
        for state in ('preparing', 'deleting', 'deleted'):
            binding.state = state
            with pytest.raises(HTTPException):
                resolve_project(db, binding.environment_id, binding.project_id)


class Upstream:
    def __init__(self, arguments, finish='tool_calls'):
        self.arguments, self.finish = arguments, finish

    async def aiter_lines(self):
        yield 'data: ' + json.dumps({'choices': [{'delta': {'tool_calls': [{
            'index': 0, 'id': 'call_1', 'function': {'name': 'update_agent_draft',
                'arguments': self.arguments}}]}, 'finish_reason': None}]})
        yield 'data: ' + json.dumps({'choices': [{'delta': {}, 'finish_reason': self.finish}]})
        yield 'data: [DONE]'


def test_last_round_limit_does_not_issue_extra_request_and_save_is_durable(scenario):
    client, _, path, initial = scenario
    async def run():
        with client.session_factory() as db:
            binding = db.get(AIProjectBinding, path.split('/')[-1])
            env = db.get(UserEnvironment, binding.environment_id)
            settings = get_settings().model_copy(update={'assistant_operations_rounds': 1})
            # No HTTP client: the last permitted round must not request another response.
            return ''.join([item async for item in relay(
                Upstream(json.dumps({'instructions': '保存后即使连接关闭仍保留。'})),
                db, env, settings, binding.project_id, initial['digest'],
                {'model': 'test-model', 'messages': []}, None, '', {})])
    output = asyncio.run(run())
    assert 'assistant-saved' in output and '处理次数上限' in output and '[DONE]' in output


def test_truncated_tool_arguments_never_write(scenario):
    client, _, path, initial = scenario
    async def run():
        with client.session_factory() as db:
            binding = db.get(AIProjectBinding, path.split('/')[-1])
            env = db.get(UserEnvironment, binding.environment_id)
            return ''.join([item async for item in relay(
                Upstream('{"instructions":"do not save"}', finish='length'),
                db, env, get_settings(), binding.project_id, initial['digest'],
                {'model': 'test-model', 'messages': []}, None, '', {})])
    assert '未完成' in asyncio.run(run())
    with client.session_factory() as db:
        assert db.get(AIProjectDraft, path.split('/')[-1]).revision == initial['revision']


def test_invalid_arguments_are_corrected_without_replaying_a_save(scenario):
    client, auth, path, initial = scenario
    requests = []
    def provider(request):
        body = json.loads(request.content)
        requests.append(body)
        last = json.loads(body['messages'][-1]['content'])
        if len(requests) == 1:
            assert last['code'] == 'invalid_arguments'
            assert last['issues'] == [{'field': 'instructions', 'reason': 'string_too_short'}]
            event = {'choices': [{'delta': {'tool_calls': [{'index': 0, 'id': 'fixed-call',
                'function': {'name': 'update_agent_draft', 'arguments': '{"instructions":"corrected"}'}}]},
                'finish_reason': None}]}
            stop = {'choices': [{'delta': {}, 'finish_reason': 'tool_calls'}]}
        else:
            assert last['saved'] is True
            event = {'choices': [{'delta': {'content': '已保存。'}, 'finish_reason': None}]}
            stop = {'choices': [{'delta': {}, 'finish_reason': 'stop'}]}
        return httpx.Response(200, text='data: '+json.dumps(event)+'\n\ndata: '+json.dumps(stop)+'\n\n'
            +'data: '+json.dumps({'choices': [], 'usage': {'prompt_tokens': 10, 'completion_tokens': 5,
                                                          'total_tokens': 15}})+'\n\ndata: [DONE]\n\n')
    async def run():
        with client.session_factory() as db:
            binding = db.get(AIProjectBinding, path.split('/')[-1])
            env = db.get(UserEnvironment, binding.environment_id)
            async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as transport:
                return ''.join([item async for item in relay(
                    Upstream('{"instructions":""}'), db, env, get_settings(), binding.project_id,
                    initial['digest'], {'model': 'test-model', 'messages': []},
                    transport, 'http://synthetic/v1/chat/completions', {})])
    output = asyncio.run(run())
    assert len(requests) == 2 and output.count('assistant-saved') == 1
    assert '"total_tokens": 30' in output
    assert client.get(path+'/trials', headers=auth).json()['revision'] == initial['revision'] + 1
