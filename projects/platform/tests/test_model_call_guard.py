import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select, func

from control_plane import model_call_guard as guard
from control_plane.models import ModelCallReservation, now
from test_daily_free import owner


def settings(env, limit='.05', window='batch-1', **changes):
    policy = {'id': window, 'usd_limit': limit,
              'starts_at': (now() - timedelta(minutes=1)).isoformat(),
              'ends_at': (now() + timedelta(hours=1)).isoformat(),
              'max_output_tokens': 1024,
              'models': {'model': {'input_ceiling_per_million': '.3',
                                   'output_ceiling_per_million': '1.2', 'context_limit': 1000000}}}
    policy.update(changes)
    return SimpleNamespace(model_call_budgets=json.dumps({env.id: policy}))


def body(**changes):
    return {'model': 'model', 'messages': [{'role': 'user', 'content': '整理文件'}], **changes}


def tools(*outputs, name='execute_code'):
    messages = body()['messages']
    for i, output in enumerate(outputs):
        messages += [{'role': 'assistant', 'tool_calls': [
            {'id': str(i), 'type': 'function', 'function': {'name': name, 'arguments': '{}'}}]},
            {'role': 'tool', 'tool_call_id': str(i), 'content': output}]
    return messages


@pytest.mark.parametrize('failure', [
    'Execution error:\n\ncode_execution_quota_exhausted',
    'Execution error:\n\nCode execution is temporarily rate-limited.',
    'Error: authorization failed', 'Error: execution timed out',
    'Error: office_task_already_submitted', 'Error: result unknown',
])
def test_terminal_tool_error_does_not_reach_another_model(failure):
    with pytest.raises(HTTPException) as exc:
        guard.check_tool_failures(tools(failure))
    assert exc.value.status_code == 400


def test_correction_once_source_prose_and_new_turn():
    failure = 'stdout:\nstderr: Traceback (most recent call last):\nTypeError: tuple != list'
    guard.check_tool_failures(tools(failure))
    with pytest.raises(HTTPException, match='correction_limit'):
        guard.check_tool_failures(tools(failure, failure))
    guard.check_tool_failures(tools('This document discusses quota exceeded and permission denied.'))
    guard.check_tool_failures(tools('Error: rate limit', name='retrieval'))
    guard.check_tool_failures(tools(failure, failure) + [{'role': 'user', 'content': '继续改稿'}])
    with pytest.raises(HTTPException, match='requires_review'):
        guard.check_tool_failures(tools('Error: provider sent no receipt'))


def test_atomic_reservation_settlement_late_duplicate_and_no_wallet_mutations(harness):
    client, _ = harness
    with client.session_factory() as db:
        env = owner(db)
        policy = settings(env, limit='.004')
        forwarded, attempt = guard.reserve(db, env.id, 'secret', body(stream=True), policy)
        assert forwarded['max_tokens'] == 1024 and forwarded['num_retries'] == 0
        assert forwarded['stream_options']['include_usage']
        assert forwarded['fallbacks'] == []
        row = db.get(ModelCallReservation, attempt)
        assert row.state == 'dispatched' and row.reserved_nanos < 4000000
        with pytest.raises(HTTPException, match='budget_exhausted'):
            guard.reserve(db, env.id, 'secret', body(), policy)
        db.rollback()
        assert not guard.settle(db, attempt, {})
        db.rollback()
        assert guard.settle(db, attempt, {'prompt_tokens': 100, 'completion_tokens': 20})
        assert guard.settle(db, attempt, {'prompt_tokens': 100, 'completion_tokens': 20})
        assert db.get(ModelCallReservation, attempt).settled_nanos == 54000
        guard.reserve(db, env.id, 'secret', body(), policy)
        assert db.scalar(select(func.count()).select_from(ModelCallReservation)) == 2
        assert not guard.settle(db, attempt, {'prompt_tokens': 101, 'completion_tokens': 20})
        with pytest.raises(HTTPException, match='requires_review'):
            guard.reserve(db, env.id, 'secret', body(), policy)


def test_concurrent_reservations_cannot_overbook(harness):
    client, _ = harness
    with client.session_factory() as db:
        env = owner(db)
    policy = settings(env, limit='.004')
    def take(_):
        with client.session_factory() as db:
            try:
                return guard.reserve(db, env.id, 'secret', body(), policy)[1]
            except HTTPException:
                db.rollback()
                return None
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(take, range(4)))
    assert sum(bool(r) for r in results) == 1


def test_changed_window_missing_receipt_and_model_bounds(harness):
    client, _ = harness
    with client.session_factory() as db:
        env = owner(db)
        guard.reserve(db, env.id, 'secret', body(), settings(env, '.004'))
        with pytest.raises(HTTPException, match='budget_exhausted'):
            guard.reserve(db, env.id, 'secret', body(), settings(env, '.004', window='new-window'))
        db.rollback()
        with pytest.raises(HTTPException, match='window_closed'):
            guard.reserve(db, env.id, 'secret', body(), settings(env, ends_at=(now()-timedelta(seconds=1)).isoformat()))
        original = body()
        assert guard.reserve(db, env.id, 'secret', original,
                             SimpleNamespace(model_call_budgets='{}')) == (original, None)


@pytest.mark.parametrize('payload', [
    body(model='unknown'), body(n=2), body(max_tokens=0),
    body(tools=[{'type': 'web_search'}]), body(modalities=['audio']),
    body(messages=[{'role': 'user', 'content': [{'type': 'image_url', 'image_url': {'url': 'https://image'}}]}]),
])
def test_uncapped_or_nontext_requests_fail_closed(payload):
    env = SimpleNamespace(id='owner')
    policy = json.loads(settings(env).model_call_budgets)['owner']
    with pytest.raises(HTTPException):
        guard.quote(payload, policy)


def test_cache_and_reasoning_not_double_counted_but_conservatively_priced(harness):
    client, _ = harness
    with client.session_factory() as db:
        env = owner(db)
        _, attempt = guard.reserve(db, env.id, 'secret', body(), settings(env))
        usage = {'prompt_tokens': 100, 'completion_tokens': 20, 'total_tokens': 120,
                 'prompt_tokens_details': {'cached_tokens': 80, 'cache_creation_tokens': 10},
                 'completion_tokens_details': {'reasoning_tokens': 15}}
        assert guard.settle(db, attempt, usage)
        row = db.get(ModelCallReservation, attempt)
        assert row.settled_nanos == 54000
        assert (row.usage['cache_read_tokens'], row.usage['cache_write_tokens'], row.usage['reasoning_tokens']) == (80,10,15)


def test_stream_receipts_split_across_bytes_and_duplicate(harness):
    client, _ = harness
    with client.session_factory() as db:
        env = owner(db)
        _, attempt = guard.reserve(db, env.id, 'secret', body(), settings(env))
    packet = b'data: {"usage":{"prompt_tokens":100,"completion_tokens":20},"choices":[]}\n\n'
    wire = packet + packet + b'data: [DONE]\n\n'
    class Response:
        async def aiter_bytes(self):
            for part in (wire[:20], wire[20:49], wire[49:]):
                yield part
    async def receive():
        return b''.join([c async for c in guard.relay(Response(), attempt, client.session_factory, True)])
    assert asyncio.run(receive()) == wire
    with client.session_factory() as db:
        assert db.get(ModelCallReservation, attempt).settled_nanos == 54000


def test_overbound_receipt_blocks_next_dispatch(harness):
    client, _ = harness
    with client.session_factory() as db:
        env = owner(db)
        _, attempt = guard.reserve(db, env.id, 'secret', body(), settings(env))
        assert not guard.settle(db, attempt, {'prompt_tokens': 1000000, 'completion_tokens': 20})
        with pytest.raises(HTTPException, match='requires_review'):
            guard.reserve(db, env.id, 'secret', body(), settings(env))


@pytest.mark.parametrize('disconnected', [False, True])
@pytest.mark.parametrize('asgi_spec', ['2.3', '2.4'])
def test_stream_drains_after_disconnect_without_second_dispatch(harness, disconnected, asgi_spec):
    client, _ = harness
    with client.session_factory() as db:
        env = owner(db)
        _, attempt = guard.reserve(db, env.id, 'secret', body(), settings(env))
    async def run():
        seen = asyncio.Event()
        class Provider(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'data: {"choices":[{"delta":{"content":"42"}}]}\n\n'
                await asyncio.wait_for(seen.wait(), 2)
                yield b'data: {"choices":[],"usage":{"prompt_tokens":100,"completion_tokens":20}}\n\n'
                yield b'data: [DONE]\n\n'
        upstream = httpx.Response(200, stream=Provider(), headers={'content-type': 'text/event-stream'})
        transport = httpx.AsyncClient()
        response = guard.guarded_response(upstream, transport, attempt, client.session_factory, True)
        async def send(message):
            if message['type'] == 'http.response.body' and message.get('body'):
                seen.set()
                if disconnected and asgi_spec == '2.4':
                    raise OSError('browser disconnected')
        async def receive():
            if disconnected:
                await seen.wait()
                return {'type': 'http.disconnect'}
            await asyncio.Future()
        await asyncio.wait_for(response({'type': 'http', 'asgi': {'spec_version': asgi_spec}}, receive, send), 5)
        assert upstream.is_closed and transport.is_closed
    asyncio.run(run())
    with client.session_factory() as db:
        assert db.get(ModelCallReservation, attempt).settled_nanos == 54000


def test_original_gateway_terminal_and_budget_admission(monkeypatch, harness):
    import certifi
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from control_plane import programming
    from control_plane.settings import Settings
    client, _ = harness
    with client.session_factory() as db:
        env = owner(db)
    policy = settings(env, '.004')
    config = Settings(litellm_models='model', litellm_url='http://models', litellm_master_key='test-only',
                      workspace_service_ca=certifi.where(), model_tool_failure_guard=True,
                      model_call_budgets=policy.model_call_budgets)
    calls = []
    def provider(request):
        if request.url.path == '/model/info':
            return httpx.Response(200,json={'data':[{'model_name':'model','model_info':{
                'input_cost_per_token':.0000003,'output_cost_per_token':.0000012}}]})
        calls.append(json.loads(request.content))
        return httpx.Response(200, content=b'data: [DONE]\n\n', headers={'content-type': 'text/event-stream'})
    original = httpx.AsyncClient
    monkeypatch.setattr(programming.httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(provider)))
    async def identity(*args): return env
    async def space(*args, **kw): return 'unused', 'unused'
    monkeypatch.setattr(programming, 'resolve_identity', identity)
    monkeypatch.setattr(programming, 'resolve_space', space)
    app = FastAPI()
    app.include_router(programming.router)
    def db_session():
        with client.session_factory() as db:
            yield db
    app.dependency_overrides[programming.get_db] = db_session
    app.dependency_overrides[programming.get_settings] = lambda: config
    web = TestClient(app)
    headers = {'Authorization': 'Bearer synthetic-only'}
    blocked = web.post('/agent/v1/chat/completions', headers=headers,
                       json=body(messages=tools('Execution error: code_execution_quota_exhausted')))
    assert blocked.status_code == 200 and blocked.json()['platform_guard'] == 'office_tool_terminal_failure' and not calls
    assert web.post('/agent/v1/chat/completions', headers=headers, json=body(stream=True)).status_code == 200
    denied = web.post('/agent/v1/chat/completions', headers=headers, json=body(stream=True))
    assert denied.status_code == 200 and 'model_turn_budget_exhausted' in denied.text
    assert len(calls) == 1  # Lost usage cannot lead to another paid request.


def test_sales_price_change_or_missing_price_blocks_dispatch():
    policy = json.loads(settings(SimpleNamespace(id='owner')).model_call_budgets)['owner']
    async def run():
        for info in ({'input_cost_per_token':.0000003,'output_cost_per_token':.0000012},
                     {'input_cost_per_token':.0000003,'output_cost_per_token':.0000012,'cache_creation_input_token_cost':.0000025},
                     {'input_cost_per_token':.0000003}):
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,json={
                'data':[{'model_name':'model','model_info':info}]}))) as client:
                cfg=SimpleNamespace(litellm_url='http://synthetic',litellm_master_key='synthetic-only')
                if len(info)==2:
                    assert await guard.verify_price_ceiling(body(),policy,cfg,client)
                else:
                    with pytest.raises(HTTPException):
                        await guard.verify_price_ceiling(body(),policy,cfg,client)
    asyncio.run(run())


def test_unused_priority_and_cache_ttl_prices_do_not_block_standard_route():
    policy=json.loads(settings(SimpleNamespace(id='owner')).model_call_budgets)['owner']
    info={'input_cost_per_token':.0000003,'output_cost_per_token':.0000012,
          'output_cost_per_token_priority':.00003,'cache_creation_input_token_cost_above_12hr':.00002}
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,json={
            'data':[{'model_name':'model','model_info':info}]}))) as client:
            assert await guard.verify_price_ceiling(body(),policy,SimpleNamespace(litellm_url='http://test',litellm_master_key='test'),client)
    asyncio.run(run())
    for override in ({'service_tier':'priority'},{'prompt_cache_retention':'24h'}):
        with pytest.raises(HTTPException):guard.quote(body(**override),policy)
