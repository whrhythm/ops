"""Read a scoped LiteLLM spend projection and export confirmed rows as OTLP logs."""
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid5

import httpx
import psycopg
from psycopg.rows import dict_row

from .model_usage import normalize_usage, retain_usage

EXPORT_NAMESPACE = UUID('39819ecb-1eef-4dde-a627-d3dcf49e7b46')
PAGE_SIZE = 64
SCOPE = 'starbridge.model-usage'


def utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def canonical_call_id(value: str) -> str:
    origin, marker, attempt = value.rpartition('-attempt-')
    logical = origin if origin and marker and attempt.isascii() and attempt.isdigit() else value
    try:
        return str(UUID(logical))
    except ValueError:
        return str(uuid5(EXPORT_NAMESPACE, f'call:{logical}'))


def from_spend_row(row):
    """Every observed original row keeps its identity across restarts and replays."""
    request_id = row['request_id']
    if not request_id or not row['litellm_call_id'] or not row['owner_id']:
        raise ValueError('spend_record_missing_identity')
    call_id = canonical_call_id(row['litellm_call_id'])
    # LiteLLM's normalized usage object supplies the cache/read/write fields.
    # Failure counters with no usage object may be placeholders and stay unknown.
    usage = retain_usage(row['usage'])
    if usage or row['status'] == 'success':
        for key in ('prompt_tokens', 'completion_tokens', 'total_tokens'):
            if key not in usage and row[key] is not None:
                usage[key] = row[key]
    normalize_usage(usage, 'litellm')
    return {
        'attempt_id': str(uuid5(EXPORT_NAMESPACE, f'record:{request_id}')), 'call_id': call_id,
        'owner_id': row['owner_id'], 'channel': row['channel'], 'model': row['model'],
        'started_at': utc(row['started_at']).isoformat(), 'finished_at': utc(row['finished_at']).isoformat(),
        'status': row['status'] or 'unknown', 'response_id': request_id,
        'usage_format': 'litellm', 'usage': usage, 'observation_scope': 'litellm_spend_record',
    }


def otlp_logs(records):
    return {'resourceLogs': [{'scopeLogs': [{'scope': {'name': SCOPE}, 'logRecords': [
        {'body': {'stringValue': json.dumps(record, separators=(',', ':'))}} for record in records
    ]}]}]}


def page(dsn, cursor):
    with psycopg.connect(dsn, connect_timeout=5, row_factory=dict_row) as connection:
        with connection.cursor() as query:
            query.execute('SET TRANSACTION READ ONLY')
            query.execute('SET LOCAL statement_timeout = 5000')
            query.execute('''SELECT request_id, litellm_call_id, owner_id, channel, model, started_at,
                 finished_at, created_at, status, prompt_tokens, completion_tokens, total_tokens, usage
                 FROM starbridge_v105_metering.usage_receipts WHERE (created_at, request_id) > (%s, %s)
                 ORDER BY created_at, request_id LIMIT %s''', (*cursor, PAGE_SIZE))
            return query.fetchall()


def publish(client, endpoint, records):
    result = client.post(endpoint, json=otlp_logs(records))
    result.raise_for_status()
    if result.json().get('partialSuccess', {}).get('rejectedLogRecords', 0) not in (0, '0'):
        raise RuntimeError('collector_rejected_metering_records')


def main():
    cursor_file = Path(os.environ['METER_CURSOR_FILE'])
    dsn = os.environ['METER_SOURCE_DSN']
    endpoint = os.environ['METER_OTLP_LOGS_URL']
    token = os.environ['METER_OTLP_TOKEN']
    if len(token) < 32:
        raise SystemExit('Metering collector token is not configured')
    try:
        start, request_id = json.loads(cursor_file.read_text())
    except FileNotFoundError:
        start, request_id = os.environ['METER_START_UTC'], ''
    cursor = (utc(datetime.fromisoformat(start)).replace(tzinfo=None), request_id)
    with httpx.Client(timeout=15, follow_redirects=False, trust_env=False,
                      headers={'Authorization': f'Bearer {token}'}) as client:
        while True:
            records = page(dsn, cursor)
            if not records:
                time.sleep(5)
                continue
            publish(client, endpoint, [from_spend_row(row) for row in records])
            last = records[-1]
            cursor = (last['created_at'], last['request_id'])
            pending = cursor_file.with_suffix('.pending')
            with pending.open('w', encoding='utf-8') as file:
                json.dump([cursor[0].isoformat(), cursor[1]], file)
                file.flush()
                os.fsync(file.fileno())
            pending.replace(cursor_file)


if __name__ == '__main__':
    main()
