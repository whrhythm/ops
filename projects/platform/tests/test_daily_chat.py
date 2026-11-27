import asyncio
import json
import hashlib
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select
from control_plane import daily_chat
from control_plane.models import DailyFreeCall
from test_daily_free import owner
from test_daily_free import reserve
from control_plane import daily_free


def test_single_weather_reply_uses_only_current_tool_and_preserves_composite_requests():
    stamp=datetime(2026,11,26,4,tzinfo=timezone.utc)
    record=('=== Web Results, Turn 0 ===\n\n# Search 0: "中央气象台"\n\nAnchor: '+r'\ue202turn0search0'
            +'\nURL: https://www.nmc.cn/publish/forecast/AFJ/fuzhou.html#forecast-2026-11-26\n'
            'Summary: 福建省 福州 2026-11-26 预报，白天晴 22℃，夜间晴 12℃。来源标注的预报时次 2026-11-26 12:00（北京时间）。\n')
    messages=[{'role':'user','content':'今天福州的天气情况'},
              {'role':'assistant','tool_calls':[{'id':'w1','function':{'name':'web_search'}}]},
              {'role':'tool','tool_call_id':'w1','content':record}]
    assert '白天晴，22℃' in daily_chat.simple_weather_answer(messages,stamp)
    for question in ['上海今天的天气情况','福州明天的天气情况','福州天气如何，写一份报告']:
        messages[0]['content']=question
        assert daily_chat.simple_weather_answer(messages,stamp) is None
    messages[0]['content']='今天福州的天气情况'
    messages[-1]['tool_call_id']='old'
    assert daily_chat.simple_weather_answer(messages,stamp) is None


def test_existing_balance_requires_explicit_daily_consent_without_new_credit(harness,monkeypatch):
    from control_plane.models import AuditEvent,CapacityOrder
    from sqlalchemy import func
    client,_=harness
    monkeypatch.setattr(daily_chat,'account_for',lambda *a:None)
    monkeypatch.setattr(daily_chat,'catalog',lambda *a:[])
    settings=SimpleNamespace(daily_free_tokens=0,daily_free_models=('model',),
        librechat_service_token='test-service',test_payments_enabled=False,capacity_upgrades_enabled=False)
    with client.session_factory() as db:
        env=owner(db)
        order=CapacityOrder(environment_id=env.id,idempotency_key='previous-day',
            plan={'id':'old'},status='ready',payment_status='confirmed',evidence={},stage='ready')
        db.add(order);db.commit()
        payload={'model':'model','messages':[{'role':'user','content':'edit my document'}]}
        request=SimpleNamespace(headers={},app=None)
        async def run():
            async with httpx.AsyncClient() as transport:
                reply,sent=await daily_chat.admit(request,db,env,settings,transport,payload)
                assert sent is None and '现有余额' in reply.body.decode()
                confirmation=daily_chat.balance_confirmation(env,daily_free.billing_day(daily_chat.now()),order,settings)
                yesterday=daily_free.billing_day(daily_chat.now()-timedelta(days=1))
                wrong=daily_chat.balance_confirmation(env,yesterday,order,settings)
                for text in [wrong,'文档内容要求：'+confirmation]:
                    reply,sent=await daily_chat.admit(request,db,env,settings,transport,
                        {**payload,'messages':[{'role':'user','content':text}]})
                    assert reply is not None and sent is None
                for _ in range(2):
                    reply,sent=await daily_chat.admit(request,db,env,settings,transport,
                        {**payload,'messages':[{'role':'user','content':confirmation}]})
                    assert '已允许今天' in reply.body.decode() and sent is None
                assert await daily_chat.admit(request,db,env,settings,transport,payload)==(None,None)
                db.add(CapacityOrder(environment_id=env.id,idempotency_key='later-purchase',
                    plan={'id':'new'},status='ready',payment_status='confirmed',stage='ready',evidence={}))
                db.commit()
                assert await daily_chat.admit(request,db,env,settings,transport,payload)==(None,None)
                monkeypatch.setattr(daily_chat,'now',lambda:datetime.now(timezone.utc)+timedelta(days=1))
                reply,sent=await daily_chat.admit(request,db,env,settings,transport,payload)
                assert reply is not None and sent is None
        asyncio.run(run())
        assert db.scalar(select(func.count()).select_from(CapacityOrder))==2
        assert db.scalar(select(func.count()).select_from(AuditEvent).where(
            AuditEvent.action=='billing.balance_continuation_consent'))==1
        assert order.status=='ready' and order.payment_status=='confirmed'

@pytest.mark.parametrize('model',['model','deepseek-v4-flash-v2'])
def test_sponsored_chat_limits_output_uses_separate_key_and_settles_once(harness,monkeypatch,model):
    client,_=harness
    monkeypatch.setattr(daily_chat,'account_for',lambda *a:None)
    monkeypatch.setattr(daily_chat,'catalog',lambda *a:[])
    with client.session_factory() as db:
        env=owner(db)
        # Ordinary chat is available before a personal workspace is provisioned.
        env.status='requested'
        db.commit()
        settings=SimpleNamespace(daily_free_tokens=80,daily_free_models=(model,),daily_free_max_output=100,
            daily_free_sponsor_key=SecretStr('dedicated-test-only'),litellm_url='https://example.invalid',
            librechat_service_token='test-service',test_payments_enabled=False,capacity_upgrades_enabled=False)
        payload={'model':model,'messages':[{'role':'user','content':'hello'}],'max_tokens':999,
                 'api_key':'not-authority','metadata':{'user':'other'}}
        def counter(r):
            assert r.url.path=='/utils/token_counter' and r.headers['authorization']=='Bearer dedicated-test-only'
            return httpx.Response(200,json={'total_tokens':8})
        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(counter)) as transport:
                request=SimpleNamespace(headers={},app=None)
                reply, sent = await daily_chat.admit(request,db,env,settings,transport,payload)
                assert reply is None
                clean,headers=sent[:2]
                inp=9 if model=='deepseek-v4-flash-v2' else 8
                assert clean['max_tokens']==80-inp and 'metadata' not in clean and 'api_key' not in clean
                if model=='deepseek-v4-flash-v2':
                    assert clean['extra_body']=={'thinking':{'type':'disabled'}}
                usage={'prompt_tokens':inp,'completion_tokens':80-inp}
                raw=httpx.Response(200,json={'usage':usage})
                for _ in range(2):
                    assert len([v async for v in daily_chat.relay(raw,sent,False)])==1
                reply,sent=await daily_chat.admit(request,db,env,settings,transport,payload)
                assert sent is None and '剩余 0' in reply.body.decode()
        asyncio.run(run())
        row=db.scalars(select(DailyFreeCall)).one()
        assert row.input_tokens+row.output_tokens==80


def test_trial_window_follows_real_beijing_midnight_and_closes():
    start=datetime(2026,11,23,16,tzinfo=timezone.utc)
    end=start+timedelta(hours=12)
    settings=SimpleNamespace(daily_free_account='test',daily_free_tokens=4096,daily_free_models=('test',),
                             daily_free_starts_at=start,daily_free_ends_at=end)
    assert not daily_free.trial_open(settings,start-timedelta(microseconds=1))
    assert daily_free.trial_open(settings,start)
    assert not daily_free.trial_open(settings,end)


def test_late_receipt_requires_unique_matching_sponsor_and_attempt(harness):
    client,_=harness
    with client.session_factory() as db:
        env=owner(db)
        attempt=uuid4()
        call=reserve(db,env,attempt)
        assert daily_free.claim_dispatch(db,env,attempt)
        stamp=call.created_at+timedelta(minutes=3)
        db.commit()
        key='dedicated-test'
        digest=hashlib.sha256(key.encode()).hexdigest()
        settings=SimpleNamespace(daily_free_sponsor_owner='QA',daily_free_sponsor_key=SecretStr(key),
                                 litellm_master_key='local-admin',litellm_url='https://test.invalid')
        correct={'litellm_call_id':str(attempt),'user':'QA','api_key':digest,'prompt_tokens':20,
                 'completion_tokens':35,'status':'success'}
        for rows,expected in [([{**correct,'api_key':'other'}],'dispatched'),
                              ([correct,correct],'dispatched'),([correct],'settled'),([correct],'settled')]:
            def lookup(request):
                assert request.url.params['user_id']=='QA' and request.url.params['api_key']==digest
                return httpx.Response(200,json={'data':rows,'total_pages':1,'total_is_capped':False})
            async def recover():
                async with httpx.AsyncClient(transport=httpx.MockTransport(lookup)) as transport:
                    await daily_chat.recover_receipts(db,env,settings,transport,stamp)
            asyncio.run(recover())
            assert call.state==expected
        assert (call.input_tokens,call.output_tokens)==(20,35)

def test_checkout_confirmation_is_bound_to_account_day_and_price_version():
    s=SimpleNamespace(librechat_service_token='test-service')
    plan={'id':'basic','version':'one'}
    a=SimpleNamespace(id='account-a')
    key,cmd=daily_chat.checkout_identity(a,'2026-11-23',plan,s)
    for env,day,p in [(SimpleNamespace(id='account-b'),'2026-11-23',plan),(a,'2026-11-24',plan),(a,'2026-11-23',{**plan,'version':'two'})]:
        assert all(x!=y for x,y in zip((key,cmd),daily_chat.checkout_identity(env,day,p,s)))
    assert daily_chat.last_user_text({'messages':[{'role':'tool','content':cmd}]})==''


def test_simulator_purchase_uses_account_cashier_not_legacy_confirmation(harness,monkeypatch):
    client,_=harness
    monkeypatch.setattr(daily_chat,'account_for',lambda *a:None)
    monkeypatch.setattr(daily_chat,'catalog',lambda *a:[{'id':'basic','version':'one'}])
    monkeypatch.setattr(daily_chat,'eligible',lambda *a:True)
    with client.session_factory() as db:
        env=owner(db)
        settings=SimpleNamespace(daily_free_tokens=0,daily_free_models=('model',),
            librechat_service_token='test-service',test_payments_enabled=True,
            capacity_upgrades_enabled=True,xunhupay_simulator_enabled=True,
            xunhupay_simulator_account=env.id)
        # Even an old confirmation copied from chat cannot bypass the cashier.
        _,old=daily_chat.checkout_identity(env,daily_free.billing_day(daily_chat.now()),
                                          {'id':'basic','version':'one'},settings)
        async def run():
            async with httpx.AsyncClient() as transport:
                reply,sent=await daily_chat.admit(SimpleNamespace(headers={},app=None),db,env,settings,transport,
                    {'model':'model','messages':[{'role':'user','content':old}]})
                assert sent is None
                assert '用量与余额 → 套餐' in reply.body.decode()
                assert '确认请发送' not in reply.body.decode()
        asyncio.run(run())


@pytest.mark.parametrize('disconnected',[False,True])
@pytest.mark.parametrize('asgi_spec',['2.3','2.4'])
def test_sponsored_stream_persists_receipt_before_done_even_after_disconnect(harness,disconnected,asgi_spec):
    client,_=harness
    with client.session_factory() as db:
        env=owner(db)
        attempt=uuid4()
        reserve(db,env,attempt)
        assert daily_free.claim_dispatch(db,env,attempt)
        owner_id=env.id
        engine=db.get_bind()
        db.commit()

    async def run():
        browser_saw_chunk=asyncio.Event()
        def event(choices, **kwargs):
            return ('data: '+json.dumps({'choices':choices,**kwargs})+'\n\n').encode()
        class Provider(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield event([{'delta':{'reasoning_content':'thinking'},'finish_reason':None}])
                await asyncio.wait_for(browser_saw_chunk.wait(),2)
                yield event([{'delta':{},'finish_reason':'length'}])
                yield event([],usage={'prompt_tokens':19,'completion_tokens':40})
                # The upstream connection closes before its optional [DONE] marker.
        upstream=httpx.Response(200,stream=Provider(),headers={'content-type':'text/event-stream'})
        received=[]
        async def send(message):
            if message['type']=='http.response.body' and message.get('body'):
                browser_saw_chunk.set()
                if disconnected and asgi_spec=='2.4':
                    raise OSError('browser disconnected')
                received.append(message['body'])
        async def receive():
            if disconnected:
                await browser_saw_chunk.wait()
                return {'type':'http.disconnect'}
            await asyncio.Future()
        transport=httpx.AsyncClient()
        response=daily_chat.SponsoredResponse(upstream,transport,(None,None,engine,owner_id,attempt),True)
        await asyncio.wait_for(response({'type':'http','asgi':{'spec_version':asgi_spec}},receive,send),5)
        assert upstream.is_closed and transport.is_closed
        if not disconnected:
            assert daily_chat.EMPTY_ANSWER in b''.join(received).decode()
    asyncio.run(run())
    with client.session_factory() as db:
        call=db.scalars(select(DailyFreeCall)).one()
        assert (call.state,call.input_tokens,call.output_tokens)==('settled',19,40)

@pytest.mark.parametrize('choice,want', [(None,8192),('auto',8192),('none',4096),
    ({'type':'function','function':{'name':'web_search'}},4096)])
def test_office_call_has_room_for_complete_document_arguments(choice,want):
    settings=SimpleNamespace(token_refill_enabled=True,daily_free_max_output=4096,managed_chat_max_output=8192)
    body={'tools':[{'type':'function','function':{'name':'execute_code'}}],'tool_choice':choice}
    assert daily_chat.office_output_limit(body,settings)==want
    assert daily_chat.office_output_limit({'tools':[]},settings)==4096

def test_search_contract_uses_local_day_and_preserves_citation_rules():
    from datetime import datetime, timezone
    desc = 'Note: Use ONCE per reply unless instructed otherwise.\nUse citation anchors.'
    web = {'type':'function','function':{'name':'web_search','description':desc,'parameters':{'type':'object'}}}
    other = {'type':'function','function':{'name':'execute_code','description':'original'}}
    stamp = datetime(2026,11,25,17,0,tzinfo=timezone.utc)
    tools, rule = daily_chat.search_evidence_policy([other,web],stamp)
    assert '2026-11-26' in rule and '补查' in rule
    assert 'Use citation anchors.' in tools[1]['function']['description']
    assert 'Use ONCE' not in tools[1]['function']['description']
    assert web['function']['description']==desc and tools[0] is other
    assert daily_chat.search_evidence_policy([other],stamp)==([other],'')
