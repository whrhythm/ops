from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4
import pytest
from fastapi import HTTPException

from control_plane import daily_free, token_refill
from control_plane.models import ModelCallReservation, now
from test_daily_free import owner

def test_atomic_refill_split_settlement_and_carryover(harness):
    client, _ = harness
    settings = SimpleNamespace(daily_free_tokens=100,daily_free_models=('demo',), token_refill_enabled=True,
                               xunhupay_simulator_enabled=True,xunhupay_simulator_account='',simulator_orders_monthly=1)
    stamp = now()
    day = daily_free.billing_day(stamp)
    body = dict(confirm=True,month=day[:7],tokens=token_refill.PACK)
    with client.session_factory() as db:
        env, other = owner(db), owner(db)
        with pytest.raises(HTTPException): token_refill.refill(db,env,{**body,'tokens':999999},settings)
        assert token_refill.refill(db,env,body,settings) == token_refill.refill(db,env,body,settings)
        assert token_refill.view(db,other,day,settings)['remaining']==0
        a=uuid4()
        db.add(ModelCallReservation(id=str(a),environment_id=env.id,model='demo',key_hash='x'*64,state='reserved',
            window_id='free:'+day,reserved_nanos=10,limit_nanos=100,snapshot={'funding':'free'}))
        db.flush()
        daily_free.reserve(db,env,attempt_id=a,model='demo',input_bound=60,output_bound=80,settings=settings,stamp=stamp)
        db.commit()
        assert token_refill.view(db,env,day,settings)['reserved']==40
        assert daily_free.day_view(db,env,day,100)['reserved']==100
        daily_free.claim_dispatch(db,env,a)
        for _ in range(2):
            daily_free.settle(db,env,a,{'prompt_tokens':60,'completion_tokens':52},'litellm')
        db.commit()
        tomorrow = daily_free.billing_day(stamp+timedelta(days=1))
        assert daily_free.day_view(db,env,day,100)['used']==100
        assert daily_free.day_view(db,env,tomorrow,100)['remaining']==100
        assert token_refill.view(db,env,tomorrow,settings)['remaining']==token_refill.PACK-12
        assert not token_refill.view(db,env,day,settings)['can_refill']
