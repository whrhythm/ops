from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

from control_plane.supplier_statement import InvoiceLine, SupplierStatement, reconcile


def line(ref, **changes):
    return InvoiceLine(**{'reference':ref,'input_tokens':15,'output_tokens':2,'cache_read_tokens':5,
                          'amount':'0.00001','currency':'USD',**changes})


def receipt(ref, **changes):
    return SimpleNamespace(**{'id':str(uuid4()),'payload':{'response_id':ref,'usage_format':'litellm',
        'usage':{'prompt_tokens':15,'completion_tokens':2,'prompt_tokens_details':{'cached_tokens':5}}},
        'native_spend':'0.000018','cost':'0.000010','currency':'USD','rate_id':'contract',**changes})


def check(lines, rows):
    start=datetime.now(timezone.utc)-timedelta(hours=1)
    return reconcile(SupplierStatement(owner_id='qa',model='model',channel='supplier',
        start=start,end=start+timedelta(hours=1),reference_field='response_id',lines=lines), rows)


def test_actual_supplier_amount_compares_with_procurement_and_retains_separate_sale():
    r=check([line('x')],[(receipt('x'),False)])
    assert r['matched']==1 and r['status']=='matched'
    assert Decimal(r['details'][0]['sale_original'])!=Decimal(r['details'][0]['supplier_amount'])


def test_missing_duplicate_usage_currency_and_trial_cost_are_visible():
    lines=[line(x) for x in ('a','b','b','c','d','e','f','g')]
    rows=[(receipt('a',cost=None),False),(receipt('b'),False),(receipt('d',currency='CNY'),False),
          (receipt('e'),True),(receipt('f',cost='0.000015'),False),(receipt('g'),False),
          (receipt('h'),False),(receipt(None),False)]
    lines[-1]=line('g',input_tokens=19,cache_read_tokens=2)
    r=check(lines,rows)
    assert r['status']=='differences' and not r['matched']
    reasons={reason for detail in r['details'] for reason in detail['issues']}
    assert reasons=={'missing_supplier_reference','missing_contract_price','duplicate_supplier_line',
                     'missing_internal_receipt','purchase_currency_mismatch','purchase_amount_mismatch',
                     'input_tokens_mismatch','cache_read_tokens_mismatch','missing_supplier_line'}


def test_empty_statement_is_not_verified():
    assert check([],[])['status']=='empty'


def test_admin_gate_and_bounded_internal_scope(harness, monkeypatch):
    from test_cost_console import setup
    client, headers = setup(harness, monkeypatch)
    end = datetime.now(timezone.utc)
    body = dict(channel='supplier', owner_id='qa-account', model='model', start=(end-timedelta(hours=1)).isoformat(),
                end=end.isoformat(), reference_field='response_id', lines=[])
    path = '/v1/admin/costs/supplier-statement/compare'
    assert client.post(path,json=body).status_code in (401,403)
    assert client.post(path,json=body,headers=headers).json()==dict(status='empty',matched=0,exceptions=0,details=[])
    assert client.post(path,json={**body,'start':(end-timedelta(days=32)).isoformat()},headers=headers).status_code==422
