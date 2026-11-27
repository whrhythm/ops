"""Metered CodeAPI gateway. Only verified LibreChat identities can reserve compute."""
import asyncio
import hashlib
import json
import re
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import func, select

from .auth import Principal
from .database import get_db
from .models import CapacityAccount, CodeExecution, OfficeTask, now
from .settings import get_settings
from .workspace_gateway import own_environment

FIELDS = ('code_runs_monthly', 'code_timeout_seconds', 'code_concurrency')
# Transfers consume bounded I/O admission, not the plan's compute slots.
UPLOAD_CONCURRENCY = 4


def code_rights(plan):
    defaults = {'basic': (300, 15, 1), 'pro': (1500, 15, 2)}.get(plan.get('id'), (30, 10, 1))
    return {key: plan.get(key) if plan.get(key) is not None else value for key, value in zip(FIELDS, defaults)}


def month_key():
    return now().astimezone(timezone(timedelta(hours=8))).strftime('%Y-%m')


def usage(db, env):
    account = db.get(CapacityAccount, env.id)
    rights = code_rights(account.plan if account else {})
    count = db.scalar(select(func.count()).select_from(CodeExecution).where(
        CodeExecution.environment_id == env.id, CodeExecution.period == month_key(),
        CodeExecution.operation == 'exec', CodeExecution.state != 'rejected'))
    active = db.scalar(select(func.count()).select_from(CodeExecution).where(
        CodeExecution.environment_id == env.id, CodeExecution.operation == 'exec',
        CodeExecution.state.in_(('running', 'unknown')), CodeExecution.lease_until > now()))
    return {**rights, 'period': month_key(), 'used_runs': count, 'active_runs': active,
            'remaining_runs': max(0, rights['code_runs_monthly'] - count),
            'memory_mib': 256, 'accounting': 'dispatched_runs', 'timezone': 'Asia/Shanghai'}


def authenticate(token, settings):
    if not settings.code_execution_enabled or not settings.code_execution_public_key:
        raise HTTPException(503, 'code_execution_unavailable')
    try:
        claims = jwt.decode(token, settings.code_execution_public_key, algorithms=['EdDSA'],
                            issuer='librechat', audience='codeapi',
                            options={'require': ['exp', 'iat', 'sub', 'platform_subject', 'platform_issuer']})
        if (claims['platform_issuer'].rstrip('/') != settings.oidc_issuer.rstrip('/') or
                not isinstance(claims['platform_subject'], str) or not 1 <= len(claims['platform_subject']) <= 255 or
                claims['exp'] - claims['iat'] > 300):
            raise ValueError()
        return Principal(settings.oidc_issuer, claims['platform_subject'])
    except (jwt.InvalidTokenError, ValueError, TypeError, AttributeError):
        raise HTTPException(403, 'code_execution_identity_required') from None


def reserve(db, principal, key, digest, operation='exec', transfer_bytes=0, task=None):
    env = own_environment(db, principal)
    # The existing environment row serializes admission across API replicas.
    db.refresh(env, with_for_update=True)
    if env.status in ('suspended', 'deleted'):
        raise HTTPException(403, 'code_execution_account_disabled')
    existing = db.scalar(select(CodeExecution).where(CodeExecution.environment_id == env.id, CodeExecution.request_key == key))
    if existing:
        if task is not None:
            saved = db.get(OfficeTask, existing.id)
            if saved is not None and existing.request_hash == digest and saved.conversation_id == task['conversation_id']:
                db.commit()
                return existing
        raise HTTPException(409, 'code_execution_already_submitted')
    state = usage(db, env)
    if state['remaining_runs'] < 1:
        raise HTTPException(429, 'code_execution_quota_exhausted')
    if operation == 'upload':
        uploads, transferred = db.execute(select(func.count(), func.coalesce(func.sum(CodeExecution.transfer_bytes), 0)).where(
            CodeExecution.environment_id == env.id, CodeExecution.period == state['period'],
            CodeExecution.operation == 'upload', CodeExecution.state != 'rejected')).one()
        if uploads >= state['code_runs_monthly'] * 4 or transferred + transfer_bytes > state['code_runs_monthly'] * 16 * 1024 * 1024:
            raise HTTPException(429, 'code_execution_upload_quota_exhausted')
    active = state['active_runs']
    limit = state['code_concurrency']
    if operation == 'upload':
        active = db.scalar(select(func.count()).select_from(CodeExecution).where(
            CodeExecution.environment_id == env.id, CodeExecution.operation == 'upload',
            CodeExecution.state.in_(('running', 'unknown')), CodeExecution.lease_until > now()))
        limit = UPLOAD_CONCURRENCY
    if active >= limit:
        # Admission was rejected before dispatch. Let native LibreChat wait for
        # parallel file preparation, but never encourage retry of uncertain work.
        unknown = db.scalar(select(func.count()).select_from(CodeExecution).where(
            CodeExecution.environment_id == env.id, CodeExecution.operation == operation,
            CodeExecution.state == 'unknown',
            CodeExecution.lease_until > now()))
        raise HTTPException(429, 'code_execution_busy',
                            headers={'Retry-After': '1'} if not unknown else None)
    row = CodeExecution(environment_id=env.id, request_key=key, request_hash=digest,
                        operation=operation, transfer_bytes=transfer_bytes,
                        period=state['period'], state='running', timeout_seconds=state['code_timeout_seconds'],
                        lease_until=now() + timedelta(seconds=get_settings().code_execution_lease_seconds))
    db.add(row)
    if task is not None:
        db.flush()
        db.add(OfficeTask(id=row.id, environment_id=env.id,
                         conversation_id=task['conversation_id'], payload=task['payload'], state='accepted'))
    db.commit()
    return row


async def reserve_when_available(db, principal, key, digest, wait_seconds, task=None):
    """Wait briefly only for a definite pre-dispatch concurrency rejection.

    `reserve` commits a usage row only after passing the limit. `code_execution_busy`
    with Retry-After is raised before any row or provider request exists, so repeating
    the same reservation is safe. Quota exhaustion, an unknown in-flight result, and
    every other error are returned immediately; neither quota nor concurrency rights
    are enlarged by this queue.
    """
    deadline = time.monotonic() + max(0, wait_seconds)
    while True:
        try:
            return reserve(db, principal, key, digest, task=task)
        except HTTPException as error:
            retry_after = (error.headers or {}).get('Retry-After')
            if (
                error.status_code != 429
                or error.detail != 'code_execution_busy'
                or retry_after is None
                or not re.fullmatch(r'[1-9]\d{0,2}', retry_after)
            ):
                raise
            delay = int(retry_after)
            remaining = deadline - time.monotonic()
            if delay > remaining:
                raise
            # Release the environment row lock before waiting. Reuse the same
            # idempotency key; no row exists for a busy rejection.
            db.rollback()
            await asyncio.sleep(delay)


router = APIRouter(prefix='/internal/v1/codeapi', tags=['code-execution'])


def output_expiry(value):
    if not isinstance(value, str):
        return None
    try:
        expiry = datetime.fromisoformat(value)
        return expiry if expiry.tzinfo is not None else None
    except ValueError:
        return None


def record_completion(task, row, result, expires_at):
    expiry = output_expiry(expires_at)
    task.result = {**result, '_output_expires_at': expiry.isoformat()} if expiry else result
    task.state = row.state = 'complete'
    row.completed_at = task.updated_at = now()


def task_view(task):
    result = None
    expiry = None
    if isinstance(task.result, dict):
        expiry = output_expiry(task.result.get('_output_expires_at'))
        result = {key: task.result[key] for key in ('session_id',) if key in task.result}
        for key in ('stdout', 'stderr'):
            if isinstance(task.result.get(key), str):
                result[key] = task.result[key][:8192]
        result['files'] = [
            {key: str(file[key])[:512] for key in ('id', 'name', 'session_id', 'storage_session_id') if key in file}
            for file in task.result.get('files', [])[:50] if isinstance(file, dict)
        ] if isinstance(task.result.get('files'), list) else []
    payload = task.payload or {}
    verified = task.result.get('_office_inputs') if isinstance(task.result, dict) else None
    files = verified if isinstance(verified, list) else payload.get('files')
    inputs = [
        {key: file[key] for key in ('id', 'storage_session_id', 'name', 'kind', 'version', 'sha256', 'mime', 'size')
         if isinstance(file.get(key), (str, int))}
        for file in files[:200] if isinstance(file, dict)
    ] if isinstance(files, list) else []
    return {'id': task.id, 'conversation_id': task.conversation_id, 'state': task.state,
            'created_at': task.created_at, 'updated_at': task.updated_at, 'result': result,
            'inputs': inputs, 'output_expires_at': expiry,
            'outputs_expired': expiry is not None and now() >= expiry}


async def recover_task(db, task, authorization):
    """Reconcile the original provider receipt. Never infer failure from a lost response."""
    if task.result is not None or task.state in ('accepted', 'cancelled'):
        return task
    settings = get_settings()
    row = db.get(CodeExecution, task.id)
    cancelling = task.state == 'cancelling'
    key = row.request_key
    db.commit()
    try:
        async with httpx.AsyncClient(base_url=settings.code_execution_url, timeout=10,
                                     follow_redirects=False, trust_env=False) as client:
            route = 'tasks/' + key
            result = await client.request('POST' if cancelling else 'GET',
                                          route + '/cancel' if cancelling else route,
                                          headers={'Authorization': authorization})
            if len(result.content) > settings.code_execution_transfer_bytes:
                raise ValueError()
            db.refresh(task, with_for_update=True)
            if task.result is not None or task.state == 'cancelled':
                db.commit()
                return task
            if result.status_code == 200:
                value = result.json()
                if not isinstance(value, dict):
                    raise ValueError()
                if value.get('state') == 'complete' and isinstance(value.get('result'), dict):
                    record_completion(task, row, value['result'], value.get('output_expires_at'))
                elif cancelling and value.get('state') == 'cancelled':
                    task.state = row.state = 'cancelled'
                    row.completed_at = now()
                elif task.state != 'cancelling':
                    task.state = 'running' if value.get('state') == 'running' else 'unknown'
            elif result.status_code == 404 and task.state != 'cancelling':
                task.state = 'unknown'
            task.updated_at = now()
            db.commit()
    except (httpx.HTTPError, ValueError):
        db.rollback()  # Preserve the last observed state on a transient read failure.
    return task


def mark_uncertain(db, row, durable):
    """A late failure must not overwrite a receipt another request already recovered."""
    db.rollback()
    task = db.get(OfficeTask, row.id) if durable else None
    if task is not None:
        db.refresh(task, with_for_update=True)
    db.refresh(row)
    if task is not None and task.result is not None:
        row.state = 'complete'
    elif row.state == 'running':
        row.state = 'unknown'
        if task is not None and task.state not in ('cancelled', 'cancelling'):
            task.state = 'unknown'
            task.updated_at = now()
    db.commit()


@router.get('/tasks')
def tasks(request: Request, response: Response, db=Depends(get_db)):
    if not get_settings().office_tasks_enabled:
        raise HTTPException(404, 'office_tasks_unavailable')
    principal = authenticate(request.headers.get('authorization', '').removeprefix('Bearer '), get_settings())
    env = own_environment(db, principal)
    if env.status in ('suspended', 'deleted'):
        raise HTTPException(403, 'code_execution_account_disabled')
    conversation = request.query_params.get('conversation_id', '')
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,128}', conversation):
        raise HTTPException(400, 'office_task_invalid')
    response.headers['Cache-Control'] = 'no-store'
    keys = request.query_params.getlist('call_key')
    if len(keys) > 16 or any(not re.fullmatch(r'[a-f0-9]{64}', key) for key in keys):
        raise HTTPException(400, 'office_task_invalid')
    query = select(OfficeTask, CodeExecution.request_key).join(
        CodeExecution, CodeExecution.id == OfficeTask.id
    ).where(
        OfficeTask.environment_id == env.id, OfficeTask.conversation_id == conversation
    )
    if keys:
        query = query.where(CodeExecution.environment_id == env.id, CodeExecution.request_key.in_(keys))
    receipts = db.execute(query.order_by(OfficeTask.created_at.desc(), OfficeTask.id.desc()).limit(50))
    return {'tasks': [{**task_view(task), 'call_key': key if re.fullmatch(r'[a-f0-9]{64}', key) else None}
                      for task, key in receipts]}


def own_task(db, task_id, request):
    if not get_settings().office_tasks_enabled:
        raise HTTPException(404, 'office_tasks_unavailable')
    authorization = request.headers.get('authorization', '')
    principal = authenticate(authorization.removeprefix('Bearer '), get_settings())
    env = own_environment(db, principal)
    if env.status in ('suspended', 'deleted'):
        raise HTTPException(403, 'code_execution_account_disabled')
    task = db.get(OfficeTask, task_id)
    if task is None or task.environment_id != env.id:
        raise HTTPException(404, 'office_task_not_found')
    return task, authorization


@router.get('/tasks/{task_id}')
async def task_status(task_id: str, request: Request, response: Response, db=Depends(get_db)):
    task, authorization = own_task(db, task_id, request)
    response.headers['Cache-Control'] = 'no-store'
    result = task_view(await recover_task(db, task, authorization))
    key = db.get(CodeExecution, task.id).request_key
    return {**result, 'call_key': key if re.fullmatch(r'[a-f0-9]{64}', key) else None}


@router.post('/tasks/{task_id}/cancel')
async def cancel_task(task_id: str, request: Request, response: Response, db=Depends(get_db)):
    task, authorization = own_task(db, task_id, request)
    db.refresh(task, with_for_update=True)
    response.headers['Cache-Control'] = 'no-store'
    if task.state in ('complete', 'cancelled'):
        return task_view(task)
    created = task.created_at.replace(tzinfo=timezone.utc) if task.created_at.tzinfo is None else task.created_at
    if now() - created >= timedelta(days=6):
        raise HTTPException(409, 'office_task_receipt_expired')
    if task.state == 'accepted':
        # The same row lock guards the accepted -> running transition.
        row = db.get(CodeExecution, task.id)
        row.state = 'rejected'
        row.completed_at = now()
        task.state = 'cancelled'
    else:
        task.state = 'cancelling'
    task.updated_at = now()
    db.commit()
    return task_view(await recover_task(db, task, authorization))


@router.get('/tasks/{task_id}/outputs/{file_id}')
async def task_output(task_id: str, file_id: str, request: Request, db=Depends(get_db)):
    task, _ = own_task(db, task_id, request)
    view = task_view(task)
    result = view['result']
    if task.state != 'complete' or result is None:
        raise HTTPException(404, 'office_output_not_found')
    if view['outputs_expired']:
        raise HTTPException(410, 'office_output_expired')
    for file in result['files']:
        if file.get('id') != file_id:
            continue
        session = file.get('storage_session_id') or file.get('session_id') or result.get('session_id')
        if isinstance(session, str):
            # Execution outputs always belong to the current user's CodeAPI
            # bucket. Resolve its id from the signed identity at the provider.
            return await forward_gateway(f'download/{session}/{file_id}', request, db,
                                         params={'kind': 'user'})
    raise HTTPException(404, 'office_output_not_found')


@router.post('/account-usage/refill')
def refill_tokens(request: Request, response: Response, body: dict, db=Depends(get_db)):
    from . import daily_free, token_refill
    settings = get_settings()
    principal = authenticate(request.headers.get('authorization', '').removeprefix('Bearer '), settings)
    env = own_environment(db, principal)
    if not (settings.token_refill_enabled and settings.managed_chat_budget_enabled and
            daily_free.account_enabled(settings, env) and daily_free.trial_open(settings, now())):
        raise HTTPException(403, 'refill_unavailable')
    response.headers['Cache-Control'] = 'no-store'
    return token_refill.refill(db, env, body, settings)


@router.post('/account-usage/consent')
def authorize_chat_balance(request: Request, response: Response, body: dict, db=Depends(get_db)):
    from . import daily_chat, daily_free
    settings=get_settings()
    principal=authenticate(request.headers.get('authorization','').removeprefix('Bearer '),settings)
    env=own_environment(db,principal)
    if settings.token_refill_enabled:
        raise HTTPException(410,'legacy_consent_closed')
    if not (settings.managed_chat_budget_enabled and daily_free.account_enabled(settings,env) and daily_free.trial_open(settings,now())):
        raise HTTPException(403,'managed_consent_unavailable')
    if body.get('confirm') is not True or body.get('day')!=daily_free.billing_day(now()):
        raise HTTPException(409,'confirmation_required_for_today')
    daily_chat.record_balance_consent(db,env,body['day'])
    response.headers['Cache-Control']='no-store'
    return {'authorized':True,'day':body['day']}


@router.get('/account-usage')
def account_usage(request: Request, response: Response, db=Depends(get_db)):
    """Read existing entitlement observations; never provision, charge or wake storage."""
    from .daily_free import visible_allowance
    from .model_wallet import WalletOut, wallet_out
    settings = get_settings()
    principal = authenticate(request.headers.get('authorization', '').removeprefix('Bearer '), settings)
    env = own_environment(db, principal)
    account = db.get(CapacityAccount, env.id)
    response.headers['Cache-Control'] = 'no-store'
    plan = account.plan if account else {}
    return {'plan_name': plan.get('name', plan.get('id', '')),
            'wallet': WalletOut.model_validate(wallet_out(db, env, settings)),
            'code_execution': usage(db, env), **visible_allowance(db, env, settings)}


@router.get('/{path:path}', operation_id='codeapi_read')
@router.post('/{path:path}', operation_id='codeapi_submit')
@router.delete('/{path:path}', operation_id='codeapi_delete')
async def gateway(path: str, request: Request, db=Depends(get_db)):
    return await forward_gateway(path, request, db, params=request.query_params)


async def forward_gateway(path, request, db, *, params):
    settings = get_settings()
    authorization = request.headers.get('authorization', '')
    principal = authenticate(authorization.removeprefix('Bearer '), settings)
    env = own_environment(db, principal)
    if env.status == 'suspended':
        raise HTTPException(403, 'code_execution_account_disabled')
    # No arbitrary proxy, stateful command, hosted app or programmatic bypass.
    valid = ((request.method == 'POST' and path in ('exec', 'upload', 'upload/batch')) or
             (request.method == 'GET' and re.fullmatch(r'(download/[\w-]+/[\w-]+|files/[\w-]+|sessions/[\w-]+/objects/[\w-]+)', path)) or
             (request.method == 'DELETE' and re.fullmatch(r'(files/[\w-]+/[\w-]+|sessions/[\w-]+/objects/[\w-]+)', path)))
    if not valid:
        raise HTTPException(404, 'code_execution_route_unavailable')
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > settings.code_execution_transfer_bytes:
            raise HTTPException(413, 'code_execution_file_too_large')
    row = None
    durable = False
    if path == 'exec':
        try:
            value = json.loads(body)
            if not isinstance(value, dict) or not isinstance(value.get('code'), str) or not value['code'] or not isinstance(value.get('lang'), str):
                raise ValueError()
        except (ValueError, TypeError):
            raise HTTPException(400, 'code_execution_invalid_request') from None
        durable_key = request.headers.get('x-starbridge-execution-key')
        key = durable_key or request.headers.get('idempotency-key') or str(uuid4())
        if not re.fullmatch(r'[a-zA-Z0-9_-]{16,128}', key):
            raise HTTPException(400, 'code_execution_invalid_request')
        conversation = request.headers.get('x-starbridge-conversation', '')
        durable = settings.office_tasks_enabled and durable_key is not None
        if durable and not re.fullmatch(r'[a-zA-Z0-9_-]{1,128}', conversation):
            raise HTTPException(400, 'office_task_invalid')
        task_input = {'conversation_id': conversation, 'payload': value} if durable else None
        row = await reserve_when_available(
            db,
            principal,
            key,
            hashlib.sha256(body).hexdigest(),
            settings.code_execution_busy_wait_seconds,
            task=task_input,
        )
        if durable:
            task = db.get(OfficeTask, row.id)
            # Only the transaction that changes accepted to running can
            # submit. A repeated POST must read the original receipt, even if
            # the provider has lost the Redis keys that prevent re-execution.
            db.refresh(task, with_for_update=True)
            if task.result is not None:
                return Response(json.dumps({k: v for k, v in task.result.items() if k != '_output_expires_at'}).encode(), media_type='application/json',
                                headers={'Cache-Control': 'no-store', 'X-Starbridge-Task': row.id})
            if task.state in ('cancelled', 'cancelling'):
                raise HTTPException(409, 'office_task_cancelled')
            created = task.created_at.replace(tzinfo=timezone.utc) if task.created_at.tzinfo is None else task.created_at
            if now() - created >= timedelta(days=6):
                raise HTTPException(409, 'office_task_receipt_expired')
            if task.state != 'accepted':
                task = await recover_task(db, task, authorization)
                if task.result is not None:
                    return Response(json.dumps({k: v for k, v in task.result.items() if k != '_output_expires_at'}).encode(), media_type='application/json',
                                    headers={'Cache-Control': 'no-store', 'X-Starbridge-Task': row.id})
                raise HTTPException(409, 'office_task_already_submitted',
                                    headers={'Cache-Control': 'no-store', 'X-Starbridge-Task': row.id})
            task.state = 'running'
            task.updated_at = now()
        # Never accept a user-supplied larger timeout or alternate runtime route.
        value['timeout'] = row.timeout_seconds * 1000
        value.pop('runtime_session_hint', None)
        body = json.dumps(value).encode()
    elif request.method == 'POST':
        row = reserve(db, principal, str(uuid4()), hashlib.sha256(body).hexdigest(), 'upload', len(body))
    db.commit()  # Do not hold a transaction or lock while waiting on execution.
    try:
        async with httpx.AsyncClient(base_url=settings.code_execution_url, timeout=settings.code_execution_timeout_seconds,
                                     follow_redirects=False, trust_env=False) as client:
            async with client.stream(request.method, path, content=bytes(body),
                                     params=params,
                                     headers={'Authorization': authorization,
                                              **({'X-Starbridge-Execution-Key': key} if path == 'exec' and durable else {}),
                                              'Content-Type': request.headers.get('content-type', 'application/json')}) as upstream:
                content = bytearray()
                async for chunk in upstream.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > settings.code_execution_transfer_bytes:
                        raise HTTPException(413, 'code_execution_file_too_large')
                status = upstream.status_code
                expires_at = upstream.headers.get('x-starbridge-output-expires-at')
                media = upstream.headers.get('content-type', 'application/octet-stream')
                # CodeAPI rejects uploads/reads in its limiter before handling them.
                # Preserve its bounded wait hint for LibreChat's existing recovery;
                # do not forward provider diagnostics or replay code submissions.
                retry_after = upstream.headers.get('retry-after', '')
                retry_headers = (
                    {'Retry-After': retry_after}
                    if status == 429 and (request.method == 'GET' or path in ('upload', 'upload/batch'))
                    and re.fullmatch(r'[1-9]\d{0,3}', retry_after) and int(retry_after) <= 3600
                    else None
                )
        if row:
            task = db.get(OfficeTask, row.id) if path == 'exec' and durable else None
            if task is not None:
                db.refresh(task, with_for_update=True)
            if task is not None and status < 300:
                value = json.loads(content)
                if not isinstance(value, dict):
                    raise ValueError('invalid_execution_response')
            if task is None:
                row.state = 'complete' if status < 300 else 'rejected' if status in (400, 401, 403, 413, 429) else 'unknown'
                row.completed_at = now()
            elif task.state != 'cancelled':
                if status < 300:
                    if task.result is None:
                        record_completion(task, row, value, expires_at)
                elif task.result is None and task.state != 'cancelling':
                    task.state = 'unknown'
                    row.state = 'rejected' if status in (400, 401, 403, 413, 429) else 'unknown'
                    row.completed_at = task.updated_at = now()
            db.commit()
            if task is not None and task.state == 'cancelled':
                raise HTTPException(409, 'office_task_cancelled')
        if status >= 400:
            raise HTTPException(status if status in (400, 403, 404, 409, 413, 429) else 503,
                                'code_execution_provider_failed', headers=retry_headers)
        return Response(bytes(content), status_code=status, headers={'Content-Type': media, 'Cache-Control': 'no-store'})
    except HTTPException:
        if row:
            mark_uncertain(db, row, durable)
        raise
    except (httpx.HTTPError, ValueError):
        if row:
            mark_uncertain(db, row, durable)
        # Unclear submissions remain charged. Never retry a code execution automatically.
        raise HTTPException(503, 'code_execution_result_unconfirmed') from None
