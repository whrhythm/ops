"""Durable admission/recovery against a real HTTP provider fixture (not CodeAPI acceptance)."""
from datetime import timedelta
from sqlalchemy import select, func
from control_plane.models import CodeExecution, OfficeTask, now
from control_plane.settings import get_settings
from test_code_execution import code_harness
from test_capacity import capacity_harness
from test_api import harness

PREFIX = '/internal/v1/codeapi/'


def test_invalid_result_never_becomes_success(code_harness, monkeypatch):
    enabled(monkeypatch)
    client, signed, *_ = code_harness
    response = client.post(PREFIX+'exec', headers=headers(signed), json={'code': 'malformed-result', 'lang': 'py'})
    assert response.status_code == 503
    with client.session_factory() as db:
        assert db.scalar(select(CodeExecution)).state == 'unknown'
        assert db.scalar(select(OfficeTask)).result is None


def test_task_view_is_bounded_and_does_not_expose_internal_fields():
    from control_plane.code_execution import task_view
    task = OfficeTask(id='test', conversation_id='c', state='complete', result={
        'stdout': 'x' * 100000, 'stderr': '', 'code': 'private source',
        'files': [{'id': 'f', 'name': 'file', 'internal': 'secret'}] * 100})
    view = task_view(task)['result']
    assert len(view['stdout']) == 8192 and len(view['files']) == 50
    assert 'code' not in view and 'internal' not in view['files'][0]


def enabled(monkeypatch):
    monkeypatch.setenv('PLATFORM_OFFICE_TASKS_ENABLED', 'true')
    get_settings.cache_clear()


def headers(signed, key='office-task-execution-0001', conversation='test-conversation'):
    return {**signed(), 'X-Starbridge-Execution-Key': key, 'X-Starbridge-Conversation': conversation}


def test_duplicate_returns_saved_result_without_second_admission(code_harness, monkeypatch):
    enabled(monkeypatch)
    client, signed, env, calls, issuer = code_harness
    from hashlib import sha256
    key = sha256(b'["test-conversation","call-word"]' ).hexdigest()
    h = headers(signed, key=key)
    body = {'code': 'print(42)', 'lang': 'py'}
    first = client.post(PREFIX+'exec', headers=h, json=body)
    assert first.status_code == 200
    second = client.post(PREFIX+'exec', headers=h, json=body)
    assert second.status_code == 200 and second.json() == first.json() and len(calls) == 1
    assert client.post(PREFIX+'exec', headers=h, json={**body, 'code': 'different'}).status_code == 409
    assert client.post(PREFIX+'exec', headers=headers(signed, key=key, conversation='other'), json=body).status_code == 409
    with client.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(CodeExecution)) == 1
        task = db.scalar(select(OfficeTask))
        assert task.state == 'complete' and task.payload == body
    listing = client.get(PREFIX+'tasks?conversation_id=test-conversation', headers=signed()).json()
    assert len(listing['tasks']) == 1
    assert listing['tasks'][0]['call_key'] == key
    assert 'payload' not in listing['tasks'][0]
    assert client.get(PREFIX+'tasks?conversation_id=other', headers=signed()).json() == {'tasks': []}


def test_old_receipt_remains_addressable_after_fifty_newer_tasks(code_harness, monkeypatch):
    from uuid import uuid4
    enabled(monkeypatch)
    client, signed, env, calls, _ = code_harness
    start = now() - timedelta(minutes=2)
    with client.session_factory() as db:
        for i in range(53):
            stamp = start + timedelta(seconds=i)
            receipt = str(uuid4())
            db.add(CodeExecution(id=receipt, environment_id=env, request_key=f'{i:064x}',
                                 request_hash='0'*64, period='2026-11', state='complete',
                                 timeout_seconds=10, lease_until=stamp, created_at=stamp))
            db.flush()
            db.add(OfficeTask(id=receipt, environment_id=env, conversation_id='history',
                              state='complete', payload={}, result={}, created_at=stamp))
        db.commit()
    url = PREFIX + 'tasks?conversation_id=history'
    first = f'{0:064x}'
    page = client.get(url, headers=signed()).json()['tasks']
    assert len(page) == 50 and first not in {r['call_key'] for r in page}
    matched = client.get(url + '&call_key=' + first, headers=signed()).json()['tasks']
    assert [r['call_key'] for r in matched] == [first]
    assert client.get(url.replace('history', 'other') + '&call_key=' + first, headers=signed()).json()['tasks'] == []
    assert client.get(url + '&call_key=x', headers=signed()).status_code == 400
    assert not calls


def test_lost_response_recovered_by_read_without_reexecute(code_harness, monkeypatch):
    enabled(monkeypatch)
    client, signed, env, calls, issuer = code_harness
    assert client.post(PREFIX+'exec', headers=headers(signed), json={'code': 'disconnect', 'lang': 'py'}).status_code == 503
    with client.session_factory() as db:
        task_id = db.scalar(select(OfficeTask.id))
    # New request/session simulates reconnect; provider has retained the completed receipt.
    response = client.get(PREFIX+'tasks/'+task_id, headers=signed())
    assert response.status_code == 200 and response.json()['state'] == 'complete'
    assert response.json()['result']['stdout'] == '42' and len(calls) == 1
    assert client.get(PREFIX+'tasks/'+task_id, headers=signed('victim')).status_code == 404
    assert client.get(PREFIX+'tasks/'+task_id).status_code == 403
    with client.session_factory() as db:
        assert db.get(CodeExecution, task_id).state == 'complete'


def test_missing_provider_receipt_cannot_cause_a_second_dispatch(code_harness, monkeypatch):
    enabled(monkeypatch)
    client, signed, _, calls, _ = code_harness
    h = headers(signed)
    body = {'code': 'ambiguous', 'lang': 'py'}
    assert client.post(PREFIX+'exec', headers=h, json=body).status_code == 503
    # Point at a missing provider receipt to model lost or evicted Redis data.
    monkeypatch.setenv('PLATFORM_CODE_EXECUTION_URL', 'http://127.0.0.1:1/')
    get_settings.cache_clear()
    response = client.post(PREFIX+'exec', headers=h, json=body)
    assert response.status_code == 409
    assert response.json()['detail'] == 'office_task_already_submitted'
    assert len(calls) == 1


def test_disabled_and_expired_receipts_never_reexecute(code_harness, monkeypatch):
    client, signed, env, calls, issuer = code_harness
    assert client.get(PREFIX+'tasks?conversation_id=test-conversation', headers=signed()).status_code == 404
    enabled(monkeypatch)
    body = {'code': 'ambiguous', 'lang': 'py'}
    client.post(PREFIX+'exec', headers=headers(signed), json=body)
    with client.session_factory.begin() as db:
        task = db.scalar(select(OfficeTask))
        task.created_at = now() - timedelta(days=7)
    assert client.post(PREFIX+'exec', headers=headers(signed), json=body).status_code == 409
    assert len(calls) == 1


def test_cancelling_before_dispatch_fences_replay_and_keeps_quota(code_harness, monkeypatch):
    from control_plane.code_execution import authenticate, reserve
    import hashlib
    import json

    enabled(monkeypatch)
    client, signed, _, calls, _ = code_harness
    key = 'office-task-execution-0001'
    auth = signed()
    body = {'code': 'print(42)', 'lang': 'py'}
    encoded = json.dumps(body, separators=(',', ':')).encode()
    with client.session_factory() as db:
        principal = authenticate(auth['Authorization'].removeprefix('Bearer '), get_settings())
        task_id = reserve(db, principal, key, hashlib.sha256(encoded).hexdigest(),
                          task={'conversation_id': 'test-conversation', 'payload': body}).id
    cancel_url = PREFIX + f'tasks/{task_id}/cancel'
    assert client.post(cancel_url, headers=signed('victim')).status_code == 404
    assert client.post(cancel_url, headers=auth).json()['state'] == 'cancelled'
    assert client.post(cancel_url, headers=auth).json()['state'] == 'cancelled'
    assert client.post(PREFIX + 'exec', headers=headers(signed), content=encoded).status_code == 409
    assert not calls
    with client.session_factory() as db:
        assert db.get(CodeExecution, task_id).state == 'rejected'


def test_own_expired_output_returns_gone_but_receipt_survives(code_harness, monkeypatch):
    enabled(monkeypatch)
    client, signed, _, calls, _ = code_harness
    assert client.post(PREFIX + 'exec', headers=headers(signed), json={'code': 'print(42)', 'lang': 'py'}).status_code == 200
    with client.session_factory.begin() as db:
        task = db.scalar(select(OfficeTask))
        task_id = task.id
        task.result = {**task.result, 'files': [{'id': 'file-a', 'name': 'report.csv'}],
                       '_output_expires_at': (now() - timedelta(seconds=1)).isoformat()}
    route = PREFIX + f'tasks/{task_id}'
    assert client.get(route + '/outputs/file-a', headers=signed('victim')).status_code == 404
    assert client.get(route + '/outputs/file-a', headers=signed()).status_code == 410
    retained = client.get(route, headers=signed()).json()
    assert retained['state'] == 'complete' and retained['outputs_expired'] is True
    assert retained['result']['files'][0]['id'] == 'file-a'
    assert len(calls) == 1


def test_output_scope_is_derived_from_task_not_request_query(code_harness, monkeypatch):
    enabled(monkeypatch)
    client, signed, _, _, _ = code_harness
    assert client.post(PREFIX + 'exec', headers=headers(signed), json={'code': 'print(42)', 'lang': 'py'}).status_code == 200
    with client.session_factory.begin() as db:
        task = db.scalar(select(OfficeTask))
        task_id = task.id
        task.result = {**task.result, 'files': [{'id': 'file-a', 'name': 'report.csv'}]}
    route = PREFIX + f'tasks/{task_id}/outputs/'
    assert client.get(route + 'file-b', headers=signed()).status_code == 404
    response = client.get(route + 'file-a?kind=agent&id=other-owner', headers=signed())
    assert response.status_code == 200
    assert response.content == b'order,total\nA,42\n'
