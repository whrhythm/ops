from datetime import datetime
import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from control_plane.cost_native_audit import compare
from control_plane.litellm_usage_export import from_spend_row, publish
from control_plane.metering_receiver import metering_records
from control_plane.model_usage import normalize_usage


def test_spend_export_retains_categories_and_reuses_event_on_replay():
    call_id = str(uuid4())
    row = {'request_id': 'original-provider-response', 'litellm_call_id': call_id,
           'owner_id': 'formal-test-owner', 'channel': 'siliconrouter', 'model': 'configured-model',
           'started_at': datetime(2026, 11, 17), 'finished_at': datetime(2026, 11, 17),
           'status': 'success', 'prompt_tokens': 9480, 'completion_tokens': 816, 'total_tokens': 10296,
           'usage': {'prompt_tokens_details': {'cached_tokens': 8576},
                     'completion_tokens_details': {'reasoning_tokens': 42}}, 'spend': 'private-debit'}
    original = from_spend_row(row)
    retry = from_spend_row({**row, 'request_id': 'billed-retry-response', 'litellm_call_id': call_id + '-attempt-2'})
    records = []

    def receive(request):
        records.extend(metering_records(json.loads(request.content)))
        return httpx.Response(200, json={})

    with httpx.Client(transport=httpx.MockTransport(receive)) as client:
        publish(client, 'https://collector.invalid/v1/logs', [original, retry])
        publish(client, 'https://collector.invalid/v1/logs', [original])
    assert len({event['event_id'] for event in records}) == 2
    assert {event['call_id'] for event in records} == {call_id}
    assert records[0] == records[2] and 'spend' not in records[0]
    assert all(event['observation_scope'] == 'litellm_spend_record' for event in records)
    usage = normalize_usage(records[0]['usage'], 'litellm')
    assert (usage['cache_read_tokens'], usage['cache_write_tokens'], usage['output_tokens'],
            usage['reasoning_tokens']) == (8576, None, 816, 42)

    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={
        'partialSuccess': {'rejectedLogRecords': '1'},
    }))) as client:
        with pytest.raises(RuntimeError, match='rejected_metering_records'):
            publish(client, 'https://collector.invalid/v1/logs', [original])


def test_opaque_call_ids_group_billed_attempts_without_merging_receipts():
    row = {'litellm_call_id': 'logical-call', 'owner_id': 'formal-test-owner', 'channel': 'siliconrouter',
           'model': 'configured-model', 'started_at': datetime(2026, 11, 17), 'finished_at': datetime(2026, 11, 17),
           'status': 'success', 'prompt_tokens': 10, 'completion_tokens': 2, 'total_tokens': 12,
           'usage': {}, 'spend': '0.00004'}
    originals = [{**row, 'request_id': 'upstream-a'},
                 {**row, 'request_id': 'upstream-b', 'litellm_call_id': 'logical-call-attempt-2'}]
    receipts = [
        SimpleNamespace(id=payload['attempt_id'], payload={**payload, 'usage_state': 'observed'}, native_spend=None)
        for payload in map(from_spend_row, originals)
    ]
    assert receipts[0].payload['call_id'] == receipts[1].payload['call_id']
    assert receipts[0].id != receipts[1].id
    matches, issues = compare(receipts, originals)
    assert not issues and len(matches) == 2
