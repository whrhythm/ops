"""Shared public-web search for project tools and MCP clients."""

import asyncio
from collections import OrderedDict
from html import unescape
import json
import logging
import math
import os
import re
import time
from typing import Annotated
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse


logger = logging.getLogger('uvicorn.error')
# httpx includes the query string in INFO request logs.
logging.getLogger('httpx').setLevel(logging.WARNING)
TAGS = re.compile(r'<[^>]*>')
WORDS = re.compile(r'[a-z0-9]{2,}|[\u4e00-\u9fff]+')
TRACKING = {'gclid', 'fbclid', 'msclkid'}
PERMITTED_PERIODS = {'day', 'week', 'month', 'year'}
QUERY_FILLERS = re.compile(r'今天|今日|最近|近期|目前|现在|最新|查询|搜索|请问|情况|哪些|什么|如何|怎么|的')
QUERY_STOPWORDS = {'the', 'a', 'an', 'of', 'for', 'in', 'on', 'and', 'or', 'is', 'are',
                   'what', 'which', 'how', 'today', 'latest', 'recent', 'now'}
MCP_HOSTS = ('search-gateway', 'search-gateway.search-system',
             'search-gateway.search-system.svc', 'search-gateway.search-system.svc.cluster.local',
             'localhost', '127.0.0.1')


def plain(value, size):
    if not isinstance(value, str):
        return ''
    return ' '.join(TAGS.sub(' ', unescape(value)).split())[:size]


def public_link(value):
    if not isinstance(value, str) or len(value) > 1000 or any(ord(ch) < 33 for ch in value):
        return None
    try:
        u = urlsplit(value)
        host = u.hostname or ''
        if (u.scheme not in ('http', 'https') or u.username or u.password or '\\' in u.netloc
                or u.port not in (None, 80 if u.scheme == 'http' else 443)
                or not re.fullmatch(r'[a-z0-9.-]+\.[a-z][a-z0-9-]*', host)
                or host.endswith(('.local', '.internal', '.localhost', '.test', '.invalid', '.onion'))):
            return None
        query = [(k, v) for k, v in parse_qsl(u.query, keep_blank_values=True, max_num_fields=64)
                 if not k.lower().startswith('utm_') and k.lower() not in TRACKING]
        return urlunsplit((u.scheme, host, u.path or '/', urlencode(query), ''))
    except ValueError:
        return None


def ranked_results(query, rows):
    terms = set()
    # Query dates and conversational filler are not evidence of relevance.
    # This conservative gate rejects obvious mismatches; it does not certify a
    # source or replace the model's selection and original-page verification.
    for word in WORDS.findall(QUERY_FILLERS.sub(' ', query.casefold())):
        if '\u4e00' <= word[0] <= '\u9fff':
            terms.update(word[i:i + 2] for i in range(len(word) - 1))
        elif word not in QUERY_STOPWORDS and not word.isdigit():
            terms.add(word)
    ranked = []
    for row in rows[:40]:
        if not isinstance(row, dict) or row.get('sponsored') is True or row.get('is_advertisement') is True:
            continue
        title, snippet = plain(row.get('title'), 160), plain(row.get('content'), 400)
        link = public_link(row.get('url'))
        if not (title and snippet and link):
            continue
        matched = sum(word in title.casefold() or word in snippet.casefold() for word in terms)
        if terms and matched < max(1, math.ceil(len(terms) * 0.35)):
            continue
        score = sum(3 * (word in title.casefold()) + (word in snippet.casefold()) for word in terms)
        ranked.append((score, {'title': title, 'url': link, 'snippet': snippet}))
    results = {}
    for _, row in sorted(ranked, key=lambda item: -item[0]):
        link = urlsplit(row['url'])
        results.setdefault((link.netloc, link.path, link.query), row)
    return list(results.values())[:12]


class UpstreamUnavailable(Exception):
    pass


class SearchGateway:
    def __init__(self, upstream, ttl=60, capacity=128, clock=time.monotonic):
        self.upstream = upstream
        self.ttl = ttl
        self.capacity = capacity
        self.clock = clock
        self.cache = OrderedDict()
        self.slots = asyncio.Semaphore(4)

    async def _get(self, query, period):
        params = {'q': query, 'format': 'json', 'safesearch': '2'}
        if period:
            params['time_range'] = period
        async with httpx.AsyncClient(timeout=4.8, trust_env=False) as client:
            for attempt in range(2):
                try:
                    async with client.stream('GET', self.upstream, params=params) as response:
                        response.raise_for_status()
                        body = bytearray()
                        async for chunk in response.aiter_bytes():
                            body.extend(chunk)
                            if len(body) > 262144:
                                raise UpstreamUnavailable()
                    result = json.loads(body)
                    if not isinstance(result, dict) or not isinstance(result.get('results'), list):
                        raise UpstreamUnavailable()
                    return result
                except httpx.HTTPStatusError as error:
                    if attempt or error.response.status_code not in {502, 503, 504}:
                        raise UpstreamUnavailable() from None
                except (httpx.ConnectError, httpx.ConnectTimeout):
                    if attempt:
                        raise UpstreamUnavailable() from None
                except (ValueError, httpx.HTTPError):
                    raise UpstreamUnavailable() from None

    async def search(self, query, period=None, limit=5):
        query = query.strip()
        if (not 2 <= len(query) <= 180 or any(ord(ch) < 32 for ch in query)
                or any(word.startswith(('!', ':')) for word in query.split())
                or period is not None and period not in PERMITTED_PERIODS
                or not 1 <= limit <= 12):
            raise ValueError('invalid_search_parameters')
        key = (query, period)
        cached = self.cache.get(key)
        if cached and cached[0] > self.clock():
            self.cache.move_to_end(key)
            return {'status': 'results', 'results': cached[1][:limit]}, True
        self.cache.pop(key, None)
        try:
            async with asyncio.timeout(5.6), self.slots:
                payload = await self._get(query, period)
        except TimeoutError:
            raise UpstreamUnavailable() from None
        rows = ranked_results(query, payload['results'])
        status = 'results' if rows else 'degraded' if (
            payload.get('unresponsive_engines') or payload['results']) else 'no_results'
        logger.info('shared_search status=%s upstream_count=%d filtered_count=%d',
                    status, len(payload['results']), len(rows))
        if rows:
            self.cache[key] = (self.clock() + self.ttl, rows)
            self.cache.move_to_end(key)
            while len(self.cache) > self.capacity:
                self.cache.popitem(last=False)
        return {'status': status, 'results': rows[:limit]}, False


gateway = SearchGateway(os.getenv('SEARXNG_SEARCH_URL',
                                  'http://searxng.search-system.svc.cluster.local:8080/search'))
mcp = FastMCP('Shared Web Search', stateless_http=True, json_response=True,
              transport_security=TransportSecuritySettings(
                  allowed_hosts=[name for host in MCP_HOSTS for name in (host, host + ':*')],
                  allowed_origins=[],
              ))


@mcp.tool()
async def web_search(query: Annotated[str, Field(min_length=2, max_length=180)],
                     limit: Annotated[int, Field(ge=1, le=12)] = 5) -> dict:
    """Search public pages. Snippets are search excerpts, not verified page text."""
    try:
        result, _ = await gateway.search(query, limit=limit)
    except UpstreamUnavailable:
        return {'status': 'unavailable', 'results': []}
    return result


@mcp.custom_route('/healthz', methods=['GET'])
async def health(_request: Request):
    return JSONResponse({'ok': True})


@mcp.custom_route('/search', methods=['GET'])
async def search(request: Request):
    """SearXNG-compatible subset for the existing project's original-page reader."""
    try:
        result, cached = await gateway.search(request.query_params.get('q', ''),
                                              request.query_params.get('time_range'), limit=12)
    except ValueError:
        return JSONResponse({'status': 'invalid_query', 'results': []}, status_code=400)
    except UpstreamUnavailable:
        return JSONResponse({'status': 'unavailable', 'results': []}, status_code=503)
    rows = [{'title': row['title'], 'url': row['url'], 'content': row['snippet']}
            for row in result['results']]
    return JSONResponse({'status': result['status'], 'results': rows},
                        headers={'X-Search-Cache': 'hit' if cached else 'miss'})


app = mcp.streamable_http_app()
