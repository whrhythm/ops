"""Administrative shadow accounting; no model dispatch or wallet mutation."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Literal
from uuid import UUID
import hmac

from fastapi import APIRouter, Depends, HTTPException, Query, Header
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from .database import get_db
from .model_usage import normalize_usage, retain_usage
from .models import AuditEvent, CostReceipt, CostSyncState, PurchaseRate, now
from .purchase_pricing import PricingRule, quote
from .supplier_statement import SupplierStatement, reconcile
from .settings import get_settings


def aware(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


class RateInput(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    id: UUID
    channel: str = Field(min_length=1, max_length=128, pattern=r'^[\w.:-]+$')
    model: str = Field(min_length=1, max_length=200, pattern=r'^[\w./:-]+$')
    currency: Literal['USD', 'CNY']
    input_per_million: Decimal = Field(ge=0, le=100000, decimal_places=6, allow_inf_nan=False)
    output_per_million: Decimal = Field(ge=0, le=100000, decimal_places=6, allow_inf_nan=False)
    cached_per_million: Decimal | None = Field(default=None, ge=0, le=100000, decimal_places=6, allow_inf_nan=False)
    effective_at: AwareDatetime
    note: str = Field(min_length=3, max_length=500)
    test_only: bool = False
    pricing_rule: PricingRule | None = None


class QuoteInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    receipt_id: UUID
    currency: Literal['USD', 'CNY']
    pricing_rule: PricingRule
    channel: str = Field(min_length=1, max_length=128)
    model: str = Field(min_length=1, max_length=200)


def rate_view(rate):
    return {key: getattr(rate, key) for key in (
        'id', 'channel', 'model', 'currency', 'input_per_million', 'output_per_million',
        'cached_per_million', 'effective_at', 'created_at', 'created_by', 'note', 'test_only', 'pricing_rule')}


def observed(row):
    return row.payload.get('usage_state') == 'observed'


def tokens_for(row):
    return normalize_usage(row.payload.get('usage', {}), row.payload.get('usage_format', 'litellm'))


def pricing_tokens(row, rate):
    tokens = report_tokens(row)
    assumptions = []
    # Estimates are a separate book. Never alter the provider's usage facts.
    if rate.test_only and tokens.get('input_tokens') is not None:
        tokens = dict(tokens)
        for field in ('cache_read_tokens', 'cache_write_tokens'):
            if tokens.get(field) is None:
                tokens[field] = 0
                assumptions.append(field + ': 未提供分类，相关输入暂按普通输入估算；非供应商实付')
        tokens['uncached_input_tokens'] = (tokens['input_tokens'] - tokens['cache_read_tokens']
                                           - tokens['cache_write_tokens'])
    return tokens, assumptions


def pricing_details(row, rate):
    tokens, assumptions = pricing_tokens(row, rate)
    return {**quote(PricingRule.model_validate(rate.pricing_rule), tokens, aware(row.occurred_at)),
            'assumptions': assumptions}


def price_receipt(db, row):
    """Bind once. A later price never rewrites a priced receipt."""
    if row.rate_id or not observed(row):
        return
    eligible = select(PurchaseRate).where(
        PurchaseRate.channel == row.channel, PurchaseRate.model == row.model,
        PurchaseRate.effective_at <= row.occurred_at,
    )
    if not get_settings().cost_console_test_mode:
        eligible = eligible.where(PurchaseRate.test_only.is_(False))
    # An estimate may fill a missing contract, but must never supersede one.
    rate = db.scalars(eligible.order_by(PurchaseRate.test_only.asc(),
                                      PurchaseRate.effective_at.desc()).limit(1)).first()
    if rate is None:
        return
    if rate.pricing_rule is not None:
        result = pricing_details(row, rate)
        if result['amount'] is not None:
            row.cost, row.currency, row.rate_id = result['amount'], rate.currency, rate.id
        return
    usage = row.payload['usage']
    if row.payload.get('usage_format'):
        tokens = tokens_for(row)
        # The legacy fixed-price card cannot price cache writes or unknown classifications.
        if tokens['cache_write_tokens'] != 0 or tokens['uncached_input_tokens'] is None:
            return
        ordinary, cached, output = (tokens[k] for k in ('uncached_input_tokens', 'cache_read_tokens', 'output_tokens'))
    else:
        cached = usage.get('prompt_tokens_details', {}).get('cached_tokens', 0)
        ordinary, output = usage['prompt_tokens'] - cached, usage['completion_tokens']
    if ordinary < 0 or (cached and rate.cached_per_million is None):
        return  # Missing cache price is not a zero price or an input-price fallback.
    value = (Decimal(rate.input_per_million) * ordinary
             + Decimal(rate.output_per_million) * output
             + Decimal(rate.cached_per_million or '0') * cached) / Decimal(1000000)
    row.cost, row.currency, row.rate_id = str(value), rate.currency, rate.id


def usage_is_superset(before, after):
    return all(key in after and (
        usage_is_superset(value, after[key]) if isinstance(value, dict) and isinstance(after[key], dict)
        else value == after[key]
    ) for key, value in before.items())


def completes_pending_receipt(existing, incoming):
    old = existing.payload
    if not old.get('usage_format') or observed(existing) or existing.usage_sent_at or existing.cost_sent_at:
        return False
    late = {'usage', 'usage_state', 'status', 'finished_at', 'response_id', 'provider_request_id'}
    if {key: val for key, val in old.items() if key not in late} != {
        key: val for key, val in incoming.items() if key not in late
    }:
        return False
    if any(old.get(key) is not None and old[key] != incoming.get(key)
           for key in ('response_id', 'provider_request_id')):
        return False
    return incoming['usage_state'] == 'observed' and usage_is_superset(old['usage'], incoming['usage'])


def import_receipt(db, event, native_spend=None):
    """Internal trusted ingestion. No browser import endpoint. No prompt/body retained."""
    allowed = ('schema', 'event_id', 'attempt_id', 'call_id', 'owner_id', 'ordinal', 'channel',
               'dispatch', 'requested_model', 'model', 'stream', 'started_at', 'finished_at',
               'http_status', 'status', 'provider_request_id', 'response_id', 'usage', 'usage_state', 'usage_format',
               'observation_scope')
    payload = {key: event[key] for key in allowed if key in event}
    UUID(payload['event_id'])
    if 'usage_format' in payload:
        payload['usage'] = retain_usage(payload.get('usage'))
        tokens = normalize_usage(payload['usage'], payload['usage_format'])
        payload['usage_state'] = ('observed' if tokens['input_tokens'] is not None
                                  and tokens['output_tokens'] is not None else 'pending')
    elif payload.get('usage_state') == 'observed':
        u = payload['usage']
        if any(type(u.get(k)) is not int or not 0 <= u[k] <= 10**9 for k in ('prompt_tokens', 'completion_tokens')):
            raise ValueError('invalid_confirmed_usage')
        if 'total_tokens' in u and u['total_tokens'] != u['prompt_tokens'] + u['completion_tokens']:
            raise ValueError('usage_total_mismatch')
    existing = db.get(CostReceipt, payload['event_id'], with_for_update=True)
    amount = None if native_spend is None else str(Decimal(str(native_spend)))
    if existing:
        if amount is not None and existing.native_spend not in (None, amount):
            raise ValueError('immutable_receipt_conflict')
        if existing.payload != payload:
            if not completes_pending_receipt(existing, payload):
                raise ValueError('immutable_receipt_conflict')
            existing.payload = payload
            price_receipt(db, existing)
        if amount is not None:
            existing.native_spend = amount  # One-way enrichment from separately verified native receipt.
        return existing
    row = CostReceipt(id=payload['event_id'], owner_id=payload['owner_id'], channel=payload['channel'],
                      model=payload.get('model') or payload['requested_model'],
                      occurred_at=datetime.fromisoformat(payload['started_at'].replace('Z','+00:00')), payload=payload,
                      native_spend=amount)
    db.add(row)
    db.flush()
    price_receipt(db, row)
    return row


def receipt_view(row, rate):
    return {'id': row.id, 'owner_id': row.owner_id, 'channel': row.channel, 'model': row.model,
            'occurred_at': row.occurred_at, 'call_id': row.payload['call_id'],
            'response_id': row.payload.get('response_id'), 'ordinal': row.payload.get('ordinal', 1),
            'usage': retain_usage(row.payload.get('usage')), 'usage_state': row.payload.get('usage_state'),
            'tokens': report_tokens(row), 'usage_format': row.payload.get('usage_format', 'legacy'),
            'observation_scope': row.payload.get('observation_scope'),
            'status': row.payload.get('status'), 'native_spend': row.native_spend,
            'cost': row.cost, 'currency': row.currency, 'rate_id': row.rate_id,
            'test_price': rate.test_only if rate else None,
            'pricing_details': pricing_details(row, rate)
                if rate and rate.pricing_rule is not None else None,
            'usage_sent_at': row.usage_sent_at,
            'cost_sent_at': row.cost_sent_at, 'last_error': row.last_error}


def report_tokens(row):
    try:
        return tokens_for(row)
    except ValueError:
        # Historical receipts predate strict category validation. Retain their raw fields for audit.
        return {}


def create_router(admin):
    router = APIRouter(prefix='/v1/admin/costs', tags=['shadow cost console'])

    def enabled(principal=Depends(admin)):
        if not get_settings().cost_console_enabled:
            raise HTTPException(404, '费用后台尚未启用')
        return principal

    @router.post('/supplier-statement/compare')
    def compare_statement(body: SupplierStatement, principal=Depends(enabled), db=Depends(get_db)):
        rows = db.execute(select(CostReceipt, PurchaseRate.test_only)
                          .outerjoin(PurchaseRate, PurchaseRate.id == CostReceipt.rate_id)
                          .where(CostReceipt.owner_id == body.owner_id, CostReceipt.channel == body.channel,
                                 CostReceipt.model == body.model, CostReceipt.occurred_at >= body.start,
                                 CostReceipt.occurred_at < body.end)
                          .order_by(CostReceipt.occurred_at, CostReceipt.id).limit(5001)).all()
        if len(rows) > 5000:
            raise HTTPException(422, '请缩短账单核对时间范围')
        return reconcile(body, rows)

    @router.get('/rates')
    def rates(principal=Depends(enabled), db=Depends(get_db)):
        return {'test_mode': get_settings().cost_console_test_mode, 'items': [rate_view(r) for r in
                db.scalars(select(PurchaseRate).order_by(PurchaseRate.created_at.desc()).limit(500))]}

    @router.post('/rates', status_code=201)
    def create_rate(body: RateInput, principal=Depends(enabled), db=Depends(get_db)):
        values = body.model_dump(mode='json')
        values['effective_at'] = body.effective_at.astimezone(timezone.utc)
        existing = db.get(PurchaseRate, str(body.id))
        if existing:
            stored = {key: getattr(existing, key) for key in values}
            stored['effective_at'] = aware(stored['effective_at'])
            if stored['pricing_rule'] is not None:
                stored['pricing_rule'] = PricingRule.model_validate(stored['pricing_rule']).model_dump(mode='json')
            match = stored == values
            if not match:
                raise HTTPException(409, '此保存编号已对应另一份费率')
            return rate_view(existing)
        if body.test_only and not get_settings().cost_console_test_mode:
            raise HTTPException(422, '正式环境不能创建测试费率')
        if not body.test_only and body.effective_at < datetime.now(timezone.utc):
            raise HTTPException(422, '正式费率须设置未来生效时间；历史更正需独立审计')
        rate = PurchaseRate(**values, created_by=principal.subject)
        db.add(rate)
        db.add(AuditEvent(tenant_id='platform', issuer=principal.issuer, subject=principal.subject,
                          action='costs.rate.create', target_id=rate.id))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(409, '同一渠道和模型的生效时间已存在，请刷新核对')
        return rate_view(rate)

    @router.post('/rates/quote')
    def quote_receipt(body: QuoteInput, principal=Depends(enabled), db=Depends(get_db)):
        row = db.get(CostReceipt, str(body.receipt_id))
        if row is None:
            raise HTTPException(404, '该调用回执不存在')
        if (row.channel, row.model) != (body.channel, body.model):
            raise HTTPException(409, '试算回执的渠道和实际模型必须与费率一致')
        return {'receipt_id': row.id, 'currency': body.currency, 'usage': report_tokens(row),
                **quote(body.pricing_rule, report_tokens(row), aware(row.occurred_at))}

    @router.get('/report')
    def report(days: int = Query(default=30, ge=1, le=366), channel: str = Query(default='', max_length=128),
               model: str = Query(default='', max_length=200), page: int = Query(default=1, ge=1, le=100000),
               principal=Depends(enabled), db=Depends(get_db)):
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        stmt = select(CostReceipt).where(CostReceipt.occurred_at >= cutoff)
        if channel:
            stmt = stmt.where(CostReceipt.channel == channel)
        if model:
            stmt = stmt.where(CostReceipt.model == model)
        # Bound synchronous reporting; require narrower filters instead of silently truncating totals.
        count = db.scalar(select(func.count()).select_from(stmt.subquery()))
        if count > 10000:
            raise HTTPException(422, '此范围超过一万条，请缩小时间或模型范围')
        rows = list(db.scalars(stmt.order_by(CostReceipt.occurred_at.desc(), CostReceipt.id)))
        rates_by_id = {r.id: r for r in db.scalars(select(PurchaseRate))}
        totals = {'receipts': len(rows), 'prompt_tokens': 0, 'completion_tokens': 0,
                  'pending_price': 0, 'pending_usage': 0, 'pending_sync': 0,
                  'native_spend_usd': '0', 'native_unmatched': 0, 'costs': {}, 'test_costs': {},
                  'known_usage_sent': 0, 'known_usage': 0, 'classified_input': 0}
        native = Decimal(0)
        series = {}
        issues = []
        for r in rows:
            tokens = report_tokens(r)
            input_total = tokens.get('input_tokens') or 0
            output_total = tokens.get('output_tokens') or 0
            totals['prompt_tokens'] += input_total
            totals['completion_tokens'] += output_total
            date = (aware(r.occurred_at) + timedelta(hours=8)).date().isoformat()
            daily = series.setdefault(date, {'date': date, 'tokens': 0, 'calls': 0})
            daily['tokens'] += input_total + output_total
            daily['calls'] += 1
            if r.native_spend is not None:
                native += Decimal(r.native_spend)
            else:
                totals['native_unmatched'] += 1
            if r.cost is not None:
                bucket = 'test_costs' if rates_by_id[r.rate_id].test_only else 'costs'
                totals[bucket][r.currency] = str(Decimal(totals[bucket].get(r.currency, '0')) + Decimal(r.cost))
            reasons = []
            if not observed(r):
                totals['pending_usage'] += 1; reasons.append('用量待核验')
            else:
                totals['known_usage'] += 1
                totals['known_usage_sent'] += int(r.usage_sent_at is not None)
                if all(tokens.get(key) is not None for key in ('uncached_input_tokens', 'cache_read_tokens', 'cache_write_tokens')):
                    totals['classified_input'] += 1
                elif r.payload.get('usage_format'):
                    reasons.append('输入分类未齐，缺项保留为未知')
                if r.cost is None:
                    totals['pending_price'] += 1; reasons.append('采购价或计价所需用量待核对')
            if observed(r) and (not r.usage_sent_at or (r.cost is not None and not r.cost_sent_at)):
                totals['pending_sync'] += 1; reasons.append('等待同步')
            if r.last_error:
                reasons.append(r.last_error)
            if r.native_spend is None:
                reasons.append('原扣费明细待核验')
            if reasons:
                issues.append({'id': r.id, 'model': r.model, 'channel': r.channel, 'reasons': reasons})
        totals['native_spend_usd'] = str(native)
        sync = db.get(CostSyncState, 1)
        from .cost_native_audit import STATE_ID, audit_view
        return {'mode': 'shadow', 'test_mode': get_settings().cost_console_test_mode,
                'as_of': now(), 'totals': totals, 'series': sorted(series.values(), key=lambda x: x['date']),
                'page': page, 'page_size': 25, 'items': [receipt_view(r, rates_by_id.get(r.rate_id))
                for r in rows[(page-1)*25:page*25]], 'issues': issues[:100], 'issue_count': len(issues),
                'sync': {'checked_at': sync.checked_at, **sync.report} if sync else None,
                'native_audit': audit_view(db.get(CostSyncState, STATE_ID))}

    @router.post('/sync')
    def sync(principal=Depends(enabled), db=Depends(get_db)):
        from .cost_meter_worker import synchronize
        # No input URL, token or arbitrary event from the browser.
        result = synchronize(db, limit=25)
        db.add(AuditEvent(tenant_id='platform', issuer=principal.issuer, subject=principal.subject,
                          action='costs.sync', target_id='openmeter-shadow'))
        db.commit()
        return result

    return router


class ReceiptInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    event: dict


def ingestion_router():
    router = APIRouter()

    @router.post('/internal/v1/metering/receipts', status_code=202)
    def ingest(body: ReceiptInput, x_meter_token: str = Header(default=''), db=Depends(get_db)):
        settings = get_settings()
        if not settings.cost_console_enabled or not settings.cost_ingest_token:
            raise HTTPException(404)
        if not hmac.compare_digest(x_meter_token, settings.cost_ingest_token):
            raise HTTPException(403)
        import json
        if len(json.dumps(body.event)) > 16000:
            raise HTTPException(422, 'receipt_too_large')
        try:
            # This intake never asserts a native debit amount. Native reconciliation is separate.
            row = import_receipt(db, body.event)
            db.commit()
        except (ValueError, KeyError, TypeError, IntegrityError):
            db.rollback()
            raise HTTPException(409, 'receipt_invalid_or_conflicting')
        return {'event_id': row.id, 'accepted': True}

    return router
