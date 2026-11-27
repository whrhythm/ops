"""Actual existing gateway + local HTTP supplier simulation, no paid calls."""
import json
import os
from datetime import timedelta

import certifi
import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from control_plane import programming
from control_plane.litellm import model_user_id
from control_plane.models import ModelCallReservation, now
from control_plane.settings import Settings
from test_daily_free import owner
from test_model_call_guard import body, settings, tools


@pytest.mark.parametrize('stream,scenario', [(False,'normal'), (True,'normal'), (True,'no_usage'), (True,'provider_failure')])
def test_real_http_dispatch_receipts_and_terminal_stop(monkeypatch, harness, stream, scenario):
    url = os.environ.get('PLATFORM_MODEL_SIMULATION_URL')
    if not url:
        pytest.skip('Start the repository Postman simulation and set PLATFORM_MODEL_SIMULATION_URL')
    with httpx.Client(trust_env=False) as control:
        assert control.post(url+'/__model_reset').status_code == 200
    local, _ = harness
    with local.session_factory() as db:
        env = owner(db)
    config = Settings(litellm_models='model',litellm_url=url,litellm_master_key='synthetic-master',
        workspace_service_ca=certifi.where(),model_tool_failure_guard=True,
        model_call_budgets=settings(env, '.004').model_call_budgets)
    async def identity(*args): return env
    async def space(*args, **kwargs): return 'unused', 'unused'
    monkeypatch.setattr(programming, 'resolve_identity', identity)
    monkeypatch.setattr(programming, 'resolve_space', space)
    app=FastAPI();app.include_router(programming.router)
    def sessions():
        with local.session_factory() as db:
            yield db
    app.dependency_overrides[programming.get_db]=sessions
    app.dependency_overrides[programming.get_settings]=lambda:config
    web=TestClient(app);headers={'Authorization':'Bearer synthetic-only'}
    blocked=web.post('/agent/v1/chat/completions',headers=headers,
        json=body(stream=stream,messages=tools('Execution error: code_execution_quota_exhausted')))
    assert blocked.status_code==200 and 'office_tool_terminal_failure' in blocked.text
    request=body(stream=stream,user=model_user_id(env.issuer,env.subject),metadata={'simulation_case':scenario})
    first=web.post('/agent/v1/chat/completions',headers=headers,json=request)
    assert first.status_code==200
    if scenario=='provider_failure':
        assert 'model_call_not_completed_reservation_retained' in first.text
    with local.session_factory() as db:
        row=db.scalar(select(ModelCallReservation))
        assert row.state==('settled' if scenario=='normal' else 'dispatched')
        attempt=row.id
    if scenario!='normal':
        denied=web.post('/agent/v1/chat/completions',headers=headers,json=request)
        assert denied.status_code==200 and 'model_turn_budget_exhausted' in denied.text
        with local.session_factory() as db:
            row=db.get(ModelCallReservation,attempt)
            row.created_at=now()-timedelta(seconds=20);db.commit()
        # A later request first recovers only its exact native receipt, then reserves.
        if scenario=='no_usage':
            assert web.post('/agent/v1/chat/completions',headers=headers,
                            json=body(stream=True,user=request['user'])).status_code==200
            with local.session_factory() as db:
                assert db.get(ModelCallReservation,attempt).state=='settled'
    with httpx.Client(trust_env=False) as control:
        calls=control.get(url+'/__model_calls').json()['calls']
    assert len(calls)==(2 if scenario=='no_usage' else 1)
    assert all(c['max_tokens']==1024 and c['num_retries']==0 for c in calls)
