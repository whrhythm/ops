"""Stateless document renderer for the loopback port in the no-egress office pod."""
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile, ZIP_STORED, ZIP_DEFLATED
from font_metrics import calibrated_document


MAX_INPUT = 16 * 1024 * 1024
MAX_OUTPUT = 12 * 1024 * 1024
MAX_PAGES = 40
slot = threading.BoundedSemaphore(1)


def check_document(source):
    with ZipFile(io.BytesIO(source)) as package:
        parts = package.infolist()
        names = {entry.filename for entry in parts}
        if (len(parts) != len(names) or len(parts) > 2048 or
                sum(p.file_size for p in parts) > 48 * 1024**2 or
                not {'[Content_Types].xml', 'word/document.xml'} <= names):
            raise ValueError('unsupported_package')
        for part in parts:
            name = part.filename.lower()
            if (part.compress_type not in (ZIP_STORED, ZIP_DEFLATED) or
                    part.flag_bits & 1 or part.file_size > MAX_INPUT or
                    name.startswith(('/', 'word/embeddings/', 'word/activex/')) or
                    '..' in name.split('/') or '\\' in name or 'vbaproject' in name):
                raise ValueError('unsupported_part')
            if not name.endswith(('.xml', '.rels')):
                continue
            xml = package.read(part)
            if b'<!DOCTYPE' in xml.upper() or b'<!ENTITY' in xml.upper():
                raise ValueError('unsupported_xml')
            tree = ElementTree.fromstring(xml)
            if name == '[content_types].xml' and not any(
                    item.get('PartName') == '/word/document.xml' and
                    item.get('ContentType') == 'application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml'
                    for item in tree):
                raise ValueError('not_docx')
            if name.endswith('.rels'):
                for rel in tree:
                    if rel.get('TargetMode', '').lower() == 'external' and not rel.get('Type', '').endswith('/hyperlink'):
                        raise ValueError('external_resource')
            elif name.startswith('word/'):
                instructions = ' '.join((node.text or '') + ' ' + ' '.join(node.attrib.values())
                                        for node in tree.iter() if node.tag.endswith(('}instrText', '}fldSimple')))
                if re.search(r'\b(?:DDE(?:AUTO)?|INCLUDETEXT|INCLUDEPICTURE|LINK|DATABASE)\b', instructions, re.I):
                    raise ValueError('external_field')


def command(args, folder, timeout):
    proc = subprocess.Popen(args, cwd=folder, start_new_session=True,
                            env={'HOME': str(folder), 'TMPDIR': str(folder), 'LANG': 'C.UTF-8',
                                 'PATH': '/usr/bin:/bin'}, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        stdout, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
        raise
    if proc.returncode:
        raise ValueError('conversion_failed')
    return stdout


def render(source):
    check_document(source)
    with tempfile.TemporaryDirectory(prefix='pages-') as tmp:
        folder = Path(tmp)
        (folder / 'source.docx').write_bytes(calibrated_document(source))
        command(['soffice', '--headless', '--norestore', '--nodefault', '--nofirststartwizard',
                 '-env:UserInstallation=' + (folder/'profile').as_uri(),
                 '--convert-to', 'pdf:writer_pdf_Export', '--outdir', tmp, 'source.docx'], folder, 45)
        if (folder/'source.pdf').stat().st_size > 32 * 1024**2:
            raise ValueError('pdf_too_large')
        info = command(['pdfinfo', 'source.pdf'], folder, 5)
        count = re.search(rb'^Pages:\s+(\d+)\s*$', info, re.M)
        if not count or not 1 <= int(count[1]) <= MAX_PAGES:
            raise ValueError('page_limit')
        command(['pdftoppm', '-scale-to', '1500', '-jpeg', '-jpegopt', 'quality=86',
                 'source.pdf', 'page'], folder, 40)
        pages = sorted(folder.glob('page-*.jpg'), key=lambda f: int(f.stem[5:]))
        if len(pages) != int(count[1]) or sum(f.stat().st_size for f in pages) > MAX_OUTPUT:
            raise ValueError('page_output_limit')
        return {'sha256': hashlib.sha256(source).hexdigest(),
                'pages': [base64.b64encode(f.read_bytes()).decode('ascii') for f in pages]}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        if self.path != '/pages':
            return self.send_error(404)
        length = self.headers.get('Content-Length', '')
        if not re.fullmatch(r'[0-9]{1,8}', length) or not 0 < int(length) <= MAX_INPUT:
            return self.send_error(413)
        if not slot.acquire(blocking=False):
            return self.send_error(429)
        try:
            self.connection.settimeout(15)
            source = self.rfile.read(int(length))
            if len(source) != int(length):
                return self.send_error(400)
            body = json.dumps(render(source), separators=(',', ':')).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)
        except (BadZipFile, KeyError, ValueError, ElementTree.ParseError):
            self.send_error(422)
        except (OSError, subprocess.TimeoutExpired):
            self.send_error(503)
        finally:
            slot.release()


if __name__ == '__main__':
    ThreadingHTTPServer(('127.0.0.1', 8124), Handler).serve_forever()
