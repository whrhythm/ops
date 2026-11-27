"""Durable model-attempt journal and shadow outbox. Never changes a quota or balance."""
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4
from .model_usage import retain_usage


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def usage_fields(raw):
    return retain_usage(raw)


SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    id TEXT PRIMARY KEY, owner_id TEXT, model TEXT NOT NULL,
    stream INTEGER NOT NULL, started_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS attempts (
    id TEXT PRIMARY KEY, call_id TEXT NOT NULL REFERENCES calls(id),
    ordinal INTEGER NOT NULL, channel TEXT NOT NULL, model TEXT NOT NULL,
    started_at TEXT NOT NULL, UNIQUE(call_id, ordinal)
);
CREATE TABLE IF NOT EXISTS outbox (
    event_id TEXT PRIMARY KEY REFERENCES attempts(id), payload TEXT NOT NULL,
    published_at TEXT
);
CREATE TABLE IF NOT EXISTS outbox_schedule (
    event_id TEXT PRIMARY KEY REFERENCES outbox(event_id), available_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dispatch_limits (
    batch TEXT PRIMARY KEY, attempts INTEGER NOT NULL
);
"""


class ShadowMeter:
    def __init__(self, path):
        self.path = path
        with closing(self.connect()) as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript(SCHEMA)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA synchronous=FULL')
        return db

    def call(self, call_id, owner_id, model, stream):
        with closing(self.connect()) as db, db:
            db.execute('INSERT INTO calls VALUES (?, ?, ?, ?, ?) ON CONFLICT(id) DO NOTHING',
                       (call_id, owner_id, model, int(stream), utc_now()))
            bound = db.execute('SELECT owner_id FROM calls WHERE id=?', (call_id,)).fetchone()
            if bound['owner_id'] != owner_id:
                raise ValueError('shadow_call_identity_conflict')

    def start(self, call_id, channel, model, *, batch=None, limit=None):
        attempt_id = str(uuid4())
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            if batch is not None:
                reservation = db.execute(
                    '''INSERT INTO dispatch_limits VALUES (?, 1)
                       ON CONFLICT(batch) DO UPDATE SET attempts=attempts+1 WHERE attempts < ?''',
                    (batch, limit),
                )
                if reservation.rowcount != 1:
                    raise ValueError('shadow_dispatch_limit')
            prev = db.execute('SELECT ordinal,channel FROM attempts WHERE call_id=? ORDER BY ordinal DESC LIMIT 1',
                              (call_id,)).fetchone()
            db.execute('INSERT INTO attempts VALUES (?, ?, ?, ?, ?, ?)',
                       (attempt_id, call_id, prev['ordinal'] + 1 if prev else 1, channel, model, utc_now()))
        return attempt_id

    def finish(self, attempt_id, status, http_status, request_id, response_id, model, usage, price,
               *, defer_seconds=0):
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute(
                '''SELECT a.*, c.owner_id, c.stream, p.channel AS previous_channel
                   FROM attempts a JOIN calls c ON c.id=a.call_id LEFT JOIN attempts p
                     ON p.call_id=a.call_id AND p.ordinal=a.ordinal-1 WHERE a.id=?''', (attempt_id,)
            ).fetchone()
            if rows is None:
                raise ValueError('shadow_attempt_missing')
            result = usage_fields(usage)
            event = {
                'schema': 1, 'event_id': attempt_id, 'attempt_id': attempt_id, 'call_id': rows['call_id'],
                'owner_id': rows['owner_id'], 'ordinal': rows['ordinal'], 'channel': rows['channel'],
                'dispatch': 'initial' if rows['ordinal'] == 1 else (
                    'retry' if rows['previous_channel'] == rows['channel'] else 'fallback'),
                'requested_model': rows['model'], 'model': model, 'stream': bool(rows['stream']),
                'started_at': rows['started_at'], 'finished_at': utc_now(), 'http_status': http_status,
                'status': status, 'provider_request_id': request_id, 'response_id': response_id,
                'usage': result, 'usage_state': (
                    'observed' if {'prompt_tokens', 'completion_tokens'} <= result.keys() else 'pending'
                ),
                'price_snapshot': price, 'settlement': 'shadow',
            }
            inserted = db.execute(
                'INSERT INTO outbox(event_id,payload) VALUES (?, ?) ON CONFLICT(event_id) DO NOTHING',
                (attempt_id, json.dumps(event, separators=(',', ':'))),
            )
            if inserted.rowcount == 1 and defer_seconds > 0 and event['usage_state'] == 'observed':
                due = datetime.fromisoformat(event['finished_at']) + timedelta(seconds=defer_seconds)
                db.execute('INSERT INTO outbox_schedule VALUES (?, ?)', (attempt_id, due.isoformat()))

    def snapshot(self):
        with closing(self.connect()) as db, db:
            db.execute('BEGIN')
            calls = [dict(row) for row in db.execute('SELECT * FROM calls ORDER BY started_at,id')]
            attempts = [dict(row) for row in db.execute('SELECT * FROM attempts ORDER BY call_id,ordinal')]
            cutoff = utc_now()
            events = [json.loads(row['payload']) for row in db.execute(
                '''SELECT o.payload FROM outbox o LEFT JOIN outbox_schedule s ON o.event_id=s.event_id
                   WHERE s.available_at IS NULL OR s.available_at <= ? ORDER BY o.event_id''', (cutoff,),
            )]
            deferred = [dict(row) for row in db.execute(
                'SELECT event_id,available_at FROM outbox_schedule WHERE available_at > ? ORDER BY event_id',
                (cutoff,),
            )]
        return {'calls': calls, 'attempts': attempts, 'events': events, 'deferred': deferred}

    def pending_usage(self, owner_id, limit=100):
        if not owner_id or not 1 <= limit <= 500:
            raise ValueError('invalid_shadow_publication_scope')
        with closing(self.connect()) as db:
            rows = db.execute(
                '''SELECT o.payload
                   FROM outbox o JOIN attempts a ON a.id=o.event_id JOIN calls c ON c.id=a.call_id
                   LEFT JOIN outbox_schedule s ON s.event_id=o.event_id
                   WHERE c.owner_id=? AND o.published_at IS NULL
                     AND (s.available_at IS NULL OR s.available_at <= ?)
                     AND json_extract(o.payload,'$.usage_state')='observed'
                   ORDER BY a.started_at,a.id LIMIT ?''', (owner_id, utc_now(), limit),
            )
            return [json.loads(row['payload']) for row in rows]

    def acknowledge_usage(self, event_id):
        with closing(self.connect()) as db, db:
            db.execute('UPDATE outbox SET published_at=? WHERE event_id=? AND published_at IS NULL',
                       (utc_now(), event_id))


def reconcile_spend(snapshot, spend_rows):
    """Match the original ledger by its provider response ID and call identity."""
    rows_by_id = {}
    for row in spend_rows:
        key = row['request_id']
        if key in rows_by_id and rows_by_id[key] != row:
            raise ValueError('conflicting_litellm_receipt')
        rows_by_id[key] = row
    charged_rows = {key: row for key, row in rows_by_id.items() if
                    Decimal(str(row.get('spend') or '0')) > 0 or (
                        row.get('status') != 'failure' and (row.get('total_tokens') or 0) > 0)}
    accepted_ids = {event['event_id'] for event in snapshot['events']}
    pending = {attempt['id'] for attempt in snapshot['attempts'] if attempt['id'] not in accepted_ids}
    matched = {}
    differences = []
    for event in snapshot['events']:
        if event['usage_state'] != 'observed':
            pending.add(event['attempt_id'])
            continue
        row = charged_rows.get(event['response_id'])
        original_call = row.get('litellm_call_id') if row else None
        retry_call = f"{event['call_id']}-attempt-"
        same_call = original_call == event['call_id'] or (
            isinstance(original_call, str) and original_call.startswith(retry_call)
            and original_call[len(retry_call):].isdigit()
        )
        if not row or not same_call or event['response_id'] in matched:
            differences.append({'attempt_id': event['attempt_id'], 'reason': 'missing_or_conflicting_receipt'})
            continue
        keys = ('prompt_tokens', 'completion_tokens', 'total_tokens')
        if any(event['usage'].get(key) is not None and event['usage'][key] != row.get(key) for key in keys):
            differences.append({'attempt_id': event['attempt_id'], 'reason': 'token_counts_differ'})
            continue
        matched[event['response_id']] = {'attempt_id': event['attempt_id'], 'call_id': event['call_id'],
                                         'request_id': event['response_id'], 'usage': event['usage'],
                                         'spend': str(row.get('spend') or '0')}
    for key in charged_rows.keys() - matched.keys():
        differences.append({'request_id': key, 'reason': 'no_shadow_match'})
    rejected = [{'request_id': key, 'call_id': row['litellm_call_id'],
                 'proxy_reported_tokens': row.get('total_tokens')}
                for key, row in rows_by_id.items() if row.get('status') == 'failure' and key not in charged_rows]
    return {'matched': list(matched.values()), 'pending': sorted(pending), 'uncharged_failures': rejected,
            'differences': differences, 'original_rows': len(rows_by_id)}
