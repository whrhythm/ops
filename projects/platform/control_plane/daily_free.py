"""Daily token accounting for the sponsored model dispatcher. Paid wallets are independent."""
from datetime import timedelta, timezone
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import func, select

from .model_usage import normalize_usage
from .models import DailyFreeBucket, DailyFreeCall, now

BEIJING = timezone(timedelta(hours=8))


def billing_day(stamp):
    if stamp.tzinfo is None:
        raise ValueError('A timezone-aware admission time is required')
    return stamp.astimezone(BEIJING).date().isoformat()


def day_view(db, env, day, allowance):
    bucket = db.get(DailyFreeBucket, (env.id, day))
    limit = bucket.allowance if bucket else allowance
    sums = db.execute(select(
        DailyFreeCall.state, func.sum(DailyFreeCall.input_bound + DailyFreeCall.output_bound),
        func.sum(DailyFreeCall.input_tokens + DailyFreeCall.output_tokens)
    ).where(DailyFreeCall.environment_id == env.id, DailyFreeCall.day == day).group_by(DailyFreeCall.state))
    held = used = 0
    for state, bounds, tokens in sums:
        if state in ('reserved', 'dispatched'):
            held += int(bounds)
        elif state == 'settled':
            used += int(tokens)
    from .token_refill import allocations
    purchased_used, purchased_held = allocations(db, env, day)
    used -= purchased_used
    held -= purchased_held
    return {'day': day, 'timezone': 'Asia/Shanghai', 'limit': limit, 'reserved': held,
            'used': used, 'remaining': max(0, limit - held - used)}


def reserve(db, env, *, attempt_id: UUID, model, input_bound, output_bound, settings, stamp=None):
    """Only trusted pre-dispatch callers supply tokenized input and the enforced output bound."""
    db.refresh(env, with_for_update=True)
    key = str(attempt_id)
    call = db.get(DailyFreeCall, (env.id, key), populate_existing=True)
    if call:
        if (call.model, call.input_bound, call.output_bound) != (model, input_bound, output_bound):
            raise HTTPException(409, 'free_attempt_conflict')
        return call
    if env.status not in ('requested', 'provisioning', 'ready') or not settings.daily_free_tokens or model not in settings.daily_free_models:
        raise HTTPException(403, 'free_model_unavailable')
    if not (type(input_bound) is int and type(output_bound) is int and input_bound >= 0 and output_bound > 0):
        raise ValueError('invalid_token_bound')
    stamp = stamp or now()
    day = billing_day(stamp)
    status = day_view(db, env, day, settings.daily_free_tokens)
    from .token_refill import reserve_remainder
    reserve_remainder(db, env, key, status['remaining'], input_bound + output_bound, day, settings)
    if db.get(DailyFreeBucket, (env.id, day)) is None:
        db.add(DailyFreeBucket(environment_id=env.id, day=day, allowance=settings.daily_free_tokens))
        db.flush()
    call = DailyFreeCall(environment_id=env.id, attempt_id=key, day=day, model=model,
                         input_bound=input_bound, output_bound=output_bound, state='reserved', created_at=stamp)
    db.add(call)
    db.flush()
    return call


def locked_call(db, env, attempt_id):
    db.refresh(env, with_for_update=True)
    call = db.get(DailyFreeCall, (env.id, str(attempt_id)), populate_existing=True)
    if call is None:
        raise HTTPException(404, 'free_attempt_missing')
    return call


def claim_dispatch(db, env, attempt_id):
    """Commit this claim before sending. A lost acknowledgement must be reconciled."""
    call = locked_call(db, env, attempt_id)
    if call.state != 'reserved':
        return False
    if env.status not in ('requested', 'provisioning', 'ready'):
        raise HTTPException(403, 'free_model_unavailable')
    call.state = 'dispatched'
    db.flush()
    return True


def release_unsent(db, env, attempt_id):
    call = locked_call(db, env, attempt_id)
    if call.state not in ('reserved', 'released'):
        raise HTTPException(409, 'free_attempt_requires_receipt')
    call.state = 'released'
    db.flush()


def settle(db, env, attempt_id, usage, source_format):
    """Dispatcher-only: use the final provider receipt, including billable failed attempts."""
    call = locked_call(db, env, attempt_id)
    if call.state not in ('dispatched', 'settled'):
        raise HTTPException(409, 'free_attempt_not_dispatched')
    tokens = normalize_usage(usage, source_format)
    inp, out = tokens['input_tokens'], tokens['output_tokens']
    if inp is None or out is None:
        return call
    if call.state == 'settled' and (call.input_tokens, call.output_tokens) != (inp, out):
        raise HTTPException(409, 'free_receipt_conflict')
    call.input_tokens, call.output_tokens, call.state = inp, out, 'settled'
    db.flush()
    return call


def trial_open(settings, stamp):
    if getattr(settings,'daily_free_all_accounts',False):
        return bool(settings.daily_free_tokens and settings.daily_free_models and settings.daily_free_sponsor_owner
                    and settings.daily_free_sponsor_key.get_secret_value())
    start=settings.daily_free_starts_at
    end=settings.daily_free_ends_at
    return bool(settings.daily_free_account and settings.daily_free_tokens and settings.daily_free_models
                and start and end and start <= stamp < end)


def account_enabled(settings,env):
    return bool(getattr(settings,'daily_free_all_accounts',False) or env.id==settings.daily_free_account)


def visible_allowance(db, env, settings):
    stamp=now()
    if not account_enabled(settings,env) or not trial_open(settings,stamp):
        return {}
    day=billing_day(stamp)
    view=day_view(db,env,day,settings.daily_free_tokens)
    if getattr(settings,'managed_chat_budget_enabled',False):
        from sqlalchemy import case, or_
        from .daily_chat import consent_recorded, checkout_identity
        from .models import ModelCallReservation as R, CapacityOrder
        from .model_call_guard import nanos
        import hmac
        outstanding=('reserved','dispatched','blocked')
        held,blocked=db.execute(select(
            func.sum(case((R.state=='settled',R.settled_nanos),else_=R.reserved_nanos)),
            func.count(case((R.state=='blocked',1)))
        ).where(R.state!='released', or_(R.window_id=='free:'+day,
            R.state.in_(outstanding) & (R.snapshot['funding'].as_string()=='free')))).one()
        order=db.scalar(select(CapacityOrder).where(CapacityOrder.environment_id==env.id,
            CapacityOrder.idempotency_key.startswith('daily-plan-'+day+'-')).order_by(CapacityOrder.created_at.desc()).limit(1))
        prepaid=bool(order and order.status=='ready' and order.payment_status=='confirmed' and
            hmac.compare_digest(order.idempotency_key,checkout_identity(env,day,order.plan,settings)[0]))
        view.update(balance_authorized=prepaid or consent_recorded(db,env,day),
                    pool_paused=bool(blocked or (held or 0)>=nanos(settings.managed_chat_free_daily_usd)))
    if getattr(settings,'token_refill_enabled',False):
        from .token_refill import view as refill_view
        view['refill'] = refill_view(db,env,day,settings)
    return {'daily_free': {**view,'models': list(settings.daily_free_models)}}
