from contextlib import contextmanager
from datetime import timedelta
import io
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zipfile import ZipFile

import httpx
from fastapi import HTTPException
from sqlalchemy import select

from control_plane import word_delivery
from control_plane.models import CodeExecution, OfficeTask, now
from control_plane.settings import get_settings
from test_api import harness
from test_capacity import capacity_harness
from test_code_execution import PREFIX, code_harness
from test_office_tasks import enabled, headers


@contextmanager
def provider():
    buf = io.BytesIO()
    with ZipFile(buf, 'w') as doc:
        doc.writestr('word/document.xml', '<document/>')
    original = buf.getvalue()
    calls = []

    class Service(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            calls.append('exec')
            self.send_response(200)
            self.send_header('X-Starbridge-Output-Expires-At', (now() + timedelta(days=1)).isoformat())
            self.end_headers()
            self.wfile.write(json.dumps({'session_id': 's' * 21, 'files': [
                {'id': 'f' * 21, 'name': '报告.docx'}]}).encode())

        def do_GET(self):
            calls.append('download')
            assert self.headers['Authorization'].startswith('Bearer ')
            assert 'id=mongo-user' in self.path and 'kind=user' in self.path
            self.send_response(200)
            self.end_headers()
            self.wfile.write(original)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Service)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/', original, calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_word_survives_expiry_and_storage_retry_without_second_execution(code_harness, monkeypatch):
    client, signed, *_ = code_harness
    objects = {}
    full = True

    def storage(db, principal, settings, method, endpoint, path, *, body=None):
        assert principal.subject == 'alice'
        if method == 'PUT':
            if full:
                raise HTTPException(507)
            if path in objects:
                raise HTTPException(409)
            objects[path] = body
        return httpx.Response(200, content=objects.get(path, b''))

    monkeypatch.setattr(word_delivery, 'call_files', storage)
    with provider() as (url, original, calls):
        monkeypatch.setenv('PLATFORM_CODE_EXECUTION_URL', url)
        enabled(monkeypatch)
        h = headers(signed, key='a'*64)
        body = {'code': 'make document', 'lang': 'py'}
        first = client.post(PREFIX+'exec', json=body, headers=h)
        assert first.status_code == 200 and '尚未保存' in first.json()['stderr']
        full = False
        second = client.post(PREFIX+'exec', json=body, headers=h)
        assert second.status_code == 200 and 'stderr' not in second.json()
        assert client.post(PREFIX+'exec', json=body, headers=h).status_code == 200
        assert calls == ['exec', 'download', 'download'] and len(objects) == 2
        with client.session_factory.begin() as db:
            task = db.scalar(select(OfficeTask))
            task.result = {**task.result, '_output_expires_at': (now()-timedelta(days=1)).isoformat()}
            task_id = task.id
            assert len(list(db.scalars(select(CodeExecution)))) == 1
    # Provider is offline. Both the historical native attachment and the pinned card must work.
    url = PREFIX+'tasks?conversation_id=test-conversation'
    receipt = client.get(url, headers=signed()).json()['tasks'][0]
    assert not receipt['outputs_expired'] and receipt['output_expires_at'] is None
    file = receipt['result']['files'][0]
    assert file['personal_path'].startswith('聊天文档/') and file['storage_session_id'] != 's'*21
    for session in ('s'*21, file['storage_session_id']):
        path = PREFIX+'download/'+session+'/'+file['id']+'?kind=user'
        assert client.get(path, headers=signed()).content == original
        assert client.get(path+'&id=other-user', headers=signed()).status_code == 403
        assert client.get(path, headers=signed('victim')).status_code == 404
    route = PREFIX+'tasks/'+task_id+'/outputs/'+file['id']
    assert client.get(route, headers=signed()).content == original
    objects[file['personal_path']] = b'changed'
    assert client.get(route, headers=signed()).content == original
    objects[next(key for key in objects if key.endswith('/1.snapshot'))] = b'corrupt'
    assert client.get(route, headers=signed()).status_code == 409
