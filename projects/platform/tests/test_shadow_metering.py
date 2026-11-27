from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from control_plane import shadow_metering
from control_plane.shadow_metering import ShadowMeter, reconcile_spend, usage_fields
from control_plane.shadow_openmeter import publish_usage
from control_plane.shadow_relay import Completion


def test_attempt_identity_ordinal_and_replay_survive_restart(tmp_path):
    path = tmp_path / 'shadow.sqlite3'
    call_id = str(uuid4())
    meter = ShadowMeter(path)
    meter.call(call_id, 'user', 'example', True)
    with ThreadPoolExecutor(max_workers=4) as pool:
        started = list(pool.map(lambda _: meter.start(call_id, 'primary', 'example'), range(8)))
    assert len(set(started)) == 8
    meter = ShadowMeter(path)
    attempt = meter.start(call_id, 'backup', 'example-v2')
    for _ in range(2):
        meter.finish(attempt, 'complete', 200, 'req1', 'response1', 'example-v2',
                     {'prompt_tokens': 17, 'completion_tokens': 9, 'total_tokens': 26}, None)
    snapshot = meter.snapshot()
    assert [a['ordinal'] for a in snapshot['attempts']] == list(range(1, 10))
    assert len(snapshot['events']) == 1
    assert snapshot['events'][0]['dispatch'] == 'fallback'
    spend = {'request_id': 'response1', 'litellm_call_id': call_id, 'spend': '0.0000159',
             'prompt_tokens': 17, 'completion_tokens': 9, 'total_tokens': 26}
    report = reconcile_spend(snapshot, [spend, spend])
    assert len(report['matched']) == 1 and not report['differences']
    assert set(report['pending']) == set(started)
    diff = reconcile_spend(snapshot, [{**spend, 'prompt_tokens': 20}])
    assert {row['reason'] for row in diff['differences']} == {'token_counts_differ', 'no_shadow_match'}


def test_fragmented_stream_usage_is_read_once_and_counts_are_subsets():
    completion = Completion(streaming=True)
    for chunk in [b'data: {"id": "resp", "model": "v2", "choices": []}\r\n\r\n',
                  b'data: {"usage":{"prompt_tokens":7,"completion_tokens":5,',
                  b'"total_tokens":12,"prompt_tokens_details":{"cached_tokens":3,"private":"drop"},',
                  b'"completion_tokens_details":{"reasoning_tokens":2}}}\n\ndata: [DO',
                  b'NE]\n\n']:
        completion.observe(chunk)
    assert completion.done and completion.response_id == 'resp'
    assert usage_fields(completion.usage) == {
        'prompt_tokens': 7, 'completion_tokens': 5, 'total_tokens': 12,
        'prompt_tokens_details': {'cached_tokens': 3},
        'completion_tokens_details': {'reasoning_tokens': 2},
    }


def test_failed_attempt_without_usage_is_pending_not_free(tmp_path):
    meter = ShadowMeter(tmp_path / 'shadow.sqlite3')
    call = str(uuid4())
    meter.call(call, 'a', 'requested', True)
    first = meter.start(call, 'primary', 'requested')
    second = meter.start(call, 'primary', 'requested')
    meter.finish(first, 'failed', 503, 'failed-request', None, None, {}, None)
    meter.finish(second, 'unconfirmed', None, None, None, None, {}, None)
    snapshot = meter.snapshot()
    assert {e['dispatch'] for e in snapshot['events']} == {'initial', 'retry'}
    assert all(e['usage'] == {} and e['usage_state'] == 'pending' for e in snapshot['events'])
    report = reconcile_spend(snapshot, [])
    assert set(report['pending']) == {first, second} and report['differences'] == []


def test_deferred_provider_receipt_is_durable_and_converges_once(tmp_path, monkeypatch):
    now = '2026-11-16T09:00:00+00:00'
    monkeypatch.setattr(shadow_metering, 'utc_now', lambda: now)
    path = tmp_path / 'shadow.sqlite3'
    meter = ShadowMeter(path)
    call = str(uuid4())
    meter.call(call, 'user', 'supplier-late', True)
    attempt = meter.start(call, 'siliconrouter', 'test')
    usage = {'prompt_tokens': 17, 'completion_tokens': 9, 'total_tokens': 26}
    outcome = (attempt, 'complete', 200, 'request-a', 'response-a', 'test', usage, None)
    meter.finish(*outcome, defer_seconds=90)
    ledger = [{'request_id': 'response-a', 'litellm_call_id': call, 'spend': '0.0000159', **usage}]
    meter = ShadowMeter(path)
    meter.finish(*outcome)
    pending = meter.snapshot()
    assert pending['deferred'] == [{'event_id': attempt, 'available_at': '2026-11-16T09:01:30+00:00'}]
    assert not meter.pending_usage('user')
    assert reconcile_spend(pending, ledger)['pending'] == [attempt]
    now = '2026-11-16T09:01:30+00:00'
    settled = meter.snapshot()
    assert not settled['deferred']
    assert len(settled['events']) == 1 and settled['events'][0]['event_id'] == attempt
    assert meter.pending_usage('user') == settled['events']
    report = reconcile_spend(settled, ledger)
    assert len(report['matched']) == 1 and not report['pending'] and not report['differences']
    meter.finish(*outcome, defer_seconds=90)
    assert meter.snapshot() == settled


def test_supplier_budget_survives_restart_and_concurrent_dispatch(tmp_path):
    path = tmp_path / 'shadow.sqlite3'
    meter = ShadowMeter(path)
    call = str(uuid4())
    meter.call(call, 'user', 'supplier-stream', True)

    def dispatch(_):
        try:
            return meter.start(call, 'siliconrouter', 'test', batch='trial', limit=3)
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=5) as pool:
        attempts = list(pool.map(dispatch, range(5)))
    assert len([attempt for attempt in attempts if attempt is not None]) == 3
    with pytest.raises(ValueError, match='shadow_dispatch_limit'):
        ShadowMeter(path).start(call, 'siliconrouter', 'test', batch='trial', limit=3)


def test_openmeter_retries_the_same_identity_after_lost_ack(tmp_path):
    path = tmp_path / 'shadow.sqlite3'
    meter = ShadowMeter(path)
    call = str(uuid4())
    meter.call(call, 'candidate-owner', 'supplier-stream', True)
    attempt = meter.start(call, 'siliconrouter', 'model-alias')
    meter.finish(attempt, 'complete', 200, 'provider-request', 'provider-response', 'actual-model',
                 {'prompt_tokens': 25, 'completion_tokens': 71, 'total_tokens': 96}, None)
    call = str(uuid4())
    meter.call(call, 'candidate-owner', 'supplier-error', True)
    pending = meter.start(call, 'siliconrouter', 'model-alias')
    meter.finish(pending, 'failed', 502, 'provider-error', None, None, {}, None)
    seen = []

    def lose_ack(event):
        seen.append(event)
        raise TimeoutError('result unavailable')

    assert publish_usage(meter, 'other-owner', seen.append) == 0
    with pytest.raises(TimeoutError):
        publish_usage(meter, 'candidate-owner', lose_ack)
    assert publish_usage(ShadowMeter(path), 'candidate-owner', seen.append) == 1
    assert seen[0] == seen[1]
    assert seen[0]['id'] == attempt and seen[0]['data']['total_tokens'] == 96
    assert seen[0]['data']['model'] == 'actual-model'
    assert publish_usage(meter, 'candidate-owner', seen.append) == 0
