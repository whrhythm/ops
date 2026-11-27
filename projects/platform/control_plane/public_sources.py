"""Bounded public HTTPS reads and a non-executable JSON adapter language."""
import hashlib
import json
import re
import ssl
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urlsplit, urljoin

import httpx
import jwt
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, model_validator

from .agent_mcp import PublicTransport


def public_url(value):
    p = urlsplit(value)
    if (len(value) > 800 or p.scheme != 'https' or not p.hostname or p.username or p.password
            or p.port not in (None, 443) or p.fragment or '%' in p.netloc
            or not re.fullmatch(r'[A-Za-z0-9.-]+', p.hostname) or '.' not in p.hostname
            or p.hostname.endswith('.') or any(ord(c) < 33 for c in value)
            or re.search(r'(token|key|secret|password|auth)=', p.query, re.I)):
        raise ValueError('public_https_url_required')
    return value


async def fetch_public(url):
    url = public_url(url)
    server = {'url': url, 'host': urlsplit(url).hostname}
    async with httpx.AsyncClient(transport=PublicTransport(server, maximum=262144),
                                timeout=12, follow_redirects=False, trust_env=False) as client:
        async with client.stream('GET', url, headers={'Accept': 'application/json, text/html;q=0.8'}) as response:
            if response.status_code != 200:
                return {'ok': False, 'code': 'source_http_error', 'status': response.status_code}
            content = await response.aread()
            if len(content) > 262144:
                raise ValueError('source_too_large')
            text = content.decode('utf-8')
            return {'ok': True, 'url': url, 'fetched_at': datetime.now(timezone.utc).isoformat(),
                    'sha256': hashlib.sha256(content).hexdigest(), 'text': text}


async def read_source(settings, url):
    public_url(url)
    if not settings.network_tools_enabled or not settings.mcp_gateway_url or len(settings.mcp_gateway_token) < 32:
        raise ValueError('source_discovery_unavailable')
    tls = ssl.create_default_context(cafile=settings.workspace_service_ca)
    async with httpx.AsyncClient(verify=tls, trust_env=False, timeout=20) as client:
        try:
            response = await client.post(settings.mcp_gateway_url.rstrip('/') + '/public-read',
                headers={'Authorization': 'Bearer ' + settings.mcp_gateway_token}, json={'url': url})
        except httpx.HTTPError:
            return {'ok': False, 'code': 'source_unavailable'}
        if response.status_code != 200:
            return {'ok': False, 'code': 'source_unavailable'}
        result = response.json()
        if not isinstance(result, dict) or len(json.dumps(result)) > 400000:
            raise ValueError('source_invalid_response')
        return result


class Page(HTMLParser):
    def __init__(self, url):
        super().__init__()
        self.url, self.links, self.words, self.hidden = url, [], [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'): self.hidden += 1
        if tag == 'a' and len(self.links) < 30:
            href = dict(attrs).get('href', '')
            try:
                url = public_url(urljoin(self.url, href))
                if url not in self.links: self.links.append(url)
            except ValueError:
                pass

    def handle_endtag(self, tag):
        if tag in ('script', 'style'): self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden and data.strip(): self.words.append(data.strip())


def sample(value, depth=0):
    if depth > 7: return '[nested]'
    if isinstance(value, dict): return {k: sample(v, depth+1) for k, v in list(value.items())[:30]}
    if isinstance(value, list): return [sample(v, depth+1) for v in value[:2]]
    return value[:500] if isinstance(value, str) else value


def inspection(result, settings, environment_id, project_id):
    if not result.get('ok'): return result
    text, url = result['text'], result['url']
    try:
        data = json.loads(text)
        view = {'format': 'json', 'sample': sample(data)}
        if len(json.dumps(view)) > 24000:
            return {'ok': False, 'code': 'source_structure_too_large'}
    except (ValueError, RecursionError):
        page = Page(url); page.feed(text)
        view = {'format': 'html', 'text': ' '.join(page.words)[:7000], 'links': page.links}
    claims = {'aud': 'source-probe', 'sub': environment_id, 'project': project_id, 'url': url,
              'sha256': result['sha256'], 'format': view['format'],
              'exp': int(datetime.now(timezone.utc).timestamp()) + 3600}
    return {'ok': True, 'source_receipt': jwt.encode(claims, settings.librechat_service_token, algorithm='HS256'),
            'url': url, 'fetched_at': result['fetched_at'], 'untrusted_external_data': True, **view}


def verify_receipt(receipt, settings, environment_id, project_id):
    try:
        value = jwt.decode(receipt, settings.librechat_service_token, algorithms=['HS256'], audience='source-probe')
        if value['sub'] != environment_id or value['project'] != project_id or value['format'] != 'json':
            raise ValueError('source_scope_invalid')
        public_url(value['url'])
        return value
    except (jwt.PyJWTError, KeyError) as error:
        raise ValueError('source_receipt_invalid') from error


class Adapter(BaseModel):
    model_config = ConfigDict(extra='forbid')
    url: str = Field(max_length=800)
    records_path: list[StrictStr | StrictInt] = Field(max_length=10)
    fields: dict[str, list[StrictStr | StrictInt]] = Field(min_length=1, max_length=16)

    @model_validator(mode='after')
    def validate_paths(self):
        public_url(self.url)
        for label, path in self.fields.items():
            if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,47}', label) or len(path) > 10:
                raise ValueError('source_field_invalid')
        return self


def at(value, path):
    for key in path:
        if isinstance(value, dict) and isinstance(key, str): value = value[key]
        elif isinstance(value, list) and type(key) is int and 0 <= key < len(value): value = value[key]
        else: raise ValueError('source_schema_changed')
    return value


def project(adapter, text):
    try:
        data = json.loads(text)
        records = at(data, adapter.records_path)
        if not isinstance(records, list): raise ValueError('source_records_required')
        result = []
        for record in records[:40]:
            row = {}
            for label, path in adapter.fields.items():
                if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,47}', label) or len(path) > 10:
                    raise ValueError('source_field_invalid')
                value = at(record, path)
                if isinstance(value, (list, dict)) or (isinstance(value, str) and len(value) > 800):
                    raise ValueError('source_scalar_required')
                row[label] = value
            result.append(row)
        if len(json.dumps(result)) > 24000: raise ValueError('source_output_too_large')
        return {'records': result, 'record_count': len(records), 'truncated': len(records) > 40}
    except (KeyError, IndexError, TypeError, json.JSONDecodeError, RecursionError) as error:
        raise ValueError('source_schema_changed') from error


async def execute_network(settings, program, arguments):
    if arguments: raise ValueError('source_arguments_invalid')
    adapter = Adapter.model_validate(program['adapter'])
    result = await read_source(settings, adapter.url)
    if not result.get('ok'): raise ValueError(result.get('code', 'source_unavailable'))
    return json.dumps({'source': adapter.url, 'fetched_at': result['fetched_at'],
        'untrusted_external_data': True, **project(adapter, result['text'])}, ensure_ascii=False)
