"""Single-worker candidate service for authenticated scan-to-Word tasks."""
from concurrent.futures import ThreadPoolExecutor
import asyncio
from http.client import HTTPConnection
from hashlib import sha256
from hmac import compare_digest
import json
import logging
import os
from pathlib import Path
import re
from shutil import copyfile
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Lock
from time import time
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request, Response

MAX_INPUT = 10 * 1024**2
TTL = 24 * 60 * 60
ID = re.compile(r'[a-f0-9]{64}')
CONVERSATION = re.compile(r'[a-f0-9-]{36}')
TASK_TIMEOUT = 600
log = logging.getLogger('uvicorn.error')


def selected_pages(value):
    if not value:
        return None
    if len(value) > 100:
        raise HTTPException(422, 'invalid_page_range')
    pages = set()
    for part in value.split(','):
        if not re.fullmatch(r'[1-9][0-9]?(?:-[1-9][0-9]?)?', part):
            raise HTTPException(422, 'invalid_page_range')
        bounds = part.split('-')
        start, end = int(bounds[0]), int(bounds[-1])
        if not start <= end <= 20:
            raise HTTPException(422, 'invalid_page_range')
        pages.update(range(start, end + 1))
    return sorted(pages)


def create_app(root, token):
    if len(token) < 32:
        raise ValueError('missing OCR service token')
    root = Path(root)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    pool = ThreadPoolExecutor(max_workers=1)
    lock = Lock()
    page_slot = asyncio.Lock()
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware('http')
    async def authenticate(request, call_next):
        if request.url.path in ('/ready', '/live') and request.method == 'GET':
            return await call_next(request)
        if not compare_digest(request.headers.get('authorization', '').encode(), f'Bearer {token}'.encode()):
            return Response(status_code=403)
        subject = request.headers.get('x-scan-owner', '')
        if not ID.fullmatch(subject):
            return Response(status_code=403)
        request.state.owner = subject
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    def write(folder, value):
        staged = folder / 'state.tmp'
        staged.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
        staged.replace(folder / 'state.json')

    def read(folder):
        try:
            return json.loads((folder / 'state.json').read_text(encoding='utf-8'))
        except FileNotFoundError:
            raise HTTPException(404) from None

    def run(folder, source, state):
        execution = state['execution_id']
        log.info('scan execution %s started', execution)
        try:
            with TemporaryDirectory(prefix='office-ocr-') as private:
                job = Path(private)
                (job / 'input.pdf').write_bytes(source)
                if state.get('pages'):
                    (job / 'pages.json').write_text(json.dumps(state['pages']))
                subprocess.run([sys.executable, '-m', 'control_plane.scan_job', private],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
                               timeout=TASK_TIMEOUT - 10)
                result = json.loads((job / 'result.json').read_text(encoding='utf-8'))
                with lock:
                    for file in ('result.docx', *(f'page-{page["page"]}.png' for page in result['pages'])):
                        copyfile(job / file, folder / file)
                    write(folder, {**state, 'status': 'complete', 'result': result})
            log.info('scan execution %s complete, %d pages', execution, len(result['pages']))
        except Exception as error:
            with lock:
                write(folder, {**state, 'status': 'failed'})
            log.warning('scan execution %s failed: %s', execution, type(error).__name__)

    # A stopped process never reports its previous in-progress work as complete.
    for entry in root.glob('*/*/state.json'):
        state = read(entry.parent)
        if state['status'] == 'running':
            write(entry.parent, {**state, 'status': 'failed'})

    def conversation_dir(request, conversation):
        if not CONVERSATION.fullmatch(conversation):
            raise HTTPException(400)
        return root / request.state.owner / conversation

    @app.get('/ready')
    def ready():
        return Response(status_code=204)

    @app.post('/word-pages')
    async def word_pages(request: Request):
        if os.environ.get('STARBRIDGE_WORD_PAGES_ENABLED') != 'true':
            raise HTTPException(404)
        if page_slot.locked():
            raise HTTPException(429)
        async with page_slot:
            return await render_request(request)

    async def render_request(request):
        source = bytearray()
        async for chunk in request.stream():
            if len(source) + len(chunk) > 16 * 1024**2:
                raise HTTPException(413)
            source.extend(chunk)

        def convert():
            conn = HTTPConnection('127.0.0.1', 8124, timeout=100)
            try:
                conn.request('POST', '/pages', bytes(source))
                output = conn.getresponse()
                if output.status != 200:
                    raise HTTPException(output.status if output.status in (413, 422, 429) else 503)
                size = 17 * 1024**2
                body = output.read(size + 1)
                if len(body) > size:
                    raise HTTPException(413)
                return Response(body, media_type='application/json')
            except OSError:
                raise HTTPException(503) from None
            finally:
                conn.close()

        return await asyncio.to_thread(convert)

    @app.get('/live')
    def live():
        with lock:
            for path in root.glob('*/*/state.json'):
                state = read(path.parent)
                if state['status'] == 'running' and time() - state['created_at'] >= TASK_TIMEOUT:
                    return Response(status_code=503)
        return Response(status_code=204)

    @app.post('/scans/{conversation}')
    async def start(request: Request, conversation: str):
        folder = conversation_dir(request, conversation)
        source = bytearray()
        async for chunk in request.stream():
            if len(source) + len(chunk) > MAX_INPUT:
                raise HTTPException(413)
            source.extend(chunk)
        if not source.startswith((b'%PDF-', b'\x89PNG\r\n\x1a\n', b'\xff\xd8\xff')):
            raise HTTPException(415)
        pages = selected_pages(request.query_params.get('pages'))
        if pages and not source.startswith(b'%PDF-') and pages != [1]:
            raise HTTPException(422, 'invalid_page_range')
        source_digest = sha256(source).hexdigest()
        digest = source_digest
        if pages:
            page_key = ','.join(map(str, pages))
            digest = sha256(f'{source_digest}:{page_key}'.encode()).hexdigest()
        with lock:
            if (folder / 'state.json').exists():
                old = read(folder)
                if old['digest'] == digest and old['status'] != 'failed' and time() - old['created_at'] <= TTL:
                    return {k: old[k] for k in ('status', 'digest', 'execution_id')}
                if old['status'] == 'running':
                    raise HTTPException(409)
            for path in root.glob('*/*/state.json'):
                other = read(path.parent)
                if other['status'] == 'running':
                    raise HTTPException(429)
                if time() - other['created_at'] > TTL:
                    for name in ('source.pdf', 'result.docx', 'state.json'):
                        (path.parent / name).unlink(missing_ok=True)
                    for image in path.parent.glob('page-*.png'):
                        image.unlink(missing_ok=True)
            folder.mkdir(mode=0o700, parents=True, exist_ok=True)
            for image in folder.glob('page-*.png'):
                image.unlink(missing_ok=True)
            state = {'status': 'running', 'digest': digest, 'source_digest': source_digest,
                     'pages': pages, 'created_at': int(time()),
                     'execution_id': uuid4().hex}
            write(folder, state)
            pool.submit(run, folder, bytes(source), state)
        return {k: state[k] for k in ('status', 'digest', 'execution_id')}

    def get_state(request, conversation):
        folder = conversation_dir(request, conversation)
        state = read(folder)
        if time() - state['created_at'] > TTL:
            raise HTTPException(410)
        return folder, state

    @app.get('/scans/{conversation}')
    def status(request: Request, conversation: str):
        return get_state(request, conversation)[1]

    def output(request, conversation, digest):
        folder, state = get_state(request, conversation)
        if state['status'] != 'complete' or state['digest'] != digest:
            raise HTTPException(404)
        return folder

    @app.get('/scans/{conversation}/{digest}/word')
    def word(request: Request, conversation: str, digest: str):
        with lock:
            content = (output(request, conversation, digest) / 'result.docx').read_bytes()
        return Response(content, media_type='application/vnd.openxmlformats-officedocument.wordprocessingml.document')

    @app.get('/scans/{conversation}/{digest}/pages/{number}')
    def page(request: Request, conversation: str, digest: str, number: int):
        with lock:
            file = output(request, conversation, digest) / f'page-{number}.png'
            if not 1 <= number <= 20 or not file.is_file():
                raise HTTPException(404)
            return Response(file.read_bytes(), media_type='image/png')

    return app


def app_from_env():
    return create_app(os.environ['STARBRIDGE_SCAN_STATE'], os.environ['STARBRIDGE_SCAN_TOKEN'])
