"""Scoped daily trial admission and explicit same-chat test checkout."""
import hashlib
import hmac
import json
import re
from datetime import timedelta, timezone
from uuid import NAMESPACE_URL, uuid4, uuid5

import anyio
import httpx
from fastapi import HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.requests import ClientDisconnect

from . import daily_free
from .capacity import account_for, catalog, eligible
from .models import AuditEvent, CapacityOrder, ChatBalanceConsent, DailyFreeCall, ModelCallReservation, UserEnvironment, now


SEARCH_CONTEXT_BYTES = 12 * 1024
SEARCH_SECTION = re.compile(r'(?m)(?=^# (?:Search|News) \d+: )')


def _search_excerpt(content, allowance):
    if len(content.encode()) <= allowance:
        return content
    heading, *cards = SEARCH_SECTION.split(content)
    marker = '[网页摘录已缩短]'
    if not cards or len(heading.encode()) > 128:
        return content  # Unknown format: keep the normal admission guard in force.
    parts = [heading.rstrip()]
    space = allowance - len(parts[0].encode()) - len(marker.encode()) - 4
    for position, card in enumerate(cards):
        metadata, separator, excerpt = card.partition('\nSummary: ')
        if not separator or not re.search(r'(?m)^Anchor: .+', metadata) or not re.search(r'(?m)^URL: https?://', metadata):
            continue
        prefix = metadata + separator
        prefix_size = len(prefix.encode())
        if prefix_size + 32 > space:
            continue
        share = max(space // (len(cards) - position), prefix_size + 32)
        snippet = excerpt.encode()[:share - prefix_size].decode('utf-8', 'ignore').rstrip()
        part = prefix + snippet
        parts.append(part)
        space -= len(part.encode()) + 2
    return '\n\n'.join([*parts, marker]) if len(parts) > 1 else content


def search_evidence_policy(tools, stamp):
    """Set the managed search contract without mutating the caller's tool definitions."""
    dated = stamp.astimezone(daily_free.BEIJING).date().isoformat()
    rule = (f'北京时间今天是 {dated}。涉及“今天”等相对日期时，先确定目标日期再设计查询。'
            '中国境内城市天气优先调用 web_search 的中央气象台查询：query 严格写成 '
            'weather|forecast|YYYY-MM-DD|省级行政区中文名|城市中文名。直辖市两个地名相同。'
            '询问现在多少度或实况用 current 替换 forecast；一般天气情况用 forecast。'
            '返回的中央气象台摘要为已核对站点和日期的原始字段。用户只问天气或气温时，'
            '最终回答限一两句，只报本轮查到的地点、日期、天气、温度及其引用。'
            '不要附加历史预报、穿衣、防晒、雾、降水推断或追问；实况必须带观测时间。'
            '无对应记录可换公开来源补查。'
            '用户要查指定机构且已知官方公开页面时，优先直接读原文：调用 web_search，query 写成 '
            'read|完整的公开 HTTPS 网页 URL|需要核对的主题。搜索返回 429 时不要重复搜索；若知道相关官方网站的具体页面，可使用此阅读方式。'
            '按问题核对来源正文的地点、对象、有效日期和数值；区分发布日和内容适用日。'
            '若首轮只有导航、其他地点、过期内容或验证页，下一步必须实际再次调用 web_search，'
            '主动换为带日期的当地机构发布稿、公报或报道等其他来源；不要要求用户改问法。'
            '有效日期不等于发表日期；对于提前发布的材料，补查时去掉发布日期过滤。'
            '最多补查两次。没有实时观测但有当天预报时，直接给当天预报，分清预报与实况。'
            '找到足够证据后直接回答并引用支持该事实的原文。正文仅支持部分信息时只答已证实部分；'
            '不使用其他地点、其他日期的数值填空，也不加无关的背景预测。')
    updated = []
    enabled = False
    for tool in tools or []:
        function = tool.get('function') if isinstance(tool, dict) else None
        if isinstance(function, dict) and function.get('name') == 'web_search':
            description = function.get('description', '').replace('Note: Use ONCE per reply unless instructed otherwise.', '')
            tool = {**tool, 'function': {**function, 'description': description.rstrip() + '\n' + rule}}
            enabled = True
        updated.append(tool)
    return updated, rule if enabled else ''


def simple_weather_answer(messages, stamp):
    """For an explicit, single-city daily question return only this turn's verified station record."""
    messages=[m for m in messages if isinstance(m,dict) and m.get('role') not in ('system','developer')]
    if len(messages)<3 or messages[-1].get('role')!='tool':
        return None
    def plain(content):
        if isinstance(content,list) and all(isinstance(part,dict) and part.get('type')=='text' and
                                           isinstance(part.get('text'),str) for part in content):
            return '\n'.join(part['text'] for part in content)
        return content if isinstance(content,str) else None
    last=messages[-1]
    calls=messages[-2].get('tool_calls') or []
    if len(calls)!=1 or calls[0].get('id')!=last.get('tool_call_id') or calls[0].get('function',{}).get('name')!='web_search':
        return None
    body=plain(last.get('content')) or ''
    if body.startswith(('[','"')):
        try:
            body=plain(json.loads(body)) or ''
        except ValueError:
            return None
    if '=== Web Results, Turn ' not in body or len(SEARCH_SECTION.findall(body))!=1:
        return None
    record=re.search(r'^URL: https://www\.nmc\.cn/publish/forecast/[A-Z]+/[a-z0-9_-]+\.html#(current|forecast)-(\d{4}-\d{2}-\d{2})\nSummary: (\S+) (\S+) ([^\n]{1,180})$',body,re.MULTILINE)
    anchor=re.search(r'^Anchor: ((?:\\ue202|\ue202)turn\d+search0)$',body,re.MULTILINE)
    if not record or not anchor:
        return None
    kind,date,province,city,value=record.groups()
    user=next((m for m in reversed(messages[:-2]) if m.get('role')=='user'),{})
    question=re.sub(r'\s+','',plain(user.get('content')) or '').rstrip('？?。')
    if city not in question:
        return None
    remainder=question.replace(province,'',1) if province!=city and province in question else question
    remainder=remainder.replace(city,'',1).removeprefix('市')
    today=stamp.astimezone(daily_free.BEIJING).date()
    offsets={'今天':0,'今日':0,'明天':1,'明日':1,'后天':2}
    offset=next((n for word,n in offsets.items() if word in remainder),0)
    if (today+timedelta(days=offset)).isoformat()!=date:
        return None
    if kind=='current' and re.fullmatch(r'(现在|当前|此刻|实时)(的)?(气温|温度)?(是)?(多少度?|几度)',remainder):
        obs=re.fullmatch(r'实况，观测时间 (\d{4}-\d{2}-\d{2} \d{2}:\d{2})（北京时间），气温 (-?[\d.]+)℃。',value)
        if obs and obs[1].startswith(date):
            return f'{city}最新观测气温为 **{obs[2]}℃**，观测时间为北京时间 {obs[1]}。{anchor[1]}'
    if kind=='forecast' and re.fullmatch(r'(今天|今日|明天|明日|后天)?的?天气(情况|预报|怎么样|如何)?',remainder):
        row=re.fullmatch(r'(\d{4}-\d{2}-\d{2}) 预报，白天([\u3400-\u9fff]+) (-?[\d.]+)℃，夜间([\u3400-\u9fff]+) (-?[\d.]+)℃。来源标注的预报时次 (\d{4}-\d{2}-\d{2} \d{2}:\d{2})（北京时间）。',value)
        if row and row[1]==date:
            return f'{city} {date} 的天气预报：**白天{row[2]}，{row[3]}℃；夜间{row[4]}，{row[5]}℃。** {anchor[1]}'
    return None


def sponsored_search_messages(messages, byte_limit=SEARCH_CONTEXT_BYTES):
    """Shorten only linked web-search excerpts in the outbound sponsored copy."""
    calls = {}
    results = []
    for index, msg in enumerate(messages):
        if not isinstance(msg, dict):
            continue
        if msg.get('role') == 'assistant':
            for call in msg.get('tool_calls') or []:
                if isinstance(call, dict) and isinstance(call.get('function'), dict) and isinstance(call.get('id'), str):
                    calls[call['id']] = call['function'].get('name')
        if msg.get('role') != 'tool' or calls.get(msg.get('tool_call_id')) != 'web_search':
            continue
        content = msg.get('content')
        if isinstance(content, list) and len(content) == 1 and isinstance(content[0], dict) and content[0].get('type') == 'text':
            content = content[0].get('text')
        if isinstance(content, str) and content.startswith('=== Web Results, Turn '):
            results.append((index, content))
    if sum(len(content.encode()) for _, content in results) <= byte_limit:
        return messages
    output = list(messages)
    remaining = byte_limit
    for count, (index, content) in enumerate(results):
        # Reserve room for later searches; short earlier results donate unused space.
        compact = _search_excerpt(content, max(512, remaining // (len(results) - count)))
        remaining = max(0, remaining - len(compact.encode()))
        if compact is not content:
            original = messages[index]['content']
            value = [{**original[0], 'text': compact}] if isinstance(original, list) else compact
            output[index] = {**messages[index], 'content': value}
    return output


def word_followup(payload):
    """Scope a format-only request to its source answer and this turn's tool receipts."""
    tools = payload.get('tools') or []
    names = {t.get('function', {}).get('name') for t in tools if isinstance(t, dict)}
    if 'execute_code' not in names or payload.get('tool_choice') not in (None, 'auto'):
        return None
    messages = payload['messages']
    def plain(msg):
        body = msg.get('content')
        if isinstance(body, list) and len(body) == 1 and body[0].get('type') == 'text':
            body = body[0].get('text')
        return body.strip() if isinstance(body, str) else ''
    turns = [i for i,m in enumerate(messages) if m.get('role') == 'user']
    if len(turns) < 2:
        return None
    question = plain(messages[turns[-1]])
    match = re.fullmatch(
        r'(?:请)?(?:帮我)?(?:把|将)?(.{1,32}?)(?:整理成|导出为?|做成|保存为|转成|转换成|生成)'
        r'(?:一[份个])?(?:可编辑的?)?(?:word|docx?)(?:文档|文件|报告)?[。！!？?]?',
        re.sub(r'\s+', '', question), re.I)
    if not match or re.search(r'查|搜|补充|更新|最新|核实|核对|验证|分析并|重新分析|附件|上传', match[1]):
        return None
    label = re.sub(r'(?:详情|情况|内容|资料|结果)+$', '', match[1])
    # An exact retry may follow an admission refusal. Never cross an unrelated user turn.
    previous = len(turns) - 2
    while previous > 0 and plain(messages[turns[previous]]) == question:
        tail = messages[turns[previous]+1:turns[previous+1]]
        if not any('#starbridge-billing' in plain(m) for m in tail if m.get('role')=='assistant'):
            return None
        previous -= 1
    start, end = turns[previous], turns[previous+1]
    prior = messages[start+1:end]
    answer = next((plain(m) for m in reversed(prior) if m.get('role')=='assistant'
                   and not m.get('tool_calls') and plain(m)), '')
    if not answer or '#starbridge-billing' in answer:
        return None
    reference = re.fullmatch(r'(上面|上述|以上|前面|刚才|这段|这些|上一条|上一个|你的|这份)(的)?(回答|回复|文字|说明|内容|材料|总结|分析)?', match[1])
    if not reference and (not label or label not in (plain(messages[start]) + answer)):
        return None
    # Only carry source URLs whose citation identifiers actually occur in the selected answer.
    urls = []
    for m in prior:
        if m.get('role') != 'tool':
            continue
        for anchor,url in re.findall(r'(?m)^Anchor: (\S+)\s*\nURL: (https?://[^\s]+)', plain(m)):
            if anchor in answer and url not in urls:
                urls.append(url)
    context = [messages[start], {'role':'assistant','content':answer + ('\n\n引用来源\n'+'\n'.join(urls) if urls else '')}]
    instructions = [m for m in messages[:turns[-1]] if m.get('role') in ('system','developer')]
    return {**payload, 'tools':[t for t in tools if t.get('function',{}).get('name') == 'execute_code'],
            'messages':instructions + context + messages[turns[-1]:]}


def message(payload, text):
    identifier = 'chatcmpl-' + uuid4().hex
    base = dict(id=identifier, model=payload['model'], created=int(now().timestamp()))
    if not payload.get('stream'):
        return JSONResponse({**base, 'object':'chat.completion',
            'choices':[{'index':0,'message':{'role':'assistant','content':text},'finish_reason':'stop'}]})
    def chunks():
        for delta,finish in [({'role':'assistant','content':text},None),({},'stop')]:
            data = {**base,'object':'chat.completion.chunk','choices':[{'index':0,'delta':delta,'finish_reason':finish}]}
            yield 'data: '+json.dumps(data,ensure_ascii=False)+'\n\n'
        yield 'data: [DONE]\n\n'
    return StreamingResponse(chunks(),media_type='text/event-stream',headers={'Cache-Control':'no-store'})


def completed_word_export(db, env, payload, stamp):
    """Resolve the last native tool reply to a current, downloadable file owned by this user."""
    from .code_execution import task_view
    from .models import CodeExecution, OfficeTask
    messages = payload.get('messages')
    if not isinstance(messages, list) or len(messages) < 2 or messages[-1].get('role') != 'tool':
        return False
    tool_id = messages[-1].get('tool_call_id')
    if not isinstance(tool_id,str) or not tool_id or len(tool_id)>256:
        return False
    calls = messages[-2].get('tool_calls') or []
    if len(calls)!=1 or calls[0].get('id')!=tool_id or calls[0].get('function',{}).get('name')!='execute_code':
        return False
    if not word_followup(payload):
        return False
    recent = select(OfficeTask.conversation_id, CodeExecution.request_key, OfficeTask.id).join(
        CodeExecution, CodeExecution.id==OfficeTask.id).where(
        OfficeTask.environment_id==env.id, CodeExecution.environment_id==env.id,
        OfficeTask.state=='complete',OfficeTask.updated_at>=stamp-timedelta(minutes=15)
    ).order_by(OfficeTask.updated_at.desc()).limit(64)
    for conversation, key, task_id in db.execute(recent):
        data=json.dumps([conversation,tool_id],separators=(',',':'),ensure_ascii=False).encode()
        if hashlib.sha256(data).hexdigest()!=key:
            continue
        task=db.get(OfficeTask,task_id)
        receipt=task_view(task)
        result=receipt['result'] or {}
        if receipt['outputs_expired'] or result.get('stderr'):
            return False
        return any(f.get('name','').lower().endswith('.docx') and
                   re.fullmatch(r'[\w-]+',f.get('id','')) and
                   re.fullmatch(r'[\w-]+',f.get('storage_session_id') or f.get('session_id') or result.get('session_id') or '')
                   for f in result.get('files',[]))
    return False


def office_output_limit(payload, settings):
    limit = settings.daily_free_max_output
    if not getattr(settings, 'token_refill_enabled', False):
        return limit
    choice = payload.get('tool_choice')
    if choice == 'none' or (isinstance(choice, dict) and choice.get('function', {}).get('name') != 'execute_code'):
        return limit
    if any(t.get('function', {}).get('name') == 'execute_code' for t in payload.get('tools') or [] if isinstance(t, dict)):
        return max(limit, settings.managed_chat_max_output)
    return limit


def last_user_text(body):
    messages=body.get('messages') or []
    if not messages or messages[-1].get('role')!='user':
        return ''
    content=messages[-1].get('content')
    if isinstance(content,list) and len(content)==1 and content[0].get('type')=='text':
        content=content[0].get('text')
    return content.strip() if isinstance(content,str) else ''


def checkout_identity(env, day, plan, settings):
    key=f"daily-plan-{day}-{plan['id']}-{plan['version']}"
    code=hmac.new(settings.librechat_service_token.encode(),f'{env.id}:{key}'.encode(),hashlib.sha256).hexdigest()[:12]
    return key+'-'+code, '确认办理测试套餐并使用套餐额度 '+code


def balance_confirmation(env, day, order, settings):
    value=f'balance:{env.id}:{day}:{order.id if order else "existing-wallet"}'
    code=hmac.new(settings.librechat_service_token.encode(),value.encode(),hashlib.sha256).hexdigest()[:12]
    return '确认今天使用现有余额 '+code


async def checkout(app,env,settings,plan,key):
    headers={'X-CodeChat-Service-Token':settings.librechat_service_token,'X-CodeChat-Subject':env.subject,
             'Idempotency-Key':key}
    # Reuse the same authenticated capacity API and controller used by the account page.
    prefix='/internal/v1/workspace/capacity/orders'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://internal',headers=headers) as client:
        r=await client.post(prefix,json={'plan_id':plan['id'],'plan_version':plan['version']})
        if r.status_code not in (200,201):
            raise HTTPException(409,'测试套餐订单未创建，请检查当前套餐状态')
        order=r.json()
        r=await client.post(f"{prefix}/{order['id']}/confirm-test")
        if r.status_code!=200:
            raise HTTPException(409,'测试套餐尚未确认，可重试同一笔订单')
        return r.json()


async def recover_receipts(db, env, settings, client, stamp):
    """Recover missing stream receipts from the configured sponsor's native spend records."""
    pending=db.execute(select(DailyFreeCall.attempt_id,DailyFreeCall.created_at).where(
        DailyFreeCall.environment_id==env.id, DailyFreeCall.state=='dispatched',
        DailyFreeCall.created_at < stamp-timedelta(seconds=15)).order_by(DailyFreeCall.created_at.desc()).limit(20)).all()
    if not pending or not settings.daily_free_sponsor_owner:
        return
    ids={row.attempt_id for row in pending}
    digest=hashlib.sha256(settings.daily_free_sponsor_key.get_secret_value().encode()).hexdigest()
    def utc_string(value):
        if value.tzinfo is not None:
            value=value.astimezone(timezone.utc)
        return value.strftime('%Y-%m-%d %H:%M:%S')
    params={'api_key':digest,'user_id':settings.daily_free_sponsor_owner,'page_size':100,
            'start_date':utc_string(pending[-1].created_at-timedelta(seconds=2)),
            'end_date':utc_string(stamp+timedelta(seconds=2))}
    db.commit()
    try:
        response=await client.get(settings.litellm_url.rstrip('/')+'/spend/logs/v2',params=params,
            headers={'Authorization':'Bearer '+settings.litellm_master_key},timeout=5)
        if not response.is_success:
            return
        page=response.json()
    except (httpx.HTTPError,ValueError):
        return
    # An incomplete result set cannot establish that the receipt is unique.
    if not isinstance(page,dict) or page.get('total_pages')!=1 or page.get('total_is_capped'):
        return
    matches={}
    for row in page.get('data',[]):
        attempt=row.get('litellm_call_id')
        if attempt in ids and row.get('api_key')==digest and row.get('user')==settings.daily_free_sponsor_owner:
            matches.setdefault(attempt,[]).append(row)
    for attempt, rows in matches.items():
        if len(rows)==1 and rows[0].get('status')=='success':
            daily_free.settle(db,env,attempt,rows[0],'litellm')
    db.commit()


def consent_recorded(db, env, day):
    if db.get(ChatBalanceConsent, (env.id, day)) is not None:
        return True
    return db.scalar(select(CapacityOrder.id).where(
        CapacityOrder.environment_id==env.id, CapacityOrder.status=='ready',
        CapacityOrder.payment_status=='confirmed',
        CapacityOrder.evidence['balance_consent_day'].as_string()==day).limit(1)) is not None


def record_balance_consent(db, env, day):
    db.refresh(env, with_for_update=True)
    if day != daily_free.billing_day(now()):
        raise HTTPException(409, 'billing_day_changed')
    if db.get(ChatBalanceConsent, (env.id, day)) is None:
        db.add(ChatBalanceConsent(environment_id=env.id, day=day))
        db.add(AuditEvent(tenant_id=env.first_tenant_id,issuer=env.issuer,subject=env.subject,
            action='billing.balance_continuation_consent',target_id=env.id))
    db.commit()


async def admit(request,db,env,settings,client,payload):
    """Return local reply, sponsored request, or an explicitly authorized paid continuation."""
    stamp=now()
    await recover_receipts(db,env,settings,client,stamp)
    if getattr(settings,'managed_chat_budget_enabled',False):
        from .model_call_guard import recover_pending
        await recover_pending(db,env,settings.daily_free_sponsor_key.get_secret_value(),settings,client)
    day=daily_free.billing_day(stamp)
    token_mode = getattr(settings, 'token_refill_enabled', False)
    if token_mode:
        from .token_refill import view as refill_view
        managed, balance_allowed = True, False
        purchased = refill_view(db, env, day, settings)['remaining']
    else:
        purchased = 0
        account=account_for(db,env,settings)
        plans=sorted((p for p in catalog(settings,db) if p.get('model_credit_usd_micros',0)>0 and eligible(p,account,env)),
                     key=lambda p:(p['amount_minor'],p['id']))
        order=db.scalar(select(CapacityOrder).where(CapacityOrder.environment_id==env.id,
            CapacityOrder.idempotency_key.startswith('daily-plan-'+day+'-')).order_by(CapacityOrder.created_at.desc()).limit(1))
        if order and not hmac.compare_digest(order.idempotency_key, checkout_identity(env,day,order.plan,settings)[0]):
            order=None
        user_text=last_user_text(payload)
        previous=db.scalar(select(CapacityOrder).where(
            CapacityOrder.environment_id==env.id,CapacityOrder.status=='ready',
            CapacityOrder.payment_status=='confirmed').order_by(CapacityOrder.created_at.desc()).limit(1))
        managed=getattr(settings,'managed_chat_budget_enabled',False)
        balance_choice=balance_confirmation(env,day,previous,settings) if previous or managed else ''
        if balance_choice and user_text==balance_choice:
            db.refresh(env,with_for_update=True)
            if managed:
                record_balance_consent(db,env,day)
            elif previous.evidence.get('balance_consent_day')!=day:
                previous.evidence={**previous.evidence,'balance_consent_day':day}
                db.add(AuditEvent(tenant_id=env.first_tenant_id,issuer=env.issuer,subject=env.subject,
                    action='billing.balance_continuation_consent',target_id=previous.id))
                db.commit()
            return message(payload,'已允许今天免费额度不足时使用现有余额，按实际模型调用扣费。请继续刚才的任务。'),None
        balance_allowed=consent_recorded(db,env,day) if managed or previous else False
        if order and order.status=='ready' and order.payment_status=='confirmed':
            if user_text.startswith('确认办理测试套餐并使用套餐额度 '):
                return message(payload,'这笔测试套餐已生效，权益已发放一次。可直接在此对话继续。'),None
            return None,None
        simulator_checkout = bool(getattr(settings, 'xunhupay_simulator_enabled', False)
            and getattr(settings, 'xunhupay_simulator_account', '') in ('',env.id))
        options=bool(plans and settings.test_payments_enabled and settings.capacity_upgrades_enabled
                     and not simulator_checkout)
        key,confirmation=checkout_identity(env,day,plans[0],settings) if options else ('','')
        if options and user_text==confirmation:
            # No database transaction may retain account locks across the checkout handler.
            db.commit()
            receipt=await checkout(request.app,env,settings,plans[0],key)
            return message(payload,'已确认测试订单 '+receipt['id']+'。权益生效后，请在此对话继续。重复确认不会再次发放。'),None
        if order and order.status in ('pending_payment','applying','failed'):
            return message(payload,'测试套餐正在开通或待核验，请稍后在此对话继续。'),None
    budget=daily_free.day_view(db,env,day,settings.daily_free_tokens)
    if (token_mode and payload.get('model') in settings.daily_free_models and payload.get('n',1)==1
            and payload.get('response_format') in (None,{'type':'text'})
            and completed_word_export(db,env,payload,stamp)):
        return message(payload,'Word 文档已生成，可在本条消息的文件卡片中预览或下载。'),None
    def prompt(shared=False, input_bound=None):
        if token_mode:
            if shared:
                return message(payload,'平台暂时无法处理这次请求，请稍后再试。充值不会解除本次限制。')
            if payload['model'] not in settings.daily_free_models:
                return message(payload,'当前模型不支持本账号的 tokens，请切换到 '+', '.join(settings.daily_free_models)+' 后再试。')
            available = budget['remaining'] + purchased
            detail = f'本次预计输入约 {input_bound:,} tokens，还需为回答预留用量。' if input_bound is not None else ''
            return message(payload,f'当前可用 {available:,} tokens。{detail}剩余用量不足，本次模型调用未发出。'
                           '[查看用量或充值 tokens](#starbridge-billing)')

        text=f"今日免费额度剩余 {budget['remaining']} tokens，无法完成本次请求。北京时间零点重置，本次未扣余额。"
        if budget['reserved']:
            text=f"当前有 {budget['reserved']} tokens 免费额度正在被其他请求占用，本次暂未派发，也未扣余额。请稍后重试，或查看套餐与余额。"
        if shared:
            text=(f"平台今天的共享免费预算暂时不足以继续这次请求。你的个人免费额度还剩 {budget['remaining']} tokens。"
                  "本次未扣个人余额。可以稍后重试，也可以查看现有余额。")
        if managed:
            reason='shared' if shared else ('model' if payload['model'] not in settings.daily_free_models else 'personal')
            if reason=='model':
                text='当前模型不在每日免费范围内。你可以切换免费模型，或查看现有余额。'
            text+=f"\n\n[查看现有余额与继续方式](#starbridge-billing-{reason})"
        elif previous:
            text+=f"\n\n确认今天使用现有余额继续，请发送：{balance_choice}"
        if simulator_checkout and not managed:
            text+='\n\n[查看套餐](#starbridge-billing) 或打开账号菜单中的“用量与余额 → 套餐”。当前为模拟付款，不收取现金。'
        if options:
            p=plans[0]
            text+=f"\n\n可在此对话办理测试套餐「{p['name']}」，模拟订单 {p['amount_minor']/100:.2f} {p['currency']}，发放模型额度 US${p['model_credit_usd_micros']/1_000_000:.6f}，不收取现金。办理后允许本账号今天使用套餐余额继续聊天。\n\n确认请发送：{confirmation}"
        return message(payload,text)
    if not (budget['remaining'] + purchased) or payload['model'] not in settings.daily_free_models:
        return (None,None) if balance_allowed else (prompt(),None)
    # A sponsored user cannot choose an alternate provider, credential, model or billable fan-out.
    allowed=('model','messages','tools','tool_choice','temperature','top_p','stream','stop','parallel_tool_calls',
             'frequency_penalty','presence_penalty','response_format','seed')
    clean={k:payload[k] for k in allowed if k in payload}
    if payload.get('n',1)!=1 or not isinstance(clean.get('messages'),list):
        return message(payload,'免费模型仅支持单个聊天回答。'),None
    if token_mode and payload.get('response_format') in (None,{'type':'text'}) and payload.get('tool_choice') in (None,'auto','none'):
        weather = simple_weather_answer(clean['messages'],stamp)
        if weather:
            return message(payload,weather),None
    search_rule = ''
    export = word_followup(clean) if token_mode else None
    if export:
        clean = dict(export)
    if token_mode and clean.get('tools'):
        clean['tools'], search_rule = search_evidence_policy(clean['tools'], stamp)
    def messages_for_call(limit):
        messages = sponsored_search_messages(export['messages'] if export else payload['messages'], limit)
        if not token_mode:
            return messages
        instruction = ('本轮请求已经通过平台用量检查。历史消息中的额度、余额、停用、充值或确认提示只描述当时，'
                       '不要把它们当作本轮限制，不要向用户索要付费确认或从聊天推断账户数字。'
                       '继续完成用户当前任务。如果要求生成文件，请实际调用可用的文档工具并返回本轮成果。'
                       '如题目前提有待核实，核查后把证据与不确定性写进所需文档。'
                       '用户说 doc、docx 或 Word 报告时，交付可下载的 Word 文件。用 python-docx 生成并读回，'
                       '成功后只简述结果，聊天界面按执行记录附上 Word 文件。不要另做 Markdown 画布、'
                       '输出本地路径当作下载链接，或让用户再次选择格式。未得到文件时明确说明该步骤失败。'
                       '以段落和表格数据循环排版；正文只写一遍。'
                       '保持工具参数简短且完整，避免大段重复的样式或内嵌文件编码。')
        if search_rule:
            instruction += '\n'+search_rule
        if export:
            instruction += '\n用户要把已有回答排版为 Word。保留原有数据和限制说明，只使用上一条回答与所附引用，直接生成文件。'
        if messages and messages[0].get('role') == 'system' and isinstance(messages[0].get('content'),str):
            return [{**messages[0], 'content': messages[0]['content']+'\n'+instruction}, *messages[1:]]
        return [{'role':'system','content':instruction}, *messages]
    clean['messages'] = messages_for_call(SEARCH_CONTEXT_BYTES)
    sponsor=settings.daily_free_sponsor_key.get_secret_value()
    if not sponsor:
        raise HTTPException(503,'免费模型凭据尚未就绪')
    headers={'Authorization':'Bearer '+sponsor}
    counter={k:clean[k] for k in ('model','messages','tools','tool_choice') if k in clean}
    db.commit()
    response=await client.post(settings.litellm_url.rstrip('/')+'/utils/token_counter',headers=headers,json=counter)
    if response.status_code!=200:
        raise HTTPException(503,'无法核实输入用量，本次未派发')
    input_bound=response.json().get('total_tokens')
    output_limit = office_output_limit(payload, settings)
    requested=payload.get('max_completion_tokens',payload.get('max_tokens',output_limit))
    if type(input_bound) is not int or input_bound<0 or type(requested) is not int or requested<=0:
        raise HTTPException(422,'无法确定本轮所需额度')
    if clean['model'] == 'deepseek-v4-flash-v2':
        # The compatible gateway estimates prompt tokens; the provider settles actual usage.
        input_bound += max(1, (input_bound + 99) // 100)
        clean['extra_body'] = {'thinking': {'type': 'disabled'}}
    budget=daily_free.day_view(db,env,day,settings.daily_free_tokens)
    allowed_output=min(requested,output_limit,budget['remaining']+purchased-input_bound)
    if allowed_output<=0 or (clean['model']=='deepseek-v4-flash-v2' and allowed_output<min(64,requested)):
        return (None,None) if balance_allowed else (prompt(input_bound=input_bound),None)
    clean['max_tokens']=allowed_output
    if clean.get('stream'):
        clean['stream_options']={'include_usage':True}
    retry_key=request.headers.get('idempotency-key')
    attempt=uuid5(NAMESPACE_URL,env.id+':'+retry_key+':'+hashlib.sha256(json.dumps(clean,sort_keys=True).encode()).hexdigest()) if retry_key else uuid4()
    if managed:
        from .managed_chat import free_policy
        from .model_call_guard import GuardRejected, reserve as reserve_budget, verify_price_ceiling
        policy=free_policy(settings,stamp)
        policy['max_output_tokens'] = min(output_limit, settings.managed_chat_max_output)
        fingerprint=await verify_price_ceiling(clean,policy,settings,client)
        db.rollback()
        for shorter in (False, True):
            if shorter:
                excerpts = messages_for_call(3072)
                if excerpts == clean['messages']:
                    return (None,None) if balance_allowed else (prompt(shared=True),None)
                clean = {**clean, 'messages': excerpts}
                response=await client.post(settings.litellm_url.rstrip('/')+'/utils/token_counter',headers=headers,
                    json={**counter, 'messages': excerpts})
                tokens=response.json().get('total_tokens') if response.status_code==200 else None
                if type(tokens) is not int or tokens<0:
                    raise HTTPException(503,'无法核实输入用量，本次未派发')
                input_bound=tokens+(max(1,(tokens+99)//100) if clean['model']=='deepseek-v4-flash-v2' else 0)
            try:
                clean,_=reserve_budget(db,env.id,sponsor,clean,settings,receipt_owner=settings.daily_free_sponsor_owner,
                    price_fingerprint=fingerprint,policy_override=policy,attempt_id=attempt,commit=False,dispatch=False)
                allowed_output = clean['max_tokens']
                break
            except GuardRejected as exc:
                if exc.detail != 'shared_free_budget_exhausted':
                    raise
                db.rollback()
        else:
            return (None,None) if balance_allowed else (prompt(shared=True),None)
    try:
        daily_free.reserve(db,env,attempt_id=attempt,model=clean['model'],input_bound=input_bound,
                           output_bound=allowed_output,settings=settings)
    except HTTPException as e:
        if e.detail!='free_allowance_insufficient':
            raise
        db.rollback()
        budget=daily_free.day_view(db,env,day,settings.daily_free_tokens)
        if token_mode:
            purchased=refill_view(db,env,day,settings)['remaining']
        return (None,None) if balance_allowed else (prompt(input_bound=input_bound),None)
    if not daily_free.claim_dispatch(db,env,attempt):
        raise HTTPException(409,'此免费请求已经派发')
    if managed:
        from .model_call_guard import claim_reserved
        claim_reserved(db,attempt)
    owner_id=env.id
    engine=db.get_bind()
    db.commit()
    headers['x-litellm-call-id']=str(attempt)
    return None,(clean,headers,engine,owner_id,attempt)


EMPTY_ANSWER = '这次模型在写出正文前就用完了本轮输出额度。请把任务缩小到一个段落或一个问题；每日免费额度在北京时间零点重置。本轮用量以用量记录为准。'
NO_TEXT = '模型结束了本次回答，但没有返回正文。可以把任务缩小到一个段落或一个问题再试。本轮用量以用量记录为准。'


async def relay(response,sponsored,is_stream):
    _,_,engine,owner,attempt=sponsored
    def record(usage):
        if isinstance(usage,dict):
            with Session(engine) as db:
                env=db.get(UserEnvironment,owner)
                daily_free.settle(db,env,attempt,usage,'litellm')
                if db.get(ModelCallReservation,str(attempt)):
                    from .model_call_guard import settle as settle_budget
                    settle_budget(db,str(attempt),usage)
                db.commit()
    if not is_stream:
        raw=await response.aread()
        packet=json.loads(raw)
        record(packet.get('usage'))
        for choice in packet.get('choices', []):
            answer=choice.get('message') or {}
            if (choice.get('finish_reason') in ('stop','length') and not (answer.get('content') or '').strip()
                    and not answer.get('tool_calls') and not answer.get('function_call') and not answer.get('refusal')):
                answer['content']=EMPTY_ANSWER if choice['finish_reason']=='length' else NO_TEXT
                choice['message']=answer
                raw=json.dumps(packet,ensure_ascii=False).encode()
        yield raw
        return
    visible=False
    pending_usage=None
    async for line in response.aiter_lines():
        if line.startswith('data:') and line[5:].strip() not in ('','[DONE]'):
            packet=json.loads(line[5:])
            if packet.get('usage'):
                pending_usage=packet['usage']
                if not packet.get('choices') or any(c.get('finish_reason') for c in packet['choices']):
                    record(pending_usage)
                    pending_usage=None
            for choice in packet.get('choices', []):
                delta=choice.get('delta') or {}
                visible |= bool((delta.get('content') or '').strip() or delta.get('tool_calls')
                                or delta.get('function_call') or delta.get('refusal'))
                if not visible and choice.get('finish_reason') in ('length','stop'):
                    notice=EMPTY_ANSWER if choice['finish_reason']=='length' else NO_TEXT
                    line='data: '+json.dumps({**packet,'choices':[{**choice,'delta':{**delta,'content':notice}}]},ensure_ascii=False)
                    visible=True
        elif line.startswith('data:') and line[5:].strip()=='[DONE]' and pending_usage:
            record(pending_usage)
        yield line+'\n'


class SponsoredResponse(StreamingResponse):
    """Read the already-dispatched, output-capped call through its receipt after a browser disconnect."""
    def __init__(self, upstream, client, sponsored, is_stream):
        self.upstream=upstream
        self.client=client
        self.sponsored=sponsored
        self.is_stream=is_stream
        self.sender,self.receiver=anyio.create_memory_object_stream(8)
        super().__init__(self.receiver, media_type=upstream.headers.get('content-type'),
                         headers={'Cache-Control':'no-store','X-Accel-Buffering':'no'})

    async def pump(self):
        async with self.sender:
            connected=True
            async for chunk in relay(self.upstream,self.sponsored,self.is_stream):
                if connected:
                    try:
                        await self.sender.send(chunk)
                    except anyio.BrokenResourceError:
                        connected=False

    async def __call__(self, scope, receive, send):
        try:
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(self.pump)
                async with self.receiver:
                    try:
                        await super().__call__(scope,receive,send)
                    except ClientDisconnect:
                        pass
        finally:
            with anyio.CancelScope(shield=True):
                await self.upstream.aclose()
                await self.client.aclose()
