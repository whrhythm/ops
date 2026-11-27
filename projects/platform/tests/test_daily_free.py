from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from threading import Barrier
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select, func
from sqlalchemy.orm import sessionmaker

from control_plane import daily_free as free
from control_plane.models import Tenant, UserEnvironment, DailyFreeCall
from alembic import command
from alembic.config import Config
from pathlib import Path
import os
from sqlalchemy.engine import make_url
from control_plane.database import make_engine
from control_plane.settings import get_settings

policy = SimpleNamespace(daily_free_tokens=100, daily_free_models=('test-model',))
stamp = datetime(2026, 11, 23, 15, 59, 59, tzinfo=timezone.utc)


def owner(db):
    subject = str(uuid4())
    t = Tenant(name='Local billing test', owner_issuer='test', owner_subject=subject, project_limit=1)
    db.add(t)
    db.flush()
    env = UserEnvironment(issuer='test', subject=subject, identity_hash=uuid4().hex, storage_gib=1,
                          status='ready', first_tenant_id=t.id)
    db.add(env)
    db.commit()
    return env


def reserve(db, env, key, **kw):
    params=dict(attempt_id=key, model='test-model', input_bound=30, output_bound=50,
                settings=policy, stamp=stamp)
    return free.reserve(db, env, **{**params, **kw})


def test_retry_disconnect_receipt_and_midnight(harness):
    client, _ = harness
    with client.session_factory() as db:
        env = owner(db)
        a, b = uuid4(), uuid4()
        assert reserve(db, env, a).state == 'reserved'
        db.commit()
        assert reserve(db, env, a).day == '2026-11-23'
        with pytest.raises(HTTPException, match='free_attempt_conflict'):
            reserve(db, env, a, output_bound=49)
        assert free.claim_dispatch(db, env, a)
        db.commit()
        assert not free.claim_dispatch(db, env, a)
        with pytest.raises(HTTPException, match='requires_receipt'):
            free.release_unsent(db, env, a)
        assert free.settle(db, env, a, {}, 'litellm').state == 'dispatched'
        with pytest.raises(HTTPException, match='insufficient'):
            reserve(db, env, b)
        tomorrow = stamp + timedelta(seconds=1)
        assert reserve(db, env, b, stamp=tomorrow).day == '2026-11-24'
        assert reserve(db, env, a, stamp=tomorrow).day == '2026-11-23'
        u = {'prompt_tokens': 27, 'prompt_cache_hit_tokens': 20, 'completion_tokens': 7}
        for _ in range(2):
            free.settle(db, env, a, u, 'litellm')
        with pytest.raises(HTTPException, match='conflict'):
            free.settle(db, env, a, {**u, 'completion_tokens': 8}, 'litellm')
        old = free.day_view(db, env, '2026-11-23', 200)
        assert (old['used'], old['remaining'], old['reserved'], old['limit']) == (34,66,0,100)
        assert free.day_view(db, env, '2026-11-24', 100)['remaining'] == 20
        free.release_unsent(db, env, b)
        free.release_unsent(db, env, b)
        assert not free.claim_dispatch(db, env, b)
        assert free.day_view(db, env, '2026-11-24', 100)['remaining'] == 100


def test_no_default_paid_fallback_or_foreign_owner(harness):
    client, _ = harness
    with client.session_factory() as db:
        env, other = owner(db), owner(db)
        a = uuid4()
        with pytest.raises(HTTPException, match='unavailable'):
            reserve(db, env, a, model='other-model')
        reserve(db, env, a, output_bound=70)
        with pytest.raises(HTTPException, match='insufficient'):
            reserve(db, env, uuid4(), input_bound=0, output_bound=1)
        with pytest.raises(HTTPException, match='missing'):
            free.claim_dispatch(db, other, a)
        assert db.scalar(select(func.count()).select_from(DailyFreeCall)) == 1


def test_overrun_records_actual_and_blocks_more(harness):
    client, _ = harness
    with client.session_factory() as db:
        env, a = owner(db), uuid4()
        reserve(db, env, a)
        free.claim_dispatch(db, env, a)
        free.settle(db, env, a, {'prompt_tokens':102,'completion_tokens':5}, 'litellm')
        view=free.day_view(db, env, '2026-11-23', 100)
        assert view['used']==107 and view['remaining']==0


def test_postgres_parallel_last_grant(monkeypatch):
    dsn = os.environ.get('PLATFORM_TEST_DATABASE_URL')
    if not dsn:
        pytest.skip('Requires disposable local PostgreSQL')
    url = make_url(dsn)
    assert url.host in ('localhost','127.0.0.1') and url.database.startswith('platform_test_')
    monkeypatch.setenv('PLATFORM_DATABASE_URL',dsn)
    get_settings.cache_clear()
    cfg = Config(str(Path(__file__).resolve().parents[1]/'alembic.ini'))
    command.upgrade(cfg,'head')
    engine = make_engine(dsn)
    sessions = sessionmaker(engine)
    with sessions() as db:
        env_id = owner(db).id
    barrier = Barrier(2)
    def run(_):
        with sessions() as db:
            env = db.get(UserEnvironment, env_id)
            barrier.wait(timeout=10)
            try:
                reserve(db, env, uuid4())
                db.commit()
                return 'reserved'
            except HTTPException as e:
                return e.detail
    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(run, range(2))) == ['free_allowance_insufficient', 'reserved']
    engine.dispose()
    get_settings.cache_clear()


def test_usage_panel_consent_is_idempotent_scoped_and_expires(harness, monkeypatch):
    from control_plane import daily_chat
    from control_plane.models import AuditEvent, ChatBalanceConsent, ModelCallReservation
    from pydantic import SecretStr
    client, _ = harness
    monkeypatch.setattr(free, 'now', lambda: stamp)
    monkeypatch.setattr(daily_chat, 'now', lambda: stamp)
    settings = SimpleNamespace(**vars(policy), daily_free_all_accounts=True, managed_chat_budget_enabled=True,
                               daily_free_sponsor_owner='sponsor', daily_free_sponsor_key=SecretStr('test'),
                               managed_chat_free_daily_usd='1')
    with client.session_factory() as db:
        a, b = owner(db), owner(db)
        view = lambda env: free.visible_allowance(db, env, settings)['daily_free']
        initial = view(a)
        assert not initial['balance_authorized'] and not initial['pool_paused']
        for _ in range(2):
            daily_chat.record_balance_consent(db, a, initial['day'])
        assert view(a) == {**initial, 'balance_authorized':True}
        assert not view(b)['balance_authorized']
        assert db.scalar(select(func.count()).select_from(ChatBalanceConsent)) == 1
        assert db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action=='billing.balance_continuation_consent')) == 1
        assert db.scalar(select(func.count()).select_from(ModelCallReservation)) == 0
        tomorrow = stamp + timedelta(seconds=1)
        monkeypatch.setattr(free, 'now', lambda: tomorrow)
        monkeypatch.setattr(daily_chat, 'now', lambda: tomorrow)
        assert not view(a)['balance_authorized']
        with pytest.raises(HTTPException, match='billing_day_changed'):
            daily_chat.record_balance_consent(db, a, initial['day'])
