"""Continuously compare independent shadow receipts with a scoped, read-only LiteLLM view."""
from collections import Counter, defaultdict
from datetime import timedelta
from decimal import Decimal
import logging
import os
import time

import psycopg
from psycopg.rows import dict_row
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError

from .models import CostReceipt, CostSyncState, now
from .cost_console import report_tokens
from .litellm_usage_export import canonical_call_id
from .settings import get_settings

STATE_ID = 2
LIMIT = 10000
FIELDS = ('prompt_tokens', 'completion_tokens', 'total_tokens')


def compare(receipts, native):
    original, shadow = defaultdict(list), defaultdict(list)
    for row in native:
        # Keep failed zero-charge attempts too: missing usage is not a missing ledger row.
        original[row['request_id']].append(row)
    for row in receipts:
        shadow[row.payload.get('response_id')].append(row)
    issues, matches = [], []
    for request_id in sorted(original.keys() | shadow.keys(), key=lambda key: key or ''):
        ledger, journal = original[request_id], shadow[request_id]
        issue = {'request_id': request_id, 'receipt_ids': [row.id for row in journal[:10]]}
        if len(ledger) > 1 or (request_id and len(journal) > 1):
            issues.append({**issue, 'reason': 'duplicate_receipt'})
            continue
        if not ledger:
            issues.append({**issue, 'reason': 'missing_native'})
            continue
        if not journal:
            issues.append({**issue, 'reason': 'missing_shadow'})
            continue
        source, receipt = ledger[0], journal[0]
        call = receipt.payload['call_id']
        native_call = source['litellm_call_id'] or ''
        retry_prefix = call + '-attempt-'
        if receipt.payload.get('observation_scope') == 'litellm_spend_record':
            same_call = bool(native_call) and canonical_call_id(native_call) == call
        else:
            same_call = native_call == call or (native_call.startswith(retry_prefix)
                                                and native_call[len(retry_prefix):].isdigit())
        if not same_call:
            issues.append({**issue, 'reason': 'call_mismatch'})
            continue
        if receipt.native_spend is not None and Decimal(receipt.native_spend) != Decimal(source['spend']):
            issues.append({**issue, 'reason': 'spend_mismatch', 'shadow': receipt.native_spend,
                           'native': source['spend']})
            continue
        usage = receipt.payload.get('usage', {})
        if receipt.payload.get('usage_state') != 'observed':
            if source['status']=='failure' and receipt.payload.get('status')=='failure' and Decimal(source['spend'])==0:
                matches.append((receipt,source['spend']))
                issues.append({**issue,'reason':'unbilled_usage_pending'})
                continue
            issues.append({**issue, 'reason': 'unconfirmed_usage'})
            continue
        if receipt.payload.get('usage_format'):
            normalized = report_tokens(receipt)
            expected = dict(zip(FIELDS, (normalized.get('input_tokens'), normalized.get('output_tokens'), normalized.get('total_tokens'))))
        else:
            expected = {field: usage.get(field) for field in FIELDS}
        if expected['total_tokens'] is None:
            if expected['prompt_tokens'] is None or expected['completion_tokens'] is None:
                issues.append({**issue, 'reason': 'unconfirmed_usage'})
                continue
            expected['total_tokens'] = expected['prompt_tokens'] + expected['completion_tokens']
        actual = {field: source[field] for field in FIELDS}
        if expected != actual:
            issues.append({**issue, 'reason': 'token_mismatch', 'shadow': expected, 'native': actual})
            continue
        matches.append((receipt, source['spend']))
    return matches, issues


def read_native(dsn, since=None, owners=None):
    with psycopg.connect(dsn, connect_timeout=5, row_factory=dict_row) as connection:
        with connection.cursor() as cursor:
            cursor.execute('SET TRANSACTION READ ONLY')
            cursor.execute('SET LOCAL statement_timeout = 5000')
            cursor.execute("SET LOCAL TIME ZONE 'UTC'")
            if since is None:
                cursor.execute('SELECT * FROM starbridge_metering.native_receipts LIMIT %s', (LIMIT + 1,))
            else:
                cursor.execute('''SELECT * FROM starbridge_v105_metering.native_receipts
                    WHERE started_at >= %s AND (%s::text[] IS NULL OR owner_id=ANY(%s::text[])) LIMIT %s''',
                               (since, owners, owners, LIMIT + 1))
            return cursor.fetchall()


def managed_owners(dsn,since):
    with psycopg.connect(dsn,connect_timeout=5,row_factory=dict_row) as connection:
        with connection.cursor() as cursor:
            cursor.execute('SET TRANSACTION READ ONLY')
            cursor.execute('SET LOCAL statement_timeout = 5000')
            cursor.execute('SELECT DISTINCT owner_id FROM starbridge_v105_metering.native_receipts WHERE started_at >= %s LIMIT %s',(since,LIMIT+1))
            rows=cursor.fetchall()
            if len(rows)>LIMIT:raise RuntimeError('Managed audit scope overflow')
            return [r['owner_id'] for r in rows]


def audit(db, read_original, owner, channel, since):
    if db.bind.dialect.name == 'postgresql':
        if not db.scalar(text('SELECT pg_try_advisory_xact_lock(101039)')):
            return {'status': 'busy'}
    else:
        db.execute(text('BEGIN IMMEDIATE'))
    report = {'status': 'unavailable', 'scope': 'all_managed_accounts' if os.environ.get('NATIVE_AUDIT_ALL_MANAGED')=='true' else 'configured_test_cohort', 'since': since.isoformat()}
    try:
        # Source first so a receipt arriving during the scan is visible in the next pass.
        original = read_original()
    except (psycopg.Error, OSError):
        original = None
    if original is not None:
        owners = (owner,) if isinstance(owner, str) else tuple(owner)
        receipts = list(db.scalars(select(CostReceipt).where(CostReceipt.owner_id.in_(owners),
            CostReceipt.channel == channel, CostReceipt.occurred_at >= since).limit(LIMIT + 1)))
        report.update(native_rows=len(original), shadow_rows=len(receipts))
        if max(len(original), len(receipts)) > LIMIT:
            report['status'] = 'overflow'
        else:
            matches, issues = compare(receipts, original)
            for receipt, amount in matches:
                if receipt.native_spend is None:
                    receipt.native_spend = amount
            report.update(status='differences' if issues else ('matched' if matches else 'empty'),
                          matched=len(matches), issue_counts=dict(Counter(i['reason'] for i in issues)),
                          issue_count=len(issues), issues=issues[:100])
    state = db.get(CostSyncState, STATE_ID)
    if state is None:
        state = CostSyncState(id=STATE_ID)
        db.add(state)
    state.report, state.checked_at = report, now()
    db.commit()
    return report


def main():
    from datetime import datetime
    from .database import SessionLocal

    if not get_settings().cost_console_enabled:
        raise SystemExit('Cost audit is disabled')
    since = datetime.fromisoformat(os.environ['NATIVE_AUDIT_SINCE'])
    if since.utcoffset() is None:
        raise SystemExit('NATIVE_AUDIT_SINCE requires a time zone')
    dsn = os.environ['NATIVE_AUDIT_DSN']
    owner, channel = os.environ['NATIVE_AUDIT_OWNER'], os.environ['NATIVE_AUDIT_CHANNEL']
    owners = [owner, *filter(None, os.environ.get('NATIVE_AUDIT_ADDITIONAL_OWNERS', '').split(','))]
    while True:
        try:
            if os.environ.get('NATIVE_AUDIT_ALL_MANAGED')=='true':
                owners=managed_owners(dsn,since)
            with SessionLocal() as db:
                audit(db, lambda: read_native(dsn, since, owners), owners, channel, since)
        except (SQLAlchemyError,psycopg.Error,RuntimeError):
            logging.error('Shadow audit could not persist its report')
        time.sleep(30)


def audit_view(state):
    if state is None:
        return None
    from .cost_console import aware

    return {**state.report, 'checked_at': state.checked_at,
            'stale': now() - aware(state.checked_at) > timedelta(minutes=2)}


if __name__ == '__main__':
    main()
