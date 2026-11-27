"""Simulated fixed-token grants and receipt-based allocation between daily and persistent tokens."""
from uuid import NAMESPACE_URL, uuid5
from fastapi import HTTPException
from sqlalchemy import case, func, select

from .models import AuditEvent, DailyFreeCall as Call, ModelCallReservation as Reservation

PACK = 100_000
GRANT = 'billing.simulated_token_refill.100000.v1'


def allocations(db, env, day=None):
    free = Reservation.snapshot['daily_token_share'].as_integer()
    actual = case((Call.state == 'settled', Call.input_tokens + Call.output_tokens),
                  else_=Call.input_bound + Call.output_bound)
    portion = case((actual > free, actual - free), else_=0)
    query = select(Call.state, func.sum(portion)).join(
        Reservation, (Reservation.id == Call.attempt_id) &
        (Reservation.environment_id == Call.environment_id)
    ).where(Call.environment_id == env.id, Call.state != 'released', free.is_not(None))
    if day is not None:
        query = query.where(Call.day == day)
    totals = dict(db.execute(query.group_by(Call.state)).all())
    return int(totals.get('settled', 0)), int(totals.get('dispatched', 0) + totals.get('reserved', 0))


def view(db, env, day, settings):
    grants = select(AuditEvent).where(AuditEvent.action == GRANT, AuditEvent.target_id == env.id)
    total = db.scalar(select(func.count()).select_from(grants.subquery())) * PACK
    used, held = allocations(db, env) if total else (0, 0)
    month = day[:7]
    grant_id = str(uuid5(NAMESPACE_URL, GRANT + ':' + env.id + ':' + month))
    available = (settings.xunhupay_simulator_enabled and settings.xunhupay_simulator_account in ('',env.id)
                 and settings.simulator_orders_monthly > 0 and env.status in ('ready','requested','provisioning'))
    return dict(remaining=max(0,total-used-held), used=used, reserved=held, pack=PACK,
                month=month, can_refill=available and db.get(AuditEvent, grant_id) is None)


def refill(db, env, body, settings):
    from .daily_free import billing_day
    from .models import now
    db.refresh(env, with_for_update=True)
    day = billing_day(now())
    current = view(db, env, day, settings)
    if (body.get('confirm') is not True or body.get('month') != current['month']
            or body.get('tokens') != PACK):
        raise HTTPException(409, 'refill_confirmation_changed')
    key = str(uuid5(NAMESPACE_URL, GRANT + ':' + env.id + ':' + current['month']))
    if not db.get(AuditEvent, key):
        if not current['can_refill']:
            raise HTTPException(403, 'refill_unavailable')
        db.add(AuditEvent(id=key, tenant_id=env.first_tenant_id, issuer=env.issuer,
                          subject=env.subject, action=GRANT, target_id=env.id))
    db.commit()
    return {'receipt': key, 'tokens': PACK, 'month': current['month']}


def reserve_remainder(db, env, key, free_before, bound, day, settings):
    if bound <= free_before:
        return
    if getattr(settings,'token_refill_enabled',False):
        credit = view(db, env, day, settings)
        row = db.get(Reservation, key)
        if (bound-free_before <= credit['remaining'] and row and row.environment_id == env.id
                and row.state == 'reserved' and row.snapshot.get('funding') == 'free'):
            row.snapshot = {**row.snapshot, 'daily_token_share':free_before}
            return
    raise HTTPException(409, 'free_allowance_insufficient')
