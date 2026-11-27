from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4
import httpx
import pytest
import psycopg
from sqlalchemy import select

from control_plane.cost_console import import_receipt
from control_plane.cost_meter_worker import BREAKDOWN_METERS, BREAKDOWN_TYPE, METERS, synchronize
from control_plane.cost_native_audit import audit
from control_plane.models import AuditEvent, CostReceipt, PurchaseRate
from control_plane.purchase_pricing import PricingRule, quote
from control_plane.settings import get_settings


def setup(harness, monkeypatch):
    client, token = harness
    monkeypatch.setenv('PLATFORM_COST_CONSOLE_ENABLED', 'true')
    monkeypatch.setenv('PLATFORM_COST_CONSOLE_TEST_MODE', 'true')
    get_settings.cache_clear()
    return client, {'Authorization': 'Bearer '+token(groups=[get_settings().admin_group])}


def event():
    key = str(uuid4())
    return {'event_id': key, 'attempt_id': key, 'call_id': str(uuid4()), 'owner_id': 'test-owner',
            'channel': 'siliconrouter', 'model': 'actual-model', 'started_at': datetime.now(timezone.utc).isoformat(),
            'finished_at': datetime.now(timezone.utc).isoformat(), 'status': 'complete',
            'usage_state': 'observed', 'usage': {'prompt_tokens': 25, 'completion_tokens': 70, 'total_tokens': 95}}


def rate(**extra):
    return {'id': str(uuid4()), 'channel': 'siliconrouter', 'model': 'actual-model', 'currency': 'USD',
            'input_per_million': '0.3', 'output_per_million': '1.2',
            'effective_at': (datetime.now(timezone.utc)-timedelta(days=1)).isoformat(),
            'test_only': True, 'note': '测试价，不代表供应商合同价', **extra}


class Sink:
    def __init__(self):
        self.events = {}; self.fail = False; self.lose_ack = False

    def send(self, e):
        if self.fail:
            raise httpx.ConnectError('offline')
        self.events[(e['source'], e['id'])] = e
        if self.lose_ack:
            self.lose_ack = False
            raise httpx.ReadTimeout('ack lost')

    def query(self, slug):
        if self.fail:
            raise httpx.ConnectError('offline')
        metric = next(k for k,v in {**METERS, **BREAKDOWN_METERS}.items() if v==slug)
        grouped = {}
        for e in self.events.values():
            if metric in BREAKDOWN_METERS:
                if e['type'] == f'{BREAKDOWN_TYPE}.{metric}':
                    d = e['data']
                    key = (d['channel'], d['model'], None, None)
                    grouped[key] = grouped.get(key, 0) + d['tokens']
                continue
            if e['type'].startswith(BREAKDOWN_TYPE):
                continue
            is_cost = e['type'].endswith('cost.v1')
            if (metric=='cost_pico') != is_cost:
                continue
            d=e['data']; key=(d['channel'],d['model'],d.get('currency'),d.get('price_kind'))
            grouped[key] = grouped.get(key,0)+(1 if metric=='requests' else d[metric])
        return [{'groupBy': {k:v for k,v in zip(('channel','model','currency','price_kind'), key) if v is not None},
                 'value': value} for key,value in grouped.items()]


def test_native_audit_enriches_and_reports_without_overwriting(harness, monkeypatch):
    client, headers = setup(harness, monkeypatch)
    e = event()
    e['response_id'] = 'provider-response-id'
    since = datetime.now(timezone.utc) - timedelta(hours=1)
    original = {'request_id': e['response_id'], 'litellm_call_id': e['call_id'],
                'status': 'success', 'spend': '0.0000915', **e['usage']}
    with client.session_factory() as db:
        import_receipt(db, e)
        db.commit()
        assert audit(db, lambda: [original], e['owner_id'], e['channel'], since)['matched'] == 1
    with client.session_factory() as db:
        changed = {**original, 'spend': '0.0001915'}
        differences = audit(db, lambda: [changed], e['owner_id'], e['channel'], since)
        assert differences['issue_counts'] == {'spend_mismatch': 1}
        assert db.get(CostReceipt, e['event_id']).native_spend == original['spend']
    endpoint = '/v1/admin/costs/report'
    fetched = client.get(endpoint, headers=headers).json()['native_audit']
    assert fetched['status'] == 'differences' and not fetched['stale']

    def down():
        raise psycopg.OperationalError('source down')

    with client.session_factory() as db:
        assert audit(db, down, e['owner_id'], e['channel'], since)['status'] == 'unavailable'
        assert db.get(CostReceipt, e['event_id']).native_spend == original['spend']
        assert audit(db, lambda: [original], e['owner_id'], e['channel'], since)['status'] == 'matched'


def test_native_audit_accepts_paid_and_sponsor_cohort_without_other_accounts(harness,monkeypatch):
    client,_=setup(harness,monkeypatch)
    since=datetime.now(timezone.utc)-timedelta(hours=1)
    records=[]
    with client.session_factory() as db:
        for owner in ['paid','sponsor','unrelated']:
            e=event();e.update(owner_id=owner,response_id=owner+'-response')
            import_receipt(db,e)
            if owner!='unrelated':records.append({'request_id':e['response_id'],'litellm_call_id':e['call_id'],
                'status':'success','spend':'0.0000915',**e['usage']})
        db.commit()
        result=audit(db,lambda:records,['paid','sponsor'],'siliconrouter',since)
        assert result['status']=='matched' and result['matched']==2
        other=db.scalar(select(CostReceipt).where(CostReceipt.owner_id=='unrelated'))
        assert other.native_spend is None


def test_zero_charge_failure_retains_unknown_usage_without_missing_native_alarm(harness,monkeypatch):
    client,_=setup(harness,monkeypatch)
    e=event();e.update(status='failure',usage={},usage_format='litellm',response_id='unbilled-failure')
    source={'request_id':e['response_id'],'litellm_call_id':e['call_id'],'status':'failure',
        'spend':'0','prompt_tokens':500,'completion_tokens':0,'total_tokens':500}
    with client.session_factory() as db:
        row=import_receipt(db,e);db.commit()
        result=audit(db,lambda:[source],e['owner_id'],'siliconrouter',datetime.now(timezone.utc)-timedelta(hours=1))
        assert result['matched']==1 and result['issue_counts']=={'unbilled_usage_pending':1}
        assert row.native_spend=='0'
        assert row.payload['usage']=={} and row.payload['usage_state']=='pending'


def test_admin_only_feature_gate_and_unknown_cost(harness, monkeypatch):
    client, h = setup(harness, monkeypatch)
    _,token=harness
    assert client.get('/v1/admin/costs/report').status_code==401
    assert client.get('/v1/admin/costs/report',headers={'Authorization':'Bearer '+token()}).status_code==403
    with client.session_factory() as db:
        e=event(); import_receipt(db,e,'0.0000915');db.commit()
        import_receipt(db,e,'0.0000915');db.commit()
    r=client.get('/v1/admin/costs/report',headers=h).json()
    assert r['totals']['receipts']==1 and r['totals']['pending_price']==1
    assert r['items'][0]['cost'] is None
    monkeypatch.setenv('PLATFORM_COST_CONSOLE_ENABLED','false'); get_settings.cache_clear()
    assert client.get('/v1/admin/costs/report',headers=h).status_code==404


def test_rate_history_idempotency_and_lost_ack(harness, monkeypatch):
    client,h=setup(harness,monkeypatch)
    e=event()
    with client.session_factory() as db:
        import_receipt(db,e,'0.0000915');db.commit()
    p=rate()
    assert client.post('/v1/admin/costs/rates',headers=h,json=p).status_code==201
    assert client.post('/v1/admin/costs/rates',headers=h,json=p).status_code==201
    assert client.post('/v1/admin/costs/rates',headers=h,json={**p,'input_per_million':'9'}).status_code==409
    sink=Sink();sink.lose_ack=True
    with client.session_factory() as db:
        assert synchronize(db,client=sink)['status']=='unavailable'
        assert db.get(CostReceipt,e['event_id']).usage_sent_at is None
    with client.session_factory() as db:
        assert synchronize(db,client=sink)['status']=='matched'
        row=db.get(CostReceipt,e['event_id']);old=(row.rate_id,row.cost)
        assert row.cost=='0.0000915'
    assert len(sink.events)==2
    assert client.post('/v1/admin/costs/rates',headers=h,json=rate(
        effective_at=(datetime.now(timezone.utc)-timedelta(hours=1)).isoformat(),input_per_million='10')).status_code==201
    with client.session_factory() as db:
        assert synchronize(db,client=sink)['sent']==0
        row=db.get(CostReceipt,e['event_id']);assert (row.rate_id,row.cost)==old
        assert len(list(db.scalars(select(AuditEvent).where(AuditEvent.action=='costs.rate.create'))))==2
    r=client.get('/v1/admin/costs/report',headers=h).json()
    assert r['totals']['test_costs']=={'USD':'0.0000915'} and r['totals']['costs']=={}
    assert r['issue_count']==0


def test_down_recovery_currency_and_no_false_zero(harness,monkeypatch):
    client,h=setup(harness,monkeypatch)
    assert client.post('/v1/admin/costs/rates',headers=h,json=rate(currency='CNY')).status_code==201
    with client.session_factory() as db:
        e=event();import_receipt(db,e);db.commit()
    sink=Sink();sink.fail=True
    with client.session_factory() as db:
        assert synchronize(db,client=sink)['status']=='unavailable'
    r=client.get('/v1/admin/costs/report',headers=h).json()
    assert r['sync']['status']=='unavailable' and r['totals']['pending_sync']==1
    sink.fail=False
    with client.session_factory() as db:
        assert synchronize(db,client=sink)['status']=='matched'
    assert client.get('/v1/admin/costs/report',headers=h).json()['totals']['test_costs']=={'CNY':'0.0000915'}


def test_price_validation_and_cache_missing(harness,monkeypatch):
    client,h=setup(harness,monkeypatch)
    for changes in ({'input_per_million':'-1'},{'input_per_million':'NaN'}, {'test_only':False}):
        assert client.post('/v1/admin/costs/rates',headers=h,json=rate(**changes)).status_code==422
    p=rate();assert client.post('/v1/admin/costs/rates',headers=h,json=p).status_code==201
    assert client.post('/v1/admin/costs/rates',headers=h,json={**p,'id':str(uuid4())}).status_code==409
    with client.session_factory() as db:
        e=event();e['usage']['prompt_tokens_details']={'cached_tokens':10};import_receipt(db,e);db.commit()
    with client.session_factory() as db:
        synchronize(db,client=Sink())
    r=client.get('/v1/admin/costs/report',headers=h).json()
    assert r['items'][0]['cost'] is None and r['totals']['pending_price']==1


def test_internal_intake_auth_idempotence_and_conflict(harness, monkeypatch):
    client,h=setup(harness,monkeypatch)
    monkeypatch.setenv('PLATFORM_COST_INGEST_TOKEN','candidate-only-test-token')
    get_settings.cache_clear()
    e=event();url='/internal/v1/metering/receipts'
    assert client.post(url,headers=h,json={'event':e}).status_code==403
    internal={'X-Meter-Token':'candidate-only-test-token'}
    for _ in range(2):
        assert client.post(url,headers=internal,json={'event':e}).status_code==202
    assert client.post(url,headers=internal,json={'event':{**e,'channel':'other'}}).status_code==409
    assert client.post(url,headers=internal,json={'event':e,'native_spend':'0'}).status_code==422
    report=client.get('/v1/admin/costs/report',headers=h).json()
    assert report['totals']['receipts']==1 and report['totals']['native_unmatched']==1
    assert report['items'][0]['cost'] is None


def test_otlp_receipts_preserve_categories_and_replay_without_inflating_totals(harness, monkeypatch):
    import json
    from fastapi.testclient import TestClient
    from control_plane import metering_receiver

    client, admin_headers = setup(harness, monkeypatch)
    monkeypatch.setattr(metering_receiver, 'SessionLocal', client.session_factory)
    monkeypatch.setenv('PLATFORM_COST_INGEST_TOKEN', 'metering-test-token-' + 'x' * 32)
    get_settings.cache_clear()
    receiver = TestClient(metering_receiver.create_app())
    headers = {'X-Meter-Token': get_settings().cost_ingest_token}

    def post(*receipts, auth=headers):
        payload = {'resourceLogs': [{'scopeLogs': [{
            'scope': {'name': metering_receiver.SCOPE},
            'logRecords': [{'body': {'stringValue': json.dumps(record)}} for record in receipts],
        }]}]}
        return receiver.post('/v1/logs', json=payload, headers=auth)

    def create(source, usage):
        result = event()
        del result['usage_state'], result['event_id']
        return {**result, 'usage_format': source, 'usage': usage}

    china = create('deepseek_chat', {'prompt_tokens': 10000, 'completion_tokens': 2000,
                                  'prompt_cache_hit_tokens': 6000, 'prompt_cache_miss_tokens': 4000,
                                  'completion_tokens_details': {'reasoning_tokens': 500}, 'private': 'drop'})
    anthropic = create('anthropic_messages', {'input_tokens': 4000, 'output_tokens': 2000,
                                             'cache_read_input_tokens': 6000, 'cache_creation_input_tokens': 1000})
    pending = {**anthropic, 'usage': {}, 'status': 'unconfirmed'}
    assert post(china, auth={}).status_code == 403
    assert post(china, pending).status_code == 200
    assert post(anthropic).status_code == 200
    assert post(china, anthropic).status_code == 200
    assert post({**anthropic, 'channel': 'another-channel'}).status_code == 400
    assert post(create('openai_chat', {'prompt_tokens': 1, 'completion_tokens': 1,
                                     'prompt_tokens_details': {'cached_tokens': 2}})).status_code == 400
    report = client.get('/v1/admin/costs/report', headers=admin_headers).json()
    assert report['totals']['receipts'] == 2 and report['totals']['prompt_tokens'] == 21000
    by_id = {row['id']: row for row in report['items']}
    first = by_id[china['attempt_id']]
    assert first['tokens']['cache_read_tokens'] == 6000 and first['tokens']['cache_write_tokens'] is None
    assert first['tokens']['uncached_input_tokens'] == 4000
    assert first['tokens']['reasoning_tokens'] == 500 and first['tokens']['total_tokens'] == 12000
    assert 'private' not in first['usage']
    assert by_id[anthropic['attempt_id']]['tokens'] == {
        'input_tokens': 11000, 'uncached_input_tokens': 4000, 'cache_read_tokens': 6000,
        'cache_write_tokens': 1000, 'output_tokens': 2000, 'reasoning_tokens': None, 'total_tokens': 13000,
    }

    class LoseBreakdownAck(Sink):
        def send(self, payload):
            if payload['type'] == f'{BREAKDOWN_TYPE}.cache_read_tokens' and not hasattr(self, 'lost'):
                self.lost = True
                self.lose_ack = True
            super().send(payload)

    sink = LoseBreakdownAck()
    with client.session_factory() as db:
        assert synchronize(db, client=sink)['status'] == 'unavailable'
    with client.session_factory() as db:
        assert synchronize(db, client=sink)['status'] == 'matched'
    assert sink.query(BREAKDOWN_METERS['cache_read_tokens'])[0]['value'] == 12000
    assert sink.query(BREAKDOWN_METERS['cache_write_tokens'])[0]['value'] == 1000
    assert sink.query(BREAKDOWN_METERS['breakdown_incomplete'])[0]['value'] == 1
    assert sink.query(METERS['requests'])[0]['value'] == 2


def test_context_and_fixed_quotes_use_exact_receipt_and_leave_ledger_untouched(harness, monkeypatch):
    client, headers = setup(harness, monkeypatch)
    first = {'input_per_million': '2', 'output_per_million': '8', 'cached_per_million': '0.2',
             'cache_write_per_million': '4'}
    second = {'input_per_million': '3', 'output_per_million': '12', 'cached_per_million': '0.3',
              'cache_write_per_million': '6'}
    rule = {'mode': 'context', 'bands': [{**first, 'max_input_tokens': 32000},
                                        {**second, 'max_input_tokens': None}]}
    e = event()
    e.update(usage_format='anthropic_messages', usage={'input_tokens': 44000, 'output_tokens': 2000,
             'cache_read_input_tokens': 5000, 'cache_creation_input_tokens': 1000})
    edge = event()
    edge.update(usage_format='anthropic_messages', usage={'input_tokens': 32000, 'output_tokens': 1,
                'cache_read_input_tokens': 0, 'cache_creation_input_tokens': 0})
    with client.session_factory() as db:
        import_receipt(db, e, '0.0123')
        import_receipt(db, edge)
        db.commit()
    url = '/v1/admin/costs/rates/quote'
    body = {'receipt_id': e['event_id'], 'pricing_rule': rule, 'currency': 'CNY',
            'channel': e['channel'], 'model': e['model']}
    assert client.post(url, json=body).status_code == 401
    assert client.post(url, headers=headers, json={**body, 'model': 'unrelated'}).status_code == 409
    result = client.post(url, headers=headers, json=body).json()
    assert result['amount'] == '0.1635' and result['band']['min_input_tokens'] == 32001
    edge_quote = client.post(url, headers=headers, json={**body, 'receipt_id': edge['event_id']}).json()
    assert edge_quote['band']['number'] == 1 and edge_quote['amount'] == '0.064008'
    quote_fixed = client.post(url, headers=headers, json={**body, 'pricing_rule': {'mode':'fixed','fixed':first}}).json()
    assert quote_fixed['band'] is None and quote_fixed['amount'] == '0.109'
    with client.session_factory() as db:
        persisted = db.get(CostReceipt, e['event_id'])
        assert persisted.cost is None and persisted.rate_id is None and persisted.native_spend == '0.0123'
    assert client.post('/v1/admin/costs/rates', headers=headers, json=rate(pricing_rule={
        'mode': 'context', 'bands': [{**first, 'max_input_tokens': None}, {**second, 'max_input_tokens': None}],
    })).status_code == 422
    posted = client.post('/v1/admin/costs/rates', headers=headers, json=rate(currency='CNY', pricing_rule=rule))
    assert posted.status_code == 201
    with client.session_factory() as db:
        assert synchronize(db, client=Sink())['status'] == 'matched'
        persisted = db.get(CostReceipt, e['event_id'])
        assert persisted.cost == '0.1635' and persisted.native_spend == '0.0123'


def test_daily_prices_use_call_start_timezone_and_leave_native_spend_unchanged(harness, monkeypatch):
    client, headers = setup(harness, monkeypatch)
    price = {'input_per_million': '1', 'output_per_million': '2',
             'cached_per_million': '0.1', 'cache_write_per_million': '1.25'}
    rule = {'mode': 'time', 'timezone': 'Asia/Shanghai', 'time_bands': [
        {**price, 'starts_at': '00:00'},
        {**price, 'starts_at': '08:00', 'input_per_million': '3'},
    ]}
    local_midnight = datetime.now(timezone.utc).replace(hour=16, minute=0, second=0, microsecond=0) - timedelta(days=1)
    morning = local_midnight + timedelta(hours=8)
    instants = [local_midnight - timedelta(seconds=1), local_midnight,
                morning - timedelta(seconds=1), morning]
    receipts = []
    with client.session_factory() as db:
        for instant in instants:
            item = event()
            item.update(started_at=instant.isoformat(), finished_at=(instant+timedelta(minutes=2)).isoformat(),
                        usage_format='openai_chat', usage={'prompt_tokens': 1000, 'completion_tokens': 10,
                            'prompt_tokens_details': {'cached_tokens': 100, 'cache_write_tokens': 0}})
            import_receipt(db, item, '0.024')
            receipts.append(item)
        db.commit()
    body = {'pricing_rule': rule, 'currency': 'USD', 'channel': 'siliconrouter', 'model': 'actual-model'}
    url = '/v1/admin/costs/rates/quote'
    for item, amount, starts_at in zip(receipts, ('0.00273', '0.00093', '0.00093', '0.00273'),
                                       ('08:00', '00:00', '00:00', '08:00'), strict=True):
        result = client.post(url, headers=headers, json={**body, 'receipt_id': item['event_id']})
        assert result.status_code == 200
        assert Decimal(result.json()['amount']) == Decimal(amount)
        assert result.json()['time_band']['starts_at'] == starts_at
        assert result.json()['time_band']['local_time'].endswith('+08:00')
    for invalid in ({**rule, 'timezone': 'bad/zone'}, {**rule, 'time_bands': rule['time_bands'][::-1]}):
        assert client.post(url, headers=headers, json={**body, 'pricing_rule': invalid,
                           'receipt_id': receipts[0]['event_id']}).status_code == 422
    payload = rate(pricing_rule=rule, effective_at=(local_midnight-timedelta(days=1)).isoformat())
    for _ in range(2):
        assert client.post('/v1/admin/costs/rates', headers=headers, json=payload).status_code == 201
    with client.session_factory() as db:
        assert synchronize(db, client=Sink())['status'] == 'matched'
    result = client.get('/v1/admin/costs/report', headers=headers).json()
    assert result['totals']['native_spend_usd'] == '0.096'
    assert all(row['rate_id'] == payload['id'] and row['pricing_details']['time_band'] for row in result['items'])


def test_daily_schedule_can_begin_at_nine_and_wrap_to_the_next_day():
    peak = {'input_per_million': '1.33', 'output_per_million': '3.98', 'cached_per_million': '0.0442'}
    off_peak = {'input_per_million': '0.66', 'output_per_million': '1.99', 'cached_per_million': '0.0221'}
    rule = PricingRule.model_validate({'mode': 'time', 'timezone': 'Asia/Shanghai', 'time_bands': [
        {**peak, 'starts_at': '09:00'},
        {**off_peak, 'starts_at': '12:00'},
        {**peak, 'starts_at': '14:00'},
        {**off_peak, 'starts_at': '18:00'},
    ]})
    tokens = {'uncached_input_tokens': 1000000, 'output_tokens': 1000000,
              'cache_read_tokens': 1000000, 'cache_write_tokens': 0}
    cases = [
        ('2026-11-17T08:59:59+08:00', '18:00', '09:00', '2.6721'),
        ('2026-11-17T09:00:00+08:00', '09:00', '12:00', '5.3542'),
        ('2026-11-17T12:00:00+08:00', '12:00', '14:00', '2.6721'),
        ('2026-11-17T14:00:00+08:00', '14:00', '18:00', '5.3542'),
        ('2026-11-17T18:00:00+08:00', '18:00', '09:00', '2.6721'),
        ('2026-11-18T00:00:00+08:00', '18:00', '09:00', '2.6721'),
    ]
    for stamp, start, end, amount in cases:
        instant = datetime.fromisoformat(stamp).astimezone(timezone.utc)
        result = quote(rule, tokens, instant)
        assert result['time_band']['starts_at'] == start
        assert result['time_band']['ends_at'] == end
        assert Decimal(result['amount']) == Decimal(amount)

@pytest.mark.parametrize('test_mode',['true','false'])
def test_later_trial_rate_does_not_hide_effective_supplier_contract(harness, monkeypatch,test_mode):
    from control_plane.models import PurchaseRate
    client, _ = setup(harness, monkeypatch)
    monkeypatch.setenv('PLATFORM_COST_CONSOLE_TEST_MODE',test_mode)
    get_settings.cache_clear()
    with client.session_factory() as db:
        for trial, days in [(False, 2), (True, 1)]:
            card=rate(test_only=trial, effective_at=datetime.now(timezone.utc)-timedelta(days=days))
            db.add(PurchaseRate(**card, created_by='qa'))
            if not trial:
                expected=card['id']
        db.flush()
        row=import_receipt(db,event(),native_spend='0.000012')
        assert row.rate_id==expected and row.cost=='0.0000915'
        assert row.native_spend=='0.000012'


def test_estimate_preserves_missing_cache_facts_and_contract_price_stays_pending(harness,monkeypatch):
    client,headers=setup(harness,monkeypatch)
    e=event();e.update(usage_format='litellm')
    with client.session_factory() as db:
        row=import_receipt(db,e,native_spend='0.0123');db.commit()
    rule={'mode':'fixed','fixed':{'input_per_million':'1','output_per_million':'2',
        'cached_per_million':'0.1','cache_write_per_million':'1'}}
    assert client.post('/v1/admin/costs/rates',headers=headers,json=rate(pricing_rule=rule)).status_code==201
    with client.session_factory() as db:
        assert synchronize(db,client=Sink())['status']=='matched'
    item=client.get('/v1/admin/costs/report',headers=headers).json()['items'][0]
    assert item['cost']=='0.000165' and item['test_price'] is True
    assert item['tokens']['cache_write_tokens'] is None
    assert item['tokens']['cache_read_tokens'] is None
    assert len(item['pricing_details']['assumptions'])==2
    assert item['native_spend']=='0.0123'
