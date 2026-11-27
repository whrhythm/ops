"""Store completed Word outputs in their owner's workspace and pin each receipt to its bytes."""
import hashlib
import json
import re
from datetime import timezone

import httpx
from fastapi import HTTPException, Response
from sqlalchemy import or_, select
from starlette.concurrency import run_in_threadpool

from .models import OfficeTask
from .workspace_gateway import call_files


ROOT = '聊天文档'
MIME = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
MARKER = b'STARBRIDGE-VERSION-1\n'
SAVE_WARNING = 'Word 尚未保存到个人文件。请重试读取此任务，系统不会重新执行或扣费。'


def word_outputs(result):
    files = (result or {}).get('files')
    if not isinstance(files, list):
        return
    for file in files:
        if not isinstance(file, dict):
            continue
        name = file.get('name')
        session = file.get('storage_session_id') or file.get('session_id') or result.get('session_id')
        file_id = file.get('id')
        if (isinstance(name, str) and name.lower().endswith('.docx') and
                all(isinstance(x, str) and re.fullmatch(r'[a-zA-Z0-9_-]{1,128}', x) for x in (file_id, session))):
            yield file_id, session, name


def snapshot_body(packet, path, checksum):
    header_bytes, separator, body = packet[len(MARKER):].partition(b'\n')
    try:
        header = json.loads(header_bytes) if len(header_bytes) <= 8192 else None
    except (UnicodeError, ValueError):
        header = None
    if (not packet.startswith(MARKER) or not separator or not isinstance(header, dict)
            or header.get('version') != 1 or header.get('path') != path or header.get('size') != len(body)
            or header.get('sha256') != checksum or hashlib.sha256(body).hexdigest() != checksum):
        raise HTTPException(409, 'word_delivery_version_changed')
    return body


def snapshot(db, principal, settings, task, file_id, session, name, body):
    if not body or len(body) > 16 * 1024**2:
        raise HTTPException(413, 'word_delivery_size')
    # The name is display metadata. Never let it select a directory.
    leaf = re.sub(r'[\\/\x00-\x1f\x7f]', '_', name)[:-5].encode()[:180].decode(errors='ignore') + '.docx'
    checksum = hashlib.sha256(body).hexdigest()
    handle = hashlib.sha256(f'{session}/{file_id}/{checksum}'.encode()).hexdigest()
    folder = f'{ROOT}/{handle[:24]}'
    path = f'{folder}/{leaf}'
    versions = '.starbridge-versions/' + hashlib.sha256(path.encode()).hexdigest()
    immutable = versions + '/1.snapshot'
    stamp = task.created_at
    header = {'version': 1, 'requestId': handle, 'path': path, 'size': len(body), 'sha256': checksum,
              'savedAt': stamp.replace(tzinfo=stamp.tzinfo or timezone.utc).astimezone(timezone.utc).isoformat().replace('+00:00', 'Z'),
              'source': {'taskId': task.id, 'fileId': file_id}}
    packet = MARKER + json.dumps(header, ensure_ascii=False).encode() + b'\n' + body
    if len(packet) > 16 * 1024**2:
        raise HTTPException(413, 'word_delivery_size')
    for directory in (ROOT, folder, '.starbridge-versions', versions):
        try:
            call_files(db, principal, settings, 'POST', '/v1/directories', directory)
        except HTTPException as err:
            if err.status_code != 409:
                raise
    # Same envelope as Personal Files version history. Editing its visible working copy
    # must never change the bytes returned by the historical conversation.
    try:
        call_files(db, principal, settings, 'PUT', '/v1/file', immutable, body=packet)
    except HTTPException as err:
        if err.status_code != 409:
            raise
        previous = call_files(db, principal, settings, 'GET', '/v1/file', immutable)
        snapshot_body(previous.content, path, checksum)
    try:
        call_files(db, principal, settings, 'PUT', '/v1/file', path, body=body)
    except HTTPException as err:
        if err.status_code != 409:
            raise
        # A prior attempt may have committed the bytes before the receipt.
        existing = call_files(db, principal, settings, 'GET', '/v1/file', path)
        if hashlib.sha256(existing.content).hexdigest() != checksum:
            raise HTTPException(409, 'word_delivery_conflict') from None
    alias = hashlib.sha256(f'{task.id}/{session}/{file_id}'.encode()).hexdigest()[:21]
    entry = {'path': path, 'snapshot': immutable, 'sha256': checksum, 'session': session, 'alias': alias}
    db.refresh(task, with_for_update=True)
    saved = dict(task.result.get('_word_deliveries', {}))
    if file_id in saved and saved[file_id] != entry:
        db.rollback()
        raise HTTPException(409, 'word_delivery_conflict')
    task.result = {**task.result, '_word_deliveries': {**saved, file_id: entry}}
    db.commit()
    return entry


async def keep_words(db, principal, settings, task, authorization, user_id):
    """Retry file transfers only. The execution receipt is committed before any transfer."""
    missing = [x for x in word_outputs(task.result) if x[0] not in task.result.get('_word_deliveries', {})]
    if not missing:
        return True
    try:
        async with httpx.AsyncClient(base_url=settings.code_execution_url, timeout=30,
                                     trust_env=False, follow_redirects=False) as client:
            for file_id, session, name in missing:
                async with client.stream('GET', f'download/{session}/{file_id}', params={'kind': 'user', 'id': user_id},
                                         headers={'Authorization': authorization}) as response:
                    response.raise_for_status()
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > 16 * 1024**2:
                            raise HTTPException(413, 'word_delivery_size')
                await run_in_threadpool(snapshot, db, principal, settings, task, file_id, session, name, bytes(body))
    except (HTTPException, httpx.HTTPError):
        db.rollback()
        return False
    return True


def archived_download(db, principal, settings, env, session, file_id):
    entry = OfficeTask.result['_word_deliveries'][file_id]
    query = select(OfficeTask).where(OfficeTask.environment_id == env.id, OfficeTask.state == 'complete',
        or_(entry['alias'].as_string() == session, entry['session'].as_string() == session))
    task = db.scalar(query.order_by(OfficeTask.created_at.desc(), OfficeTask.id.desc()).limit(1))
    if task is None:
        return None
    saved = task.result['_word_deliveries'][file_id]
    packet = call_files(db, principal, settings, 'GET', '/v1/file', saved['snapshot']).content
    body = snapshot_body(packet, saved['path'], saved['sha256'])
    return Response(body, media_type=MIME, headers={'Cache-Control': 'no-store'})
