import asyncio
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest

from control_plane import project_tool_registration as tools


def test_separate_search_read_arguments_and_legacy_compatibility():
    settings = SimpleNamespace(project_web_calls=9, agent_mcp_calls=8)
    assert tools.call_limit({'program': {'protocol': 2}}, settings) == 9
    assert tools.call_limit({'program': {}}, settings) == 3
    assert tools.WebQuery(operation='search', query='技术文档').period is None
    assert tools.WebQuery(operation='read', url='https://example.org/manual', focus='配置').operation == 'read'
    for value in ({'operation': 'read', 'url': 'http://example.org'},
                  {'operation': 'read', 'url': 'https://example.org', 'query': 'mixed'},
                  {'operation': 'search', 'query': '!engine bypass'},
                  {'operation': 'read', 'url': 'https://example.org', 'offset': -1}):
        with pytest.raises(ValueError):
            tools.WebQuery(**value)
    assert tools.Query(query='legacy').recent_days == 30


def test_split_protocol_keeps_undated_originals_and_search_candidates(monkeypatch):
    async def request(db, settings, snapshot, args, env_id):
        if args['operation'] == 'search':
            return {'operation': 'search', 'candidates': [{'url': 'https://example.org/doc'}]}
        return {'operation': 'read', 'sources': [{'url': 'https://example.org/doc', 'published_on': None, 'excerpt': 'manual'}]}
    monkeypatch.setattr(tools, 'runtime_request', request)
    snapshot = {'program': {'protocol': 2}}
    settings = SimpleNamespace(project_tool_registration_enabled=True)
    search = json.loads(asyncio.run(tools.execute(settings, snapshot, {'operation': 'search', 'query': 'manual'})))
    assert len(search['candidates']) == 1
    read = json.loads(asyncio.run(tools.execute(settings, snapshot, {'operation': 'read', 'url': 'https://example.org/doc'})))
    assert read['sources'][0]['published_on'] is None


@pytest.mark.parametrize('query', ['https://127.0.0.1/admin', 'password=private', '!engine hello', 'hello\nworld'])
def test_public_query_rejected_before_creating_a_pod(query):
    with pytest.raises(ValueError):
        tools.Query(query=query)


def test_project_command_and_version_target_are_not_client_urls():
    message = {'role': 'user', 'content': '提交项目工具 web-search-v2'}
    assert tools.command([message]).groups() == ('提交项目工具', 'web-search-v2')
    assert tools.command([{**message, 'role': 'assistant'}]) is None
    assert tools.command([{**message, 'content': '提交项目工具 ../other'}]) is None



def test_runtime_keeps_only_recent_verified_originals(monkeypatch):
    today = datetime.now(timezone.utc).date()
    snapshot = {'project_id': 'project', 'source_digest': 'a'*64}

    async def request(db, settings, snapshot, query, environment_id):
        assert snapshot['source_digest'] == 'a'*64
        assert query == {'query': '天气', 'recent_days': 3}
        return {'sources': [
            {'url': 'https://example.org/old', 'published_on': (today-timedelta(days=10)).isoformat(), 'excerpt': 'x'*150},
            {'url': 'https://example.org/live', 'published_on': today.isoformat(), 'excerpt': 'y'*150},
            {'url': 'https://example.org/unknown', 'published_on': None, 'excerpt': 'z'*150},
        ]}

    monkeypatch.setattr(tools, 'runtime_request', request)
    settings = SimpleNamespace(project_tool_registration_enabled=True)
    result = json.loads(asyncio.run(tools.execute(settings, snapshot, {'query': '天气', 'recent_days': 3})))
    assert [item['url'] for item in result['sources']] == ['https://example.org/live']
    with pytest.raises(ValueError):
        asyncio.run(tools.execute(settings, snapshot, {'query': '天气', 'recent_days': 0}))


def test_runtime_distinguishes_content_date_and_publication_date(monkeypatch):
    today = datetime.now(timezone.utc).date()

    async def request(*args):
        return {'sources': [
            {'url': 'https://example.org/schedule', 'content_on': today.isoformat(), 'excerpt': 'x' * 150},
            {'url': 'https://example.org/future', 'content_on': (today+timedelta(days=1)).isoformat(), 'excerpt': 'x' * 150},
            {'url': 'https://example.org/old', 'published_on': '2020-01-01', 'content_on': today.isoformat(), 'excerpt': 'x' * 150},
        ]}

    monkeypatch.setattr(tools, 'runtime_request', request)
    settings = SimpleNamespace(project_tool_registration_enabled=True)
    snapshot = {'project_id': 'project', 'source_digest': 'a' * 64}
    result = json.loads(asyncio.run(tools.execute(settings, snapshot, {'query': '今日赛程', 'recent_days': 3})))
    assert result['sources'] == [
        {'url': 'https://example.org/schedule', 'content_on': today.isoformat(), 'excerpt': 'x' * 150},
    ]


def test_runtime_keeps_source_metadata_and_table_rows(monkeypatch):
    now = datetime.now(timezone.utc)
    excerpt = 'Date | Opponent | Points\n' + 'Row | Team | 15\n' * 150

    async def request(*args):
        return {'sources': [{'url': 'https://example.org/results',
                             'content_on': now.date().isoformat(), 'title': 'Results',
                             'read_at': now.isoformat(), 'excerpt': excerpt}]}

    monkeypatch.setattr(tools, 'runtime_request', request)
    result = json.loads(asyncio.run(tools.execute(SimpleNamespace(project_tool_registration_enabled=True),
                                                 {}, {'query': 'results', 'recent_days': 3})))
    source = result['sources'][0]
    assert source['title'] == 'Results'
    assert source['read_at'] == now.isoformat()
    assert source['excerpt'] == excerpt
