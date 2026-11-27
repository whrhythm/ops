"""Durable shadow delivery and OpenMeter reconciliation, separate from all debit paths."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import time

import httpx
from sqlalchemy import select, text

from .cost_console import aware, observed, price_receipt, tokens_for
from .models import CostReceipt, CostSyncState, now
from .settings import get_settings

SOURCE = 'urn:starbridge:shadow:cost-console:v1'
TYPE = 'starbridge.shadow.usage.v1'
COST_TYPE = 'starbridge.shadow.cost.v1'
METERS = {'prompt_tokens': 'office_input_tokens', 'completion_tokens': 'office_output_tokens',
          'requests': 'office_requests', 'cost_pico': 'office_cost_pico'}
BREAKDOWN_METERS = {
    'uncached_input_tokens': 'office_uncached_input_tokens',
    'cache_read_tokens': 'office_cache_read_tokens',
    'cache_write_tokens': 'office_cache_write_tokens',
    'reasoning_tokens': 'office_reasoning_tokens',
    'breakdown_incomplete': 'office_breakdown_incomplete',
}
BREAKDOWN_TYPE = 'starbridge.usage.breakdown.v1'


def usage_values(row):
    if not row.payload.get('usage_format'):
        return {key: row.payload['usage'][key] for key in ('prompt_tokens', 'completion_tokens')}
    tokens = tokens_for(row)
    return {'prompt_tokens': tokens['input_tokens'], 'completion_tokens': tokens['output_tokens']}


def breakdown_values(row):
    tokens = tokens_for(row)
    parts = {key: tokens[key] for key in BREAKDOWN_METERS if key in tokens and tokens[key] is not None}
    if any(tokens[key] is None for key in ('uncached_input_tokens', 'cache_read_tokens', 'cache_write_tokens')):
        parts['breakdown_incomplete'] = 1
    return parts


def breakdown_events(row):
    # Scalar events allow a field to be missing without feeding zero into that meter.
    for field, amount in breakdown_values(row).items():
        yield {
            'specversion': '1.0', 'source': SOURCE, 'type': f'{BREAKDOWN_TYPE}.{field}',
            'id': f'{row.id}:usage:{field}', 'subject': row.owner_id,
            'time': aware(row.occurred_at).isoformat(),
            'data': {'channel': row.channel, 'model': row.model, 'tokens': amount},
        }


def meter_definitions():
    defs = []
    for field, slug in METERS.items():
        cost = field == 'cost_pico'
        m = {'slug': slug, 'eventType': COST_TYPE if cost else TYPE,
             'aggregation': 'COUNT' if field == 'requests' else 'SUM',
             'groupBy': {'channel': '$.channel', 'model': '$.model'}}
        if field != 'requests':
            m['valueProperty'] = f'$.{field}'
        if cost:
            m['groupBy'].update(currency='$.currency', price_kind='$.price_kind')
        defs.append(m)
    return defs + [{'slug': slug, 'eventType': f'{BREAKDOWN_TYPE}.{field}', 'aggregation': 'SUM',
                    'valueProperty': '$.tokens', 'groupBy': {'channel': '$.channel', 'model': '$.model'}}
                   for field, slug in BREAKDOWN_METERS.items()]


class OpenMeterClient:
    def __init__(self):
        settings = get_settings()
        if not settings.openmeter_url:
            raise ValueError('计量服务尚未配置')
        self.client = httpx.Client(base_url=settings.openmeter_url.rstrip('/'),
                                   timeout=settings.openmeter_timeout, follow_redirects=False)

    def send(self, event):
        response = self.client.post('/api/v1/events', json=event,
                                    headers={'Content-Type': 'application/cloudevents+json'})
        response.raise_for_status()

    def query(self, slug):
        response = self.client.get('/api/v1/meters/' + slug + '/query', params=[
            ('groupBy', 'channel'), ('groupBy', 'model'), ('groupBy', 'currency'), ('groupBy', 'price_kind')
        ] if slug == METERS['cost_pico'] else [('groupBy', 'channel'), ('groupBy', 'model')])
        response.raise_for_status()
        return response.json()['data']

    def close(self):
        self.client.close()


def cloud_event(row, cost=False):
    data = {'channel': row.channel, 'model': row.model, 'call_id': row.payload['call_id']}
    if cost:
        scaled = Decimal(row.cost) * Decimal(10**12)
        if scaled != scaled.to_integral_value() or abs(scaled) >= 2**53:
            raise ValueError('成本超出精确聚合范围，待人工核验')
        data.update(cost_pico=int(scaled), currency=row.currency, rate_id=row.rate_id)
    else:
        data.update(usage_values(row))
    return {'specversion': '1.0', 'source': SOURCE, 'type': COST_TYPE if cost else TYPE,
            'id': row.id + (':cost' if cost else ':usage'), 'subject': row.owner_id,
            'time': aware(row.occurred_at).isoformat(), 'data': data}


def synchronize(db, limit=100, client=None):
    # Serialize candidate worker/admin sync and DB writers, including several worker replicas.
    if db.bind.dialect.name == 'postgresql':
        if not db.scalar(text('SELECT pg_try_advisory_xact_lock(101038)')):
            return {'status': 'busy', 'sent': 0}
    else:
        db.execute(text('BEGIN IMMEDIATE'))
    owned = client is None
    sent = 0
    error = None
    has_breakdown = False
    try:
        client = client or OpenMeterClient()
        rows = list(db.scalars(select(CostReceipt).where(
            (CostReceipt.usage_sent_at.is_(None)) | (CostReceipt.cost_sent_at.is_(None))
        ).order_by(CostReceipt.occurred_at, CostReceipt.id).limit(10000)))
        for row in rows:
            if sent >= limit:
                break
            if not observed(row):
                continue
            if row.payload.get('usage_format'):
                has_breakdown = True
            price_receipt(db, row)
            # Imported time is durably committed before any possible transmission, including crash-before-ack.
            if datetime.now(timezone.utc) - aware(row.imported_at) > timedelta(days=31):
                row.last_error = '超过安全重投窗口，待人工核验'
                continue
            row.first_send_at = row.first_send_at or now()
            try:
                if row.usage_sent_at is None:
                    client.send(cloud_event(row))
                    if row.payload.get('usage_format'):
                        for event in breakdown_events(row):
                            client.send(event)
                    row.usage_sent_at = now(); sent += 1
                if row.cost is not None and row.cost_sent_at is None:
                    event = cloud_event(row, cost=True)
                    from .models import PurchaseRate
                    event['data']['price_kind'] = 'test' if db.get(PurchaseRate, row.rate_id).test_only else 'supplier'
                    client.send(event)
                    row.cost_sent_at = now(); sent += 1
                row.last_error = None
            except (httpx.HTTPError, ValueError, KeyError):
                row.last_error = '计量同步未确认，将按原编号重试'
                error = 'unavailable'
                break
        db.flush()
        expected = {}
        for row in db.scalars(select(CostReceipt)):
            if not observed(row):
                continue
            values = {**usage_values(row), 'requests': 1}
            if row.payload.get('usage_format'):
                has_breakdown = True
                values.update(breakdown_values(row))
            for key, amount in values.items():
                k = (key, row.channel, row.model, '', '')
                expected[k] = expected.get(k, 0) + amount
            if row.cost is not None:
                from .models import PurchaseRate
                kind = 'test' if db.get(PurchaseRate, row.rate_id).test_only else 'supplier'
                k = ('cost_pico', row.channel, row.model, row.currency, kind)
                expected[k] = expected.get(k, 0) + int(Decimal(row.cost) * 10**12)
        actual = {}
        meters = {**METERS, **BREAKDOWN_METERS} if has_breakdown else METERS
        for key, slug in meters.items():
            for entry in client.query(slug):
                group = entry.get('groupBy', {})
                k = (key, group.get('channel', ''), group.get('model', ''),
                     group.get('currency', ''), group.get('price_kind', ''))
                value = Decimal(str(entry['value']))
                actual[k] = actual.get(k, Decimal(0)) + value
        diffs = [{'metric': k[0], 'channel': k[1], 'model': k[2], 'currency': k[3], 'price_kind': k[4],
                  'expected': str(expected.get(k, 0)), 'actual': str(actual.get(k, 0))}
                 for k in sorted(expected.keys() | actual.keys()) if expected.get(k, 0) != actual.get(k, 0)]
        report = {'status': error or ('pending' if diffs else 'matched'), 'sent': sent,
                  'differences': diffs, 'scope': 'all_shadow_receipts', 'dedupe_days': 32}
    except (httpx.HTTPError, ValueError, KeyError):
        report = {'status': 'unavailable', 'sent': sent, 'differences': [], 'scope': 'all_shadow_receipts'}
    finally:
        if owned and client is not None:
            client.close()
    state = db.get(CostSyncState, 1)
    if state is None:
        state = CostSyncState(id=1); db.add(state)
    state.report, state.checked_at = report, now()
    db.commit()
    return report


def main():
    from .database import SessionLocal
    if not get_settings().cost_console_enabled:
        raise SystemExit('Shadow cost console is disabled')
    while True:
        with SessionLocal() as db:
            synchronize(db)
        time.sleep(15)


if __name__ == '__main__':
    import argparse
    import json

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--print-meters', action='store_true')
    if parser.parse_args().print_meters:
        print(json.dumps(meter_definitions(), indent=2))
    else:
        main()
