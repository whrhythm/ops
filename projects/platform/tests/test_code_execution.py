"""Quota admission and gateway behavior over an actual HTTP dependency."""
import json
import threading
import time
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from sqlalchemy import select

from control_plane.code_execution import reserve, usage
from control_plane.auth import Principal
from control_plane.models import CodeExecution, CapacityAccount, now
from control_plane.settings import get_settings
from test_api import harness
from test_capacity import capacity_harness

PREFIX = '/internal/v1/codeapi/'


def test_account_usage_is_identity_bound_read_only_and_marks_stale(code_harness):
    client, signed, env_id, calls, issuer = code_harness
    from control_plane.models import ModelWallet
    with client.session_factory.begin() as db:
        db.add(ModelWallet(environment_id=env_id, status='ok', observed_at=now()-timedelta(minutes=4),
                           sample={'remaining_usd_micros': 1234567, 'spent_usd_micros': 8, 'total_tokens': 9}))
    assert client.get(PREFIX+'account-usage').status_code == 403
    assert client.get(PREFIX+'account-usage', headers=signed('victim')).status_code == 404
    result = client.get(PREFIX+'account-usage?subject=victim', headers=signed())
    assert result.status_code == 200 and result.headers['cache-control'] == 'no-store'
    assert result.json()['wallet']['remaining_usd_micros'] == 1234567
    assert result.json()['wallet']['status'] == 'stale'
    assert result.json()['code_execution']['used_runs'] == 0
    assert not calls
    with client.session_factory() as db:
        assert not list(db.scalars(select(CodeExecution)))


@pytest.fixture
def code_harness(capacity_harness, monkeypatch):
    client, auth, env_id, observed, issuer = capacity_harness
    key = Ed25519PrivateKey.generate()
    monkeypatch.setenv('PLATFORM_CODE_EXECUTION_ENABLED', 'true')
    monkeypatch.setenv('PLATFORM_CODE_EXECUTION_PUBLIC_KEY', key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode())
    calls = []
    receipts = {}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_GET(self):
            if self.path.startswith('/download/'):
                expected = self.path == '/download/test-session/file-a?kind=user'
                self.send_response(200 if expected else 400)
                self.end_headers()
                if expected:
                    self.wfile.write(b'order,total\nA,42\n')
                return
            key = self.path.rsplit('/', 1)[-1]
            self.send_response(200 if key in receipts else 404)
            self.send_header('Content-Type', 'application/json'); self.end_headers()
            self.wfile.write(json.dumps({'state': 'complete', 'result': receipts[key]} if key in receipts else {}).encode())
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            key = self.headers.get('X-Starbridge-Execution-Key')
            if key and key in receipts:
                self.send_response(200); self.end_headers(); self.wfile.write(json.dumps(receipts[key]).encode()); return
            calls.append(body)
            if key:
                receipts[key] = {'stdout': '42', 'session_id': 'test-session'}
                if body['code'] == 'disconnect':
                    self.close_connection = True
                    return
            status = 429 if body['code'] == 'limited' else 503 if body['code'] == 'ambiguous' else 400 if body['code'] == 'invalid' else 200
            self.send_response(status); self.send_header('Content-Type', 'application/json')
            if status == 429: self.send_header('Retry-After', body.get('retry_after', '1'))
            self.end_headers()
            if body['code'] == 'malformed-result':
                self.wfile.write(b'[]')
                return
            self.wfile.write(json.dumps({'stdout': '42', 'session_id': 'test-session'} if status == 200 else {'error': 'private-provider-diagnostic'}).encode())
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    monkeypatch.setenv('PLATFORM_CODE_EXECUTION_URL', f'http://127.0.0.1:{server.server_port}/')
    get_settings.cache_clear()
    def signed(subject='alice', **extra):
        claims = dict(iss='librechat', aud='codeapi', sub='mongo-user', iat=int(time.time()), exp=int(time.time())+300,
                      platform_subject=subject, platform_issuer=issuer)
        return {'Authorization': 'Bearer ' + jwt.encode({**claims, **extra}, key, algorithm='EdDSA')}
    yield client, signed, env_id, calls, issuer
    server.shutdown(); server.server_close(); thread.join(); get_settings.cache_clear()


@pytest.mark.parametrize('path,hint,expected', [
    ('upload', '1', '1'), ('upload/batch', '7', '7'),
    ('upload', '0', None), ('upload', '99999', None),
    ('upload', 'invalid', None), ('exec', '1', None),
])
def test_provider_upload_throttle_keeps_only_safe_retry_hint(code_harness, path, hint, expected):
    client, signed, env_id, calls, issuer = code_harness
    result = client.post(PREFIX + path, headers=signed(),
                         json={'code': 'limited', 'lang': 'py', 'retry_after': hint})
    assert result.status_code == 429
    assert result.headers.get('retry-after') == expected
    assert 'private-provider' not in result.text
    assert len(calls) == 1  # gateway does not replay an execution or upload
    with client.session_factory() as db:
        row = db.scalar(select(CodeExecution))
        assert row.state == 'rejected'


def test_limits_identity_idempotency_and_timeout(code_harness):
    client, signed, env_id, calls, issuer = code_harness
    assert client.post(PREFIX+'exec', json={'code':'1','lang':'py'}).status_code == 403
    assert client.post(PREFIX+'exec', headers=signed(platform_issuer='https://other'), json={'code':'1','lang':'py'}).status_code == 403
    assert client.post(PREFIX+'exec', headers=signed('victim'), json={'code':'1','lang':'py'}).status_code == 404
    assert client.post(PREFIX+'programmatic/exec', headers=signed(), json={}).status_code == 404
    header = {**signed(), 'Idempotency-Key': 'synthetic-execution-0001'}
    response = client.post(PREFIX+'exec', headers=header, json={'code':'1','lang':'py','timeout':900000})
    assert response.status_code == 200 and calls[0]['timeout'] == 10000
    assert client.post(PREFIX+'exec', headers=header, json={'code':'1','lang':'py'}).status_code == 409
    assert len(calls) == 1
    with client.session_factory.begin() as db:
        db.merge(CapacityAccount(environment_id=env_id, plan={'id':'existing','code_runs_monthly':1}))
    exhausted = client.post(PREFIX+'exec', headers=signed(), json={'code':'1','lang':'py'})
    assert exhausted.status_code == 429 and 'retry-after' not in exhausted.headers
    with client.session_factory.begin() as db:
        account = db.get(CapacityAccount, env_id)
        account.plan = {'id':'basic', 'code_runs_monthly':2}
    assert client.post(PREFIX+'exec', headers=signed(), json={'code':'1','lang':'py'}).status_code == 200
    assert len(calls) == 2


def test_unclear_outcome_charged_no_retry_and_bounded_lease(code_harness):
    client, signed, env_id, calls, issuer = code_harness
    result = client.post(PREFIX+'exec', headers=signed(), json={'code':'ambiguous','lang':'py'})
    assert result.status_code == 503 and 'private-provider' not in result.text and len(calls) == 1
    blocked = client.post(PREFIX+'exec', headers=signed(), json={'code':'1','lang':'py'})
    assert blocked.status_code == 429 and 'retry-after' not in blocked.headers
    with client.session_factory.begin() as db:
        row = db.scalar(select(CodeExecution))
        assert row.state == 'unknown'
        row.lease_until = now()-timedelta(seconds=1)
    assert client.post(PREFIX+'exec', headers=signed(), json={'code':'1','lang':'py'}).status_code == 200
    with client.session_factory() as db:
        from control_plane.models import UserEnvironment
        state = usage(db, db.get(UserEnvironment, env_id))
        assert state['used_runs'] == 2


def test_definite_preexecution_rejection_not_charged_and_month_rollover(code_harness):
    client, signed, env_id, calls, issuer = code_harness
    assert client.post(PREFIX+'exec', headers=signed(), json={'code':'invalid','lang':'py'}).status_code == 400
    with client.session_factory.begin() as db:
        from control_plane.models import UserEnvironment
        assert usage(db, db.get(UserEnvironment, env_id))['used_runs'] == 0
        row = db.scalar(select(CodeExecution)); row.state = 'complete'; row.period = '2020-01'
        assert usage(db, db.get(UserEnvironment, env_id))['used_runs'] == 0


def test_upload_reservations_bounded_and_count_separate(code_harness):
    client, signed, env_id, calls, issuer = code_harness
    with client.session_factory.begin() as db:
        db.merge(CapacityAccount(environment_id=env_id, plan={'code_runs_monthly':1}))
    with client.session_factory() as db:
        row = reserve(db, Principal(issuer, 'alice'), 'synthetic-upload-0001', '0'*64, 'upload', 16*1024*1024)
        row.state = 'complete'; db.commit()
        with pytest.raises(Exception) as error:
            reserve(db, Principal(issuer, 'alice'), 'synthetic-upload-0002', '0'*64, 'upload', 1)
        assert error.value.status_code == 429


def test_preparation_busy_is_retryable_only_before_dispatch(code_harness, monkeypatch):
    monkeypatch.setenv('PLATFORM_CODE_EXECUTION_BUSY_WAIT_SECONDS', '0')
    get_settings.cache_clear()
    client, signed, env_id, calls, issuer = code_harness
    with client.session_factory() as db:
        uploads = [reserve(db, Principal(issuer, 'alice'), f'preparing-file-{n:04}', '0'*64, 'upload', 80)
                   for n in range(4)]
        with pytest.raises(Exception) as error:
            reserve(db, Principal(issuer, 'alice'), 'preparing-file-0005', '0'*64, 'upload', 80)
        assert error.value.status_code == 429 and error.value.headers['Retry-After'] == '1'
        execution = reserve(db, Principal(issuer, 'alice'), 'running-code-0001', '0'*64)
        execution_id = execution.id
    blocked = client.post(PREFIX+'exec', headers=signed(), json={'code':'1','lang':'py'})
    assert blocked.status_code == 429 and blocked.headers['retry-after'] == '1'
    assert calls == []
    with client.session_factory.begin() as db:
        assert len(db.scalars(select(CodeExecution)).all()) == 5
        db.get(CodeExecution, execution_id).state = 'complete'
    response = client.post(PREFIX+'exec', headers=signed(), json={'code':'1','lang':'py'})
    assert response.status_code == 200 and len(calls) == 1
    with client.session_factory() as db:
        from control_plane.models import UserEnvironment
        state = usage(db, db.get(UserEnvironment, env_id))
        assert state['used_runs'] == 2 and state['active_runs'] == 0
        assert len(db.scalars(select(CodeExecution).where(CodeExecution.operation == 'upload')).all()) == 4


def test_gateway_waits_for_a_definite_busy_slot_without_charging_rejected_attempts(
    code_harness, monkeypatch
):
    monkeypatch.setenv('PLATFORM_CODE_EXECUTION_BUSY_WAIT_SECONDS', '2')
    get_settings.cache_clear()
    client, signed, env_id, calls, issuer = code_harness
    from control_plane.code_execution import month_key

    with client.session_factory.begin() as db:
        db.merge(CapacityAccount(environment_id=env_id, plan={'id': 'basic', 'code_runs_monthly': 5,
                                                               'code_concurrency': 1}))
        db.add(CodeExecution(environment_id=env_id, request_key='active-command-0001',
                             request_hash='0' * 64, operation='exec', transfer_bytes=0,
                             period=month_key(), state='running', timeout_seconds=10,
                             lease_until=now() + timedelta(seconds=1)))

    started = time.monotonic()
    result = client.post(PREFIX + 'exec', headers=signed(), json={'code': '1', 'lang': 'py'})
    elapsed = time.monotonic() - started
    assert result.status_code == 200
    assert elapsed >= 0.8
    assert len(calls) == 1
    with client.session_factory() as db:
        from control_plane.models import UserEnvironment
        state = usage(db, db.get(UserEnvironment, env_id))
        assert state['used_runs'] == 2 and state['active_runs'] == 0


def test_unknown_active_execution_is_not_queued_or_replayed(code_harness, monkeypatch):
    monkeypatch.setenv('PLATFORM_CODE_EXECUTION_BUSY_WAIT_SECONDS', '2')
    get_settings.cache_clear()
    client, signed, env_id, calls, issuer = code_harness
    from control_plane.code_execution import month_key

    with client.session_factory.begin() as db:
        db.merge(CapacityAccount(environment_id=env_id, plan={'id': 'basic', 'code_runs_monthly': 5,
                                                               'code_concurrency': 1}))
        db.add(CodeExecution(environment_id=env_id, request_key='unknown-command-0001',
                             request_hash='0' * 64, operation='exec', transfer_bytes=0,
                             period=month_key(), state='unknown', timeout_seconds=10,
                             lease_until=now() + timedelta(seconds=30)))

    started = time.monotonic()
    result = client.post(PREFIX + 'exec', headers=signed(), json={'code': '1', 'lang': 'py'})
    assert result.status_code == 429 and 'retry-after' not in result.headers
    assert time.monotonic() - started < 1.0
    assert calls == []
