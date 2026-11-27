"""Admission only: never debit wallets, mutate files, or replay an execution.

Budget values are USD nanodollars. Profiles are operator-verified *sales price
ceilings*, not procurement estimates. Unknown receipts keep their reservation.
"""
import hashlib
import json
import re
import httpx
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_CEILING
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select, text

from .models import ModelCallReservation, UserEnvironment, now
from .model_usage import normalize_usage

SCALE = Decimal(10**9)
CODE_TOOLS = {'execute_code', 'code_execution', 'python'}
FILE_TOOLS = {'read_file', 'read_files'}
TERMINAL = re.compile(
    r'code_execution_(?:quota_exhausted|upload_quota_exhausted|account_disabled|identity_required)'
    r'|office_task_(?:already_submitted|cancelled|receipt_expired)'
    r'|temporarily rate-limited|rate limit|quota.{0,24}(?:exhaust|exceed)'
    r'|authorization|not authorized|permission denied|result.{0,16}unknown'
    r'|execution.{0,16}(?:unavailable|timed out)|service.{0,16}unavailable', re.I)


class GuardRejected(HTTPException):
    pass


def stop(code):
    # SDKs retry 409/429/5xx. A terminal admission decision is a non-retryable 400.
    raise GuardRejected(400, code)


def rejection_response(payload, code):
    """Finish the agent turn locally, with an explanation rather than a 400 loop."""
    from fastapi.responses import JSONResponse, StreamingResponse
    notices = {
        'model_image_input_unavailable': '当前模型无法读取本轮图片。请先用「扫描/OCR」提取文字，或粘贴需要修改的段落，再告诉我怎样修改。',
        'office_tool_terminal_failure': '工具额度、权限或执行状态不允许继续，本轮已停止。已有成果保留，请在文件任务中查看；不会自动重新执行。',
        'office_tool_result_requires_review': '工具执行没有得到可安全重试的结果，本轮已停止。已有成果保留，请先核对原任务。',
        'office_code_correction_limit': '代码纠正后仍未通过，本轮已停止，避免重复执行。已生成的文件和原件都保留。',
        'model_turn_budget_exhausted': '本次模型请求超出可用范围，已停止。[查看 token 用量](#starbridge-billing)',
        'model_call_not_completed_reservation_retained': '模型请求未完成，用量仍待核验，已暂停自动重试。已有成果和会话保留。',
        'model_budget_window_closed': '本批模型调用的授权时间已结束，未发起新的付费调用。已有成果和会话保留。',
    }
    notice = '系统提示：' + notices.get(code, '当前请求无法在已授权的预算范围内安全执行，未继续调用模型。已有成果和会话保留。')
    base = {'id': 'chatcmpl-local-guard-' + uuid4().hex, 'model': payload.get('model', ''),
            'created': int(now().timestamp()), 'platform_guard': code}
    usage = {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0}
    if not payload.get('stream'):
        return JSONResponse({**base, 'object':'chat.completion','usage':usage,
            'choices':[{'index':0,'message':{'role':'assistant','content':notice},'finish_reason':'stop'}]})
    packet = {**base, 'object':'chat.completion.chunk','usage':usage,
        'choices':[{'index':0,'delta':{'role':'assistant','content':notice},'finish_reason':'stop'}]}
    return StreamingResponse(iter(['data: '+json.dumps(packet,ensure_ascii=False)+'\n\n','data: [DONE]\n\n']),
        media_type='text/event-stream',headers={'Cache-Control':'no-store','X-Accel-Buffering':'no'})


def check_tool_failures(messages):
    """Inspect tool outputs since the latest user turn; never search source prose."""
    if not isinstance(messages, list):
        return
    tail = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        if message.get('role') == 'user':
            tail = []
        tail.append(message)
    names, failures = {}, 0
    for message in tail:
        for call in message.get('tool_calls') or []:
            if isinstance(call, dict):
                names[call.get('id')] = (call.get('function') or {}).get('name')
        if message.get('role') not in ('tool', 'function'):
            continue
        name = names.get(message.get('tool_call_id'), message.get('name'))
        if name not in CODE_TOOLS | FILE_TOOLS:
            continue
        content = message.get('content')
        if not isinstance(content, str):
            continue
        # A quoted document mentioning a quota is not a failed tool call.
        failed = bool(re.match(r'\s*(?:Execution error:|Error(?: executing| reading)?[:\s]|Tool error:)', content, re.I))
        code_error = name in CODE_TOOLS and 'Traceback (most recent call last):' in content
        if failed and TERMINAL.search(content):
            stop('office_tool_terminal_failure')
        if failed and not code_error:
            # Unknown transport/validation failures are not safe execution retries.
            stop('office_tool_result_requires_review')
        if code_error:
            failures += 1
    if failures >= 2:
        stop('office_code_correction_limit')


def nanos(value):
    result = Decimal(str(value))
    if not result.is_finite() or result < 0:
        stop('model_budget_invalid_policy')
    return int((result * SCALE).to_integral_value(rounding=ROUND_CEILING))


def lock_account(db, env_id):
    if db.bind.dialect.name == 'sqlite':
        connection = db.connection()
        if not connection.connection.driver_connection.in_transaction:
            connection.execute(text('BEGIN IMMEDIATE'))
    env = db.scalar(select(UserEnvironment).where(UserEnvironment.id == env_id).with_for_update())
    if env is None:
        stop('model_budget_identity_required')


def policy_for(settings, env_id):
    try:
        policies = json.loads(settings.model_call_budgets)
        policy = policies.get(env_id)
        if policy is not None and not isinstance(policy, dict):
            raise ValueError()
        return policy
    except (ValueError, TypeError, AttributeError):
        stop('model_budget_invalid_policy')


def quote(payload, policy):
    """Byte bound for supported text/function calls, plus conservative framing.

    This pilot excludes image/audio/native paid tools. It does not claim an
    arbitrary provider's multimodal billing can be bounded by a text tokenizer.
    """
    try:
        # Do not permit a provider override or extra_body to bypass these bounds.
        allowed = {'model','messages','tools','functions','function_call','tool_choice','parallel_tool_calls',
                   'max_tokens','max_completion_tokens','temperature','top_p','stop','seed','stream',
                   'stream_options','response_format','frequency_penalty','presence_penalty','logit_bias',
                   'logprobs','top_logprobs','reasoning_effort','verbosity','user','metadata','n','store',
                   'modalities'}
        if payload.get('extra_body')=={'thinking':{'type':'disabled'}}:
            allowed.add('extra_body')
        if set(payload) - allowed:
            raise ValueError()
        model = payload['model']
        profile = policy['models'][model]
        input_rate = nanos(profile['input_ceiling_per_million'])
        output_rate = nanos(profile['output_ceiling_per_million'])
        output = policy['max_output_tokens']
        if type(output) is not int or not 1 <= output <= 16384 or not input_rate or not output_rate:
            raise ValueError()
        for field in ('max_tokens', 'max_completion_tokens'):
            if field in payload:
                supplied = payload[field]
                if type(supplied) is not int or supplied <= 0:
                    raise ValueError()
                output = min(output, supplied)
        if payload.get('n', 1) != 1 or payload.get('modalities', ['text']) != ['text']:
            raise ValueError()
        if any(tool.get('type') != 'function' for tool in payload.get('tools', [])):
            raise ValueError()
        image_tokens=0
        counter_messages=[]
        for message in payload['messages']:
            content = message.get('content')
            clean=message
            if isinstance(content,list):
                parts=[]
                for part in content:
                    if part.get('type')=='text':parts.append(part)
                    elif part.get('type')=='image_url' and profile.get('image_token_bound'):
                        from .managed_chat import bounded_image_tokens
                        image_tokens+=bounded_image_tokens(part,profile)
                        parts.append({'type':'text','text':'[bounded image]'})
                    else:raise ValueError()
                clean={**message,'content':parts}
            counter_messages.append(clean)
            if message.get('audio') or message.get('images'):
                raise ValueError()
        if any(payload.get(k) for k in ('audio', 'prediction', 'web_search_options')):
            raise ValueError()
        # Worst-case byte tokens plus serialization/framing margin. No cache discount.
        wire = json.dumps({k: (counter_messages if k=='messages' else payload[k]) for k in ('messages', 'tools', 'functions', 'response_format') if k in payload},
                          ensure_ascii=False, separators=(',', ':')).encode()
        input_bound = len(wire) * 2 + int(profile.get('framing_tokens',8192)) + image_tokens
        if input_bound + output > int(profile['context_limit']):
            stop('model_budget_context_bound_exceeded')
        price = (input_rate * input_bound + output_rate * output + 999999) // 1000000
        snapshot = {'input_rate': input_rate, 'output_rate': output_rate,
                    'input_bound': input_bound, 'output_bound': output,
                    'native_models':profile.get('native_models',[model])}
        forwarded = {**payload, 'num_retries': 0, 'disable_fallbacks': True, 'fallbacks': [], 'context_window_fallbacks': [],
                     'content_policy_fallbacks': [], 'n': 1}
        forwarded.pop('max_tokens', None)
        forwarded.pop('max_completion_tokens', None)
        field = 'max_completion_tokens' if 'max_completion_tokens' in payload else 'max_tokens'
        forwarded[field] = output
        if forwarded.get('stream'):
            forwarded['stream_options'] = {**forwarded.get('stream_options', {}), 'include_usage': True}
        return forwarded, price, snapshot
    except (KeyError, TypeError, ValueError, AttributeError, ArithmeticError):
        stop('model_budget_unsupported_request_or_price')


async def verify_price_ceiling(payload, policy, settings, client):
    """A later sales-price edit must not invalidate an earlier operator ceiling."""
    try:
        profile = policy['models'][payload['model']]
        input_limit = Decimal(str(profile['input_ceiling_per_million'])) / 1000000
        output_limit = Decimal(str(profile['output_ceiling_per_million'])) / 1000000
        response = await client.get(settings.litellm_url.rstrip('/') + '/model/info',
            headers={'Authorization': 'Bearer ' + settings.litellm_master_key}, timeout=5)
        if response.status_code != 200:
            stop('model_budget_price_verification_unavailable')
        rows = [r for r in response.json()['data'] if r.get('model_name') == payload['model']]
        if not rows:
            raise ValueError()
        observed = []
        for row in rows:
            values = {**(row.get('model_info') or {}), **(row.get('litellm_params') or {})}
            if profile.get('native_models') and values.get('model') not in profile['native_models']:
                stop('model_budget_route_verification_unavailable')
            if values.get('input_cost_per_token') is None or values.get('output_cost_per_token') is None:
                raise ValueError()
            for name, value in sorted(values.items()):
                if value is None or not ('cost_per_token' in name or 'input_token_cost' in name):
                    continue
                # The admitted request cannot select priority/flex/batch or long-lived cache.
                # /model/info also contains built-in prices for these unused products.
                if not re.fullmatch(r'(?:input_cost_per_token|output_cost_per_token|(?:explicit_)?cache_read_input_token_cost|cache_creation_input_token_cost)(?:_above_\d+k_tokens)?',name):
                    continue
                rate = Decimal(str(value))
                limit = output_limit if name.startswith('output_') else input_limit
                if not rate.is_finite() or rate < 0 or rate > limit:
                    stop('model_budget_price_ceiling_changed')
                observed.append((name, str(rate)))
        return hashlib.sha256(json.dumps(observed, sort_keys=True).encode()).hexdigest()
    except (httpx.HTTPError, ValueError, KeyError, TypeError, ArithmeticError):
        stop('model_budget_price_verification_unavailable')


def reserve(db, env_id, key, payload, settings, stamp=None, receipt_owner=None, price_fingerprint=None,
            policy_override=None,attempt_id=None,commit=True,dispatch=True):
    policy = policy_override if policy_override is not None else policy_for(settings, env_id)
    if policy is None:
        return payload, None
    stamp = stamp or now()
    try:
        starts, ends = (datetime.fromisoformat(policy[k].replace('Z', '+00:00')) for k in ('starts_at', 'ends_at'))
        if starts.tzinfo is None or ends.tzinfo is None or not starts <= stamp < ends:
            stop('model_budget_window_closed')
        window = policy['id']
        if not isinstance(window, str) or not 1 <= len(window) <= 80:
            raise ValueError()
        limit = nanos(policy['usd_limit'])
        if not 0 < limit <= nanos(100):
            raise ValueError()
    except (KeyError, TypeError, ValueError, ArithmeticError):
        stop('model_budget_invalid_policy')
    forwarded, amount, snapshot = quote(payload, policy)
    if amount>policy.get('max_call_nanos',amount):stop('model_turn_budget_exhausted')
    if policy.get('shared_pool'):
        from .managed_chat import lock_pool
        lock_pool(db)
    lock_account(db, env_id)
    from .litellm import model_user_id
    env = db.get(UserEnvironment, env_id)
    snapshot['receipt_owner'] = receipt_owner or model_user_id(env.issuer, env.subject)
    snapshot['price_fingerprint'] = price_fingerprint
    snapshot.update({k:policy[k] for k in ('funding','managed_wallet','opening_spend','pool_day') if k in policy})
    query=select(ModelCallReservation)
    if not policy.get('shared_pool'):query=query.where(ModelCallReservation.environment_id==env_id)
    rows = db.scalars(query).all()
    # Unknown old-window calls still occupy capacity; a new window cannot erase them.
    relevant = [r for r in rows if r.state!='released' and (r.window_id==window or
        (r.state in ('reserved','dispatched','blocked') and
         (r.snapshot.get('funding')=='free' if policy.get('shared_pool') else r.snapshot.get('funding')!='free')))]
    if policy.get('managed_wallet'):
        managed=[r for r in rows if r.snapshot.get('managed_wallet')]
        if managed:
            anchor=min(managed,key=lambda r:r.created_at).snapshot['opening_spend']
            snapshot['opening_spend']=anchor
            limit=policy['capital_nanos']-anchor
    if any(r.state == 'blocked' for r in relevant):
        stop('model_budget_receipt_requires_review')
    # The operator controls today's shared pool. Past reservations retain the
    # limit used at admission; their spend and outstanding holds still count.
    if not policy.get('managed_wallet') and not policy.get('shared_pool'):
        limit = min([limit] + [r.limit_nanos for r in relevant if r.window_id == window])
    held = sum(r.settled_nanos if r.state == 'settled' else r.reserved_nanos for r in relevant)
    # Cutting a function call short can leave unparseable arguments and no document.
    can_call_tool = (payload.get('tools') and payload.get('tool_choice') != 'none'
                     or payload.get('functions') and payload.get('function_call') != 'none')
    if held + amount > limit and policy.get('shared_pool') and not can_call_tool and any(
            m.get('role') == 'tool' for m in payload['messages']):
        # Use only the remaining sponsor budget. Apply the same output cap upstream and in the ledger.
        available = (limit - held) * 1000000 - snapshot['input_rate'] * snapshot['input_bound']
        affordable = available // snapshot['output_rate']
        if affordable >= min(512, snapshot['output_bound']):
            field = 'max_completion_tokens' if 'max_completion_tokens' in payload else 'max_tokens'
            forwarded, amount, resized = quote({**payload, field: int(affordable)}, policy)
            snapshot.update(resized)
    if held + amount > limit:
        stop('shared_free_budget_exhausted' if policy.get('shared_pool') else 'model_turn_budget_exhausted')
    attempt = str(attempt_id or uuid4())
    if db.get(ModelCallReservation,attempt) is not None:stop('model_call_already_submitted')
    row = ModelCallReservation(id=attempt, environment_id=env_id, window_id=window,
        model=payload['model'], key_hash=hashlib.sha256(key.encode()).hexdigest(),
        limit_nanos=limit, reserved_nanos=amount, state='dispatched' if dispatch else 'reserved', snapshot=snapshot)
    db.add(row)
    if commit:db.commit()
    else:db.flush()
    forwarded['metadata'] = {**forwarded.get('metadata', {}), 'q1f_attempt_id': attempt,
                             'litellm_call_id': attempt}
    forwarded['user'] = snapshot['receipt_owner']
    return forwarded, attempt


def claim_reserved(db,attempt):
    row=db.get(ModelCallReservation,str(attempt),populate_existing=True)
    if row is None or row.state!='reserved':stop('model_call_already_submitted')
    row.state='dispatched';db.flush()


def settle(db, attempt, usage):
    """Trusted response/receipt only. Missing/conflicting counts never free credit."""
    row = db.get(ModelCallReservation, attempt)
    if row is None:
        return False
    lock_account(db, row.environment_id)
    db.refresh(row)
    try:
        values = normalize_usage(usage, 'litellm')
        inp, out = values['input_tokens'], values['output_tokens']
        if inp is None or out is None:
            return False
        snap = row.snapshot
        amount = (snap['input_rate'] * inp + snap['output_rate'] * out + 999999) // 1000000
        if row.state == 'settled':
            if any(row.usage.get(k) is not None and value is not None and row.usage[k]!=value
                   for k,value in values.items()):
                row.state = 'blocked'
            else:
                # Native spend receipts can omit cache details present in the stream.
                # Missing evidence cannot contradict or erase an observed category.
                row.usage={k:row.usage.get(k) if row.usage.get(k) is not None else value for k,value in values.items()}
        elif row.state == 'dispatched':
            if inp > snap['input_bound'] or out > snap['output_bound'] or amount > row.reserved_nanos:
                row.state = 'blocked'
            else:
                row.state, row.settled_nanos, row.usage = 'settled', amount, values
                row.settled_at = now()
        db.commit()
        return row.state == 'settled'
    except (ValueError, TypeError, ArithmeticError):
        row.state = 'blocked'
        db.commit()
        return False


async def recover_pending(db, env, key, settings, client):
    """Read-only LiteLLM receipt recovery; one exact attempt/owner/key match only."""
    return await recover_pending_hash(db,env,hashlib.sha256(key.encode()).hexdigest(),settings,client)


async def recover_pending_hash(db,env,key_hash,settings,client):
    if not re.fullmatch(r'[0-9a-f]{64}',key_hash):return
    rows = db.scalars(select(ModelCallReservation).where(
        ModelCallReservation.environment_id == env.id,
        ModelCallReservation.key_hash == key_hash,
        ModelCallReservation.state.in_(('dispatched','settled')),
        ModelCallReservation.created_at < now() - timedelta(seconds=15))
        .order_by(ModelCallReservation.created_at.desc()).limit(20)).all()
    db.rollback()
    if not rows:
        return
    owners = {r.snapshot['receipt_owner'] for r in rows}
    if len(owners) != 1:
        return
    owner = owners.pop()
    try:
        response = await client.get(settings.litellm_url.rstrip('/') + '/spend/logs/v2',
            headers={'Authorization': 'Bearer ' + settings.litellm_master_key},
            params={'api_key': key_hash, 'user_id': owner, 'page_size': 100,
                    'start_date': (min(r.created_at for r in rows) - timedelta(seconds=2)).strftime('%Y-%m-%d %H:%M:%S'),
                    'end_date': (now()+timedelta(seconds=2)).strftime('%Y-%m-%d %H:%M:%S')}, timeout=5)
        body = response.json()
        if response.status_code != 200 or body.get('total_pages') != 1 or body.get('total_is_capped'):
            return  # No partial page may release an unknown reservation.
        receipts = body.get('data')
        if not isinstance(receipts, list) or len(receipts) >= 100:
            return
        for row in rows:
            if row.snapshot.get('native_spend_nanos') is not None:continue
            names=row.snapshot.get('native_models')
            if not names:
                profile=json.loads(getattr(settings,'managed_chat_profiles','{}')).get(row.model,{})
                same_price=(profile and nanos(profile['input_ceiling_per_million'])==row.snapshot['input_rate']
                            and nanos(profile['output_ceiling_per_million'])==row.snapshot['output_rate'])
                names=profile.get('native_models',[row.model]) if same_price else [row.model]
            matches = []
            for receipt in receipts:
                metadata = receipt.get('metadata') or {}
                if isinstance(metadata, str):
                    metadata = json.loads(metadata)
                if (isinstance(metadata, dict) and receipt.get('litellm_call_id') == row.id
                        and receipt.get('status') == 'success'
                        and receipt.get('user') == owner and receipt.get('api_key') == key_hash
                        and receipt.get('model') in names):
                    matches.append(receipt)
            if len(matches) == 1:
                receipt = matches[0]
                usage = receipt.get('usage') or {k: receipt[k] for k in (
                    'prompt_tokens', 'completion_tokens', 'total_tokens') if k in receipt}
                if settle(db, row.id, usage):
                    row=db.get(ModelCallReservation,row.id)
                    actual=nanos(receipt.get('spend'))
                    if actual>row.reserved_nanos:row.state='blocked'
                    else:
                        row.snapshot={**row.snapshot,'native_spend_nanos':actual,'native_model':receipt.get('model')}
                        row.settled_nanos=actual
                    db.commit()
    except Exception:
        # Recovery availability never turns unknown consumption into zero.
        db.rollback()


async def relay(response, attempt, session_factory, is_stream, chunks=None):
    """Pass response bytes unchanged; observe bounded SSE receipts on the side."""
    pending = bytearray()
    latest_usage = None
    def record(packet):
        if isinstance(packet, dict) and isinstance(packet.get('usage'), dict):
            with session_factory() as db:
                settle(db, attempt, packet['usage'])
    async for chunk in chunks if chunks is not None else response.aiter_bytes():
        if isinstance(chunk, str):
            chunk = chunk.encode()
        if len(pending) + len(chunk) <= 1024 * 1024:
            pending.extend(chunk)
            if is_stream:
                while b'\n' in pending:
                    line, _, remaining = pending.partition(b'\n')
                    pending = bytearray(remaining)
                    if line.startswith(b'data:'):
                        try:
                            if line[5:].strip() == b'[DONE]':
                                if latest_usage is not None:
                                    record({'usage': latest_usage})
                            else:
                                packet = json.loads(line[5:])
                                if isinstance(packet, dict) and isinstance(packet.get('usage'), dict):
                                    latest_usage = packet['usage']
                        except (ValueError, UnicodeError):
                            pass
        else:
            # Do not parse a truncated/oversized packet as a valid receipt.
            pending = bytearray(b'x' * (1024 * 1024 + 1))
        yield chunk
    if not is_stream and pending and len(pending) <= 1024 * 1024:
        try:
            raw = pending[5:] if is_stream and pending.startswith(b'data:') else pending
            record(json.loads(raw))
        except (ValueError, UnicodeError):
            pass


def guarded_response(upstream, client, attempt, session_factory, is_stream, sponsored=None):
    import anyio
    from .daily_chat import SponsoredResponse, relay as free_relay

    class BudgetResponse(SponsoredResponse):
        async def pump(self):
            source = free_relay(upstream, sponsored, is_stream) if sponsored else None
            async with self.sender:
                connected = True
                async for chunk in relay(upstream, attempt, session_factory, is_stream, source):
                    if connected:
                        try:
                            await self.sender.send(chunk)
                        except anyio.BrokenResourceError:
                            connected = False

    return BudgetResponse(upstream, client, sponsored, is_stream)
