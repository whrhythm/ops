import asyncio
import base64
import json
import struct
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select,func

from control_plane import daily_chat, daily_free, managed_chat
from control_plane import model_call_guard as guard
from control_plane.models import ModelCallReservation, ChatBalanceConsent, now
from test_daily_free import owner


def settings(**kw):
    return SimpleNamespace(managed_chat_budget_enabled=True,managed_chat_max_output=1024,
        managed_chat_profiles=json.dumps({'model':{'input_ceiling_per_million':'.3',
            'output_ceiling_per_million':'1.2','context_limit':1000000,'framing_tokens':128}}),
        managed_chat_free_daily_usd='.002',daily_free_max_output=1024,managed_chat_max_call_usd='.01',
        litellm_url='http://supplier',litellm_master_key='test',**kw)


def body():return {'model':'model','messages':[{'role':'user','content':'hello'}]}


@pytest.mark.parametrize('structured', [False, True])
def test_long_web_search_fits_remaining_shared_budget_with_citations(structured):
    page = '=== Web Results, Turn 0 ===\n\n' + '\n\n'.join(
        f'# {"Search" if i < 3 else "News"} {i}: 标题\n\nAnchor: turn0search{i}\nURL: https://example.org/{i}\nSummary: 证据{i}' + '中文原文' * 1350
        for i in range(6))
    other = {'role':'tool','tool_call_id':'other','content':'文件结果'}
    content = [{'type':'text','text':page}] if structured else page
    messages = [{'role':'user','content':'请核实并生成报告'},
                {'role':'assistant','tool_calls':[{'id':'s','type':'function','function':{'name':'web_search','arguments':'{}'}}]},
                {'role':'tool','tool_call_id':'s','content':content}, other]
    compact = daily_chat.sponsored_search_messages(messages)
    excerpt = compact[2]['content'][0]['text'] if structured else compact[2]['content']
    assert 0 < len(excerpt.encode()) <= daily_chat.SEARCH_CONTEXT_BYTES
    assert all(f'Anchor: turn0search{i}\nURL: https://example.org/{i}\nSummary: 证据{i}' in excerpt for i in range(6))
    assert messages[2]['content'] == content and compact[0] is messages[0] and compact[3] is other
    policy=managed_chat.free_policy(settings())
    assert guard.quote({**body(),'messages':messages},policy)[1] > 34_830_999
    assert guard.quote({**body(),'messages':compact},policy)[1] < 34_830_999
    assert daily_chat.sponsored_search_messages(compact) is compact


def test_shared_shortfall_explains_personal_allowance_without_dispatch(harness,monkeypatch):
    client,_=harness
    monkeypatch.setattr(daily_chat,'account_for',lambda *a:None)
    monkeypatch.setattr(daily_chat,'catalog',lambda *a:[])
    async def checked(*a):return 'validated'
    monkeypatch.setattr(guard,'verify_price_ceiling',checked)
    s=settings(daily_free_tokens=100000,daily_free_models=('model',),librechat_service_token='test',
        test_payments_enabled=False,capacity_upgrades_enabled=False,daily_free_sponsor_owner='QA',
        daily_free_sponsor_key=SimpleNamespace(get_secret_value=lambda:'sponsor'))
    s.managed_chat_free_daily_usd='.00001'
    with client.session_factory() as db:
        env=owner(db)
        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,json={'total_tokens':12}))) as c:
                req=SimpleNamespace(headers={},app=None)
                reply,sent=await daily_chat.admit(req,db,env,s,c,body())
                assert sent is None and '共享免费预算' in reply.body.decode() and '100000' in reply.body.decode()
                assert db.scalar(select(func.count()).select_from(ModelCallReservation))==0
                assert db.scalar(select(func.count()).select_from(daily_free.DailyFreeCall))==0
                code=daily_chat.balance_confirmation(env,daily_free.billing_day(now()),None,s)
                await daily_chat.admit(req,db,env,s,c,{**body(),'messages':[{'role':'user','content':code}]})
                assert await daily_chat.admit(req,db,env,s,c,body())==(None,None)
        asyncio.run(run())


def test_pool_is_global_and_rollback_does_not_consume_budget(harness):
    client,_=harness
    s=settings();policy=managed_chat.free_policy(s)
    with client.session_factory() as db:
        a=owner(db);b=owner(db)
        payload,attempt=guard.reserve(db,a.id,'sponsor',body(),s,policy_override=policy,commit=False,dispatch=False)
        assert db.get(ModelCallReservation,attempt).state=='reserved'
        db.rollback()
        assert db.get(ModelCallReservation,attempt) is None
        _,attempt=guard.reserve(db,a.id,'sponsor',body(),s,policy_override=policy)
        with pytest.raises(HTTPException,match='budget_exhausted'):
            guard.reserve(db,b.id,'sponsor',body(),s,policy_override=policy)
        db.rollback()
        guard.settle(db,attempt,{'prompt_tokens':10,'completion_tokens':10})
        assert guard.reserve(db,b.id,'sponsor',body(),s,policy_override=policy)[1]


def test_free_tool_followup_caps_output_to_remaining_cash(harness):
    client,_=harness
    s=settings()
    payload={**body(), 'messages':[{'role':'tool','tool_call_id':'s','content':'search evidence'}]}
    policy=managed_chat.free_policy(s)
    _,_,snapshot=guard.quote(payload,policy)
    limit=(snapshot['input_bound']*snapshot['input_rate']+640*snapshot['output_rate'])//1000000
    policy['usd_limit']=str(limit/1e9)
    with client.session_factory() as db:
        env=owner(db)
        with pytest.raises(guard.GuardRejected, match='shared_free_budget_exhausted'):
            guard.reserve(db,env.id,'sponsor',{**payload,'tools':[
                {'type':'function','function':{'name':'execute_code','parameters':{'type':'object'}}}
            ]},s,policy_override=policy)
        db.rollback()
        sent,attempt=guard.reserve(db,env.id,'sponsor',payload,s,policy_override=policy)
        row=db.get(ModelCallReservation,attempt)
        assert row.snapshot['output_bound']==sent['max_tokens']==640
        assert row.reserved_nanos==limit and row.snapshot['funding']=='free'


def test_same_day_free_pool_increase_preserves_old_limits_and_charges(harness):
    client,_=harness
    s=settings()
    original=managed_chat.free_policy(s)
    with client.session_factory() as db:
        env=owner(db)
        _,first=guard.reserve(db,env.id,'sponsor',body(),s,policy_override=original)
        with pytest.raises(guard.GuardRejected,match='shared_free_budget_exhausted'):
            guard.reserve(db,env.id,'sponsor',body(),s,policy_override=original)
        db.rollback()
        larger={**original,'usd_limit':'.020'}
        _,second=guard.reserve(db,env.id,'sponsor',body(),s,policy_override=larger)
        assert db.get(ModelCallReservation,first).limit_nanos==2_000_000
        assert db.get(ModelCallReservation,second).limit_nanos==20_000_000
        with pytest.raises(guard.GuardRejected,match='shared_free_budget_exhausted'):
            guard.reserve(db,env.id,'sponsor',body(),s,policy_override=original)


def test_paid_policy_uses_authoritative_capital_and_fails_closed(harness):
    client,_=harness
    with client.session_factory() as db:
        env=owner(db)
        async def run(status,info):
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(status,json=info))) as c:
                return await managed_chat.paid_policy(db,env,settings(),c)
        result=asyncio.run(run(200,{'user_info':{'max_budget':1,'spend':'.9'}}))
        assert result['capital_nanos']==1000000000 and result['opening_spend']==900000000
        for status,data in [(500,{}),(200,{'user_info':{'max_budget':None,'spend':0}})]:
            with pytest.raises(HTTPException):asyncio.run(run(status,data))


def test_paid_ledger_allows_topup_without_resetting_spent(harness):
    client,_=harness
    with client.session_factory() as db:
        env=owner(db);s=settings()
        policy={**managed_chat.base_policy(s),'id':'managed-paid','funding':'paid','managed_wallet':True,
            'usd_limit':'.002','capital_nanos':1002000000,'opening_spend':1000000000}
        _,attempt=guard.reserve(db,env.id,'paid',body(),s,policy_override=policy)
        with pytest.raises(HTTPException):guard.reserve(db,env.id,'paid',body(),s,policy_override=policy)
        db.rollback()
        policy.update(capital_nanos=1010000000,opening_spend=1001000000,usd_limit='.009')
        assert guard.reserve(db,env.id,'paid',body(),s,policy_override=policy)[1]
        assert db.get(ModelCallReservation,attempt).state=='dispatched'


def test_images_are_bounded_without_network():
    profile={'image_token_bound':32768,'max_image_dimension':2048}
    header=b'\x89PNG\r\n\x1a\n'+b'\0'*8+struct.pack('>II',800,600)
    inline={'type':'image_url','image_url':{'url':'data:image/png;base64,'+base64.b64encode(header).decode()}}
    assert managed_chat.bounded_image_tokens(inline,profile)==32768
    for url in ['http://127.0.0.1/','data:image/png;base64,broken']:
        with pytest.raises(ValueError):managed_chat.bounded_image_tokens({'image_url':{'url':url}},profile)


def test_sparse_late_receipt_preserves_observed_cache_categories(harness):
    client,_=harness
    with client.session_factory() as db:
        env=owner(db);s=settings();policy=managed_chat.free_policy(s)
        _,attempt=guard.reserve(db,env.id,'key',body(),s,policy_override=policy)
        assert guard.settle(db,attempt,{'prompt_tokens':100,'completion_tokens':20,'prompt_tokens_details':{'cached_tokens':60}})
        assert guard.settle(db,attempt,{'prompt_tokens':100,'completion_tokens':20})
        assert db.get(ModelCallReservation,attempt).usage['cache_read_tokens']==60
        assert not guard.settle(db,attempt,{'prompt_tokens':100,'completion_tokens':20,'prompt_tokens_details':{'cached_tokens':61}})


@pytest.mark.parametrize('foreign',[None,'owner','key'])
def test_late_receipt_accepts_only_declared_native_alias_and_matching_identity(harness,foreign):
    client,_=harness
    with client.session_factory() as db:
        env=owner(db);s=settings();policy=managed_chat.free_policy(s)
        profile=policy['models']['model'];profile['native_models']=['model','openai/responses/model']
        s.managed_chat_profiles=json.dumps(policy['models'])
        _,attempt=guard.reserve(db,env.id,'synthetic',body(),s,policy_override=policy)
        row=db.get(ModelCallReservation,attempt);row.created_at=now()-timedelta(seconds=30);db.commit()
        native={'litellm_call_id':attempt,'status':'success','user':row.snapshot['receipt_owner'],
            'api_key':row.key_hash,'model':'openai/responses/model','prompt_tokens':100,
            'completion_tokens':20,'total_tokens':120,'spend':'.00002'}
        if foreign:native['user' if foreign=='owner' else 'api_key']='unrelated'
        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,json={
                'data':[native],'total_pages':1,'total_is_capped':False}))) as c:
                await guard.recover_pending_hash(db,env,row.key_hash,s,c)
        asyncio.run(run());db.refresh(row)
        if foreign:assert row.state=='dispatched' and row.settled_nanos is None
        else:assert row.state=='settled' and row.settled_nanos==20000 and row.snapshot['native_model']=='openai/responses/model'


def test_legacy_wallet_consent_needs_no_order_and_expires_each_day(harness,monkeypatch):
    client,_=harness
    monkeypatch.setattr(daily_chat,'account_for',lambda *a:None)
    monkeypatch.setattr(daily_chat,'catalog',lambda *a:[])
    async def recover(*a):pass
    monkeypatch.setattr(daily_chat,'recover_receipts',recover)
    monkeypatch.setattr(guard,'recover_pending',recover)
    s=settings(daily_free_tokens=0,daily_free_models=('model',),librechat_service_token='test',
        test_payments_enabled=False,capacity_upgrades_enabled=False,
        daily_free_sponsor_key=SimpleNamespace(get_secret_value=lambda:'sponsor'))
    with client.session_factory() as db:
        env=owner(db);request=SimpleNamespace(headers={},app=None)
        async def run():
            async with httpx.AsyncClient() as c:
                reply,_=await daily_chat.admit(request,db,env,s,c,body())
                assert '现有余额' in reply.body.decode()
                code=daily_chat.balance_confirmation(env,daily_free.billing_day(now()),None,s)
                for _ in range(2):
                    reply,_=await daily_chat.admit(request,db,env,s,c,{**body(),'messages':[{'role':'user','content':code}]})
                    assert '已允许今天' in reply.body.decode()
                assert await daily_chat.admit(request,db,env,s,c,body())==(None,None)
        asyncio.run(run())
        assert db.scalar(select(func.count()).select_from(ChatBalanceConsent))==1

def test_sponsored_image_refusal_and_text_followup(harness, monkeypatch):
    client, _ = harness
    monkeypatch.setattr(daily_chat, 'account_for', lambda *a: None)
    monkeypatch.setattr(daily_chat, 'catalog', lambda *a: [])
    async def verified(*args):
        return 'verified'
    monkeypatch.setattr(guard, 'verify_price_ceiling', verified)
    config = settings(daily_free_tokens=100000, daily_free_models=('model',), librechat_service_token='test',
                      test_payments_enabled=False, capacity_upgrades_enabled=False, daily_free_sponsor_owner='QA',
                      daily_free_sponsor_key=SimpleNamespace(get_secret_value=lambda: 'sponsor'))
    config.managed_chat_free_daily_usd = '1'
    image = {'type':'image_url', 'image_url':{'url':'data:image/png;base64,AAAA'}}
    edit = {'role':'user', 'content':[image, {'type':'text','text':'去掉引用来源'}]}
    counted = []
    def counter(request):
        counted.append(json.loads(request.content))
        return httpx.Response(200, json={'total_tokens':20})
    async def exercise(db, env):
        req = SimpleNamespace(headers={}, app=None)
        async with httpx.AsyncClient(transport=httpx.MockTransport(counter)) as c:
            with pytest.raises(guard.GuardRejected, match='model_image_input_unavailable'):
                await daily_chat.admit(req, db, env, config, c, {**body(), 'messages':[edit]})
            assert not counted
            assert db.scalar(select(func.count()).select_from(ModelCallReservation)) == 0
            history = [edit, {'role':'assistant', 'content':'系统提示'}, {'role':'user','content':'你好'}]
            reply, sponsored = await daily_chat.admit(req, db, env, config, c, {**body(), 'messages':history})
            assert reply is None and sponsored
            wire = json.dumps(counted[-1]['messages'], ensure_ascii=False)
            assert '去掉引用来源' in wire and '你好' in wire and '历史图片' in wire and 'image_url' not in wire
            assert history[0]['content'][0] == image
            assert db.scalar(select(func.count()).select_from(ModelCallReservation)) == 1
    with client.session_factory() as db:
        asyncio.run(exercise(db, owner(db)))
    vision = json.loads(config.managed_chat_profiles)
    vision['model']['image_token_bound'] = 32768
    config.managed_chat_profiles = json.dumps(vision)
    assert daily_chat.sponsored_image_messages([edit], 'model', config) == [edit]
