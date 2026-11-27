"""Project tool that searches and reads public sources for any topic."""
import asyncio
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
import ipaddress
import json
import logging
import os
import re
import socket
from urllib.parse import urljoin, urlsplit, urlunsplit

from fastapi import FastAPI, Header, HTTPException, Query, Response
import httpx


app = FastAPI(title='项目联网搜索', version='1.0.0')
slots = asyncio.Semaphore(2)
logger = logging.getLogger('uvicorn.error')
date_pattern = r'(\d{4})[-年/](\d{1,2})[-月/](\d{1,2})'
article_stamp = re.compile(rf'{date_pattern}\s+\d{{1,2}}:\d{{2}}(?::\d{{2}})?\s+来源\s*[:：]')
related_section = re.compile(
    r'\s(?:上一篇文章|下一篇文章|热门推荐|相关推荐|猜你喜欢|相关新闻|'
    r'Related (?:Searches|Articles|Stories)|Recommended (?:Articles|Stories)|You may also like)'
    r'\s*[:：]?(?=\s)', re.I,
)
month_names = 'January February March April May June July August September October November December'.split()
month_numbers = {name.lower(): number for number, month in enumerate(month_names, 1)
                 for name in (month, month[:3])}
named_month = '|'.join(month_numbers)
named_dates = re.compile(
    rf'\b(?:(?P<day_first>\d{{1,2}})\s+(?P<month_second>{named_month})|'
    rf'(?P<month_first>{named_month})\.?\s+(?P<day_second>\d{{1,2}})),?\s+(?P<year>20\d{{2}})\b',
    re.I,
)
numeric_dates = re.compile(r'(?<!\d)(\d{1,2})/(\d{1,2})/(20\d{2})(?!\d)')
weekday_dates = re.compile(
    r'\b(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+(\d{1,2})/(\d{1,2})'
    r'(?=\s+(?:\|\s+)?(?:@\s|vs\.?\s|v\.\s))', re.I,
)
local_update = re.compile(
    r'(\d{1,2})月(\d{1,2})日\s*[（(](?:星期|周)([一二三四五六日天])[）)]\s*'
    r'\d{1,2}(?:时(?:\d{1,2}分)?|点|:\d{2})\s*(?:更新|发布)'
)


def public_host(url):
    address = urlsplit(url)
    if (len(url) > 1000 or address.scheme != 'https' or not address.hostname
            or address.username or address.password or address.port not in (None, 443)
            or address.fragment or not re.fullmatch(r'[a-zA-Z0-9.-]+', address.hostname)
            or '.' not in address.hostname or any(ord(char) < 33 for char in url)
            or re.search(r'(?:token|secret|password|key|auth)=', address.query, re.I)):
        raise ValueError('invalid_public_url')
    return address.hostname


def search_url(url):
    """Try the HTTPS form of a search hit without making a plaintext request."""
    address = urlsplit(url)
    if address.scheme not in ('http', 'https') or address.port not in (None, 443):
        raise ValueError('invalid_public_url')
    url = urlunsplit(('https', address.netloc, address.path or '/', address.query, ''))
    public_host(url)
    return url


class PublicTransport(httpx.AsyncHTTPTransport):
    async def handle_async_request(self, request):
        host = public_host(str(request.url))
        resolved = await asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        ips = [ipaddress.ip_address(item[4][0]) for item in resolved]
        if not ips or any(not ip.is_global or ip.is_multicast for ip in ips):
            raise ValueError('private_address')
        ip = next((value for value in ips if value.version == 4), ips[0])
        request.url = request.url.copy_with(host=str(ip))
        request.headers['Host'] = host
        request.extensions['sni_hostname'] = host
        return await super().handle_async_request(request)


class Page(HTMLParser):
    skipped = {'script', 'style', 'noscript', 'svg', 'nav', 'footer', 'header', 'aside'}

    def __init__(self):
        super().__init__()
        self.hidden = []
        self.text = []
        self.publication = []
        self.structured = []
        self.json_ld = None
        self.links = []
        self.anchor = None
        self.title = []
        self.in_title = False
        self.table = None
        self.row = None
        self.cell = None
        self.item_depth = 0
        self.item = []

    def boundary(self):
        if not self.item_depth and self.text and self.text[-1] != '\n':
            self.text.append('\n')

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == 'title':
            self.in_title = True
        if not self.hidden:
            if tag == 'li':
                if not self.item_depth:
                    self.boundary()
                    self.item = []
                self.item_depth += 1
            if tag in {'p', 'div', 'section', 'article', 'li', 'br', 'h1', 'h2', 'h3', 'h4'}:
                self.boundary()
            if tag == 'table':
                self.boundary()
                self.table = []
            elif tag == 'tr' and self.table is not None:
                self.row = []
            elif tag in {'td', 'th'} and self.row is not None:
                self.cell = []
        if tag == 'script' and (attributes.get('type') or '').strip().lower() == 'application/ld+json':
            self.json_ld = ''
        field = attributes.get('property') or attributes.get('name') or attributes.get('itemprop') or ''
        if field.lower() in {'article:published_time', 'datepublished', 'publishdate', 'pubdate', 'date'}:
            if tag == 'meta':
                self.publication.append(attributes.get('content') or '')
            if tag == 'time':
                self.publication.append(attributes.get('datetime') or '')
        if tag == 'time' and not self.hidden and (value := attributes.get('datetime')):
            try:
                timestamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
            except ValueError:
                pass
            else:
                if timestamp.tzinfo is not None:
                    self.text.append('[' + timestamp.isoformat() + ']')
        if tag == 'a' and not self.hidden and attributes.get('href'):
            self.anchor = (attributes['href'], [])
        if tag in self.skipped:
            self.hidden.append(tag)

    def handle_endtag(self, tag):
        if tag == 'title':
            self.in_title = False
        if not self.hidden:
            if tag == 'li' and self.item_depth:
                self.item_depth -= 1
                if not self.item_depth:
                    self.text.append(' '.join(self.item))
                    self.item = []
                    self.boundary()
            if tag in {'td', 'th'} and self.cell is not None:
                self.row.append(' '.join(self.cell))
                self.cell = None
            elif tag == 'tr' and self.row is not None:
                self.table.append(self.row)
                self.row = None
            elif tag == 'table' and self.table is not None:
                for row in self.table:
                    (self.item if self.item_depth else self.text).append(' | '.join(row))
                    self.boundary()
                self.table = None
            if tag in {'p', 'div', 'section', 'article', 'li', 'h1', 'h2', 'h3', 'h4'}:
                self.boundary()
        if tag == 'script' and self.json_ld is not None:
            if self.json_ld and len(self.structured) < 16:
                try:
                    self.structured.append(json.loads(self.json_ld))
                except (ValueError, RecursionError):
                    pass
            self.json_ld = None
        if tag == 'a' and self.anchor is not None:
            self.links.append(self.anchor)
            self.anchor = None
        if self.hidden and self.hidden[-1] == tag:
            self.hidden.pop()

    def handle_data(self, text):
        if self.json_ld is not None:
            if len(self.json_ld) + len(text) > 65536:
                self.json_ld = None
            else:
                self.json_ld += text
        if not self.hidden and (value := ' '.join(text.split())):
            if self.in_title:
                self.title.append(value)
            elif self.cell is not None:
                self.cell.append(value)
            elif self.item_depth:
                # Keep a card/list entry's entity, labels and values together.
                # A line-window must not detach a number from its owning entry.
                self.item.append(value)
            else:
                self.text.append(value)
            if self.anchor is not None:
                self.anchor[1].append(value)


async def fetch_bytes(client, url, maximum, **kwargs):
    async with client.stream('GET', url, **kwargs) as response:
        response.raise_for_status()
        raw = bytearray()
        async for chunk in response.aiter_bytes():
            raw.extend(chunk)
            if len(raw) > maximum:
                raise ValueError('response_too_large')
        return raw, response.encoding or 'utf-8', response.headers.get('content-type', '')


async def read_page(client, url):
    for _ in range(4):
        async with client.stream('GET', url) as response:
            if response.is_redirect and response.headers.get('location'):
                target = urljoin(url, response.headers['location'])
                if urlsplit(target).scheme != 'https':
                    raise ValueError('unsafe_redirect')
                url = search_url(target)
                continue
            response.raise_for_status()
            if not response.headers.get('content-type', '').lower().startswith(
                ('text/html', 'text/plain', 'application/xhtml+xml')
            ):
                raise ValueError('unsupported_type')
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                raw.extend(chunk)
                if len(raw) > 1048576:
                    raise ValueError('response_too_large')
            return url, raw.decode(response.encoding or 'utf-8', errors='replace')
    raise ValueError('redirect_limit')


async def original(client, url, counts):
    try:
        async with asyncio.timeout(5):
            url, html = await read_page(client, url)
    except httpx.HTTPStatusError as error:
        counts['http_' + str(error.response.status_code)] += 1
    except (TimeoutError, ValueError, OSError, LookupError, httpx.HTTPError):
        counts['unreadable'] += 1
    else:
        page = Page()
        page.feed(html)
        counts['read'] += 1
        return url, page
    return None


@app.get('/healthz')
def health():
    return {'ok': bool(os.getenv('SEARCH_PROVIDER_URL')), 'artifact': os.getenv('PROJECT_TOOL_ARTIFACT', '')}


def first_date(values):
    for value in values:
        match = re.search(date_pattern, value)
        if match:
            try:
                return date(*(int(part) for part in match.groups())).isoformat()
            except ValueError:
                continue
    return None


def structured_publication(blocks, url):
    page = urlsplit(url)
    for block in blocks:
        nodes = block if isinstance(block, list) else [block]
        if isinstance(block, dict) and isinstance(block.get('@graph'), list):
            nodes = [block, *block['@graph']]
        for node in nodes:
            if not isinstance(node, dict) or not isinstance(node.get('datePublished'), str):
                continue
            identity = node.get('mainEntityOfPage') or node.get('url') or node.get('@id')
            if isinstance(identity, dict):
                identity = identity.get('@id') or identity.get('url')
            if not isinstance(identity, str) or not identity or identity.startswith('#'):
                continue
            try:
                target = urlsplit(urljoin(url, identity))
            except ValueError:
                continue
            if (target.scheme == page.scheme and target.netloc == page.netloc and not target.fragment
                    and target.path.rstrip('/') == page.path.rstrip('/') and target.query == page.query):
                yield node['datePublished']


def publication_date(metadata, text, structured=(), url=''):
    visible = re.findall(
        rf'(?:发布(?:时间)?[:：]?\s*|发表于\s*)({date_pattern})|'
        rf'({date_pattern})(?:日)?(?:\s*\d{{1,2}}(?::\d{{2}}(?::\d{{2}})?|时))?\s*发布',
        text[:3500],
    )
    stamped = article_stamp.search(text[:4500])
    return first_date([*metadata, *structured_publication(structured, url), *([stamped.group()] if stamped else []),
                       *(row[0] or row[4] for row in visible)])


def article_text(parts):
    text = ' '.join(parts)
    text = re.sub(r' *\n *', '\n', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    related = related_section.search(text, 100)
    if related:
        text = text[:related.start()]
    return re.sub(r'https?://[^\s<>"，。]+', '［文中链接］', text)


def dated_content(text, cutoff, current):
    candidates = []

    def add(match, year, month, day):
        try:
            value = date(int(year), int(month), int(day)).isoformat()
        except ValueError:
            return
        if cutoff <= value <= current:
            candidates.append((value, match.start()))

    for match in re.finditer(date_pattern, text):
        add(match, *match.groups())
    for match in named_dates.finditer(text):
        month = match['month_first'] or match['month_second']
        add(match, match['year'], month_numbers[month.lower()], match['day_first'] or match['day_second'])
    numeric = list(numeric_dates.finditer(text))
    # Use numeric rows only when other rows establish the page's date convention.
    month_first = any(int(match[2]) > 12 for match in numeric)
    day_first = any(int(match[1]) > 12 for match in numeric)
    if month_first != day_first:
        for match in numeric:
            add(match, match[3], match[1] if month_first else match[2], match[2] if month_first else match[1])
    today = date.fromisoformat(current)
    oldest = date.fromisoformat(cutoff)
    weekdays = ('mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun')
    for match in weekday_dates.finditer(text):
        weekday = weekdays.index(match[1].lower())
        first, second = int(match[2]), int(match[3])
        matches = set()
        for year in range(oldest.year, today.year + 1):
            for month, day in ((first, second), (second, first)):
                try:
                    value = date(year, month, day)
                except ValueError:
                    continue
                if oldest <= value <= today and value.weekday() == weekday:
                    matches.add(value)
        if len(matches) == 1:
            value = matches.pop()
            candidates.append((value.isoformat(), match.start()))
    if not month_first and not day_first:
        # Relative dates explicitly tied to a visible date column may differ by a day across time zones.
        near_today = {today, today - timedelta(days=1)}
        for match in numeric:
            before = text[max(0, match.start() - 260):match.start()]
            if not re.search(r'\b(?:today|yesterday)\b\s*[.!]?\s*(?:NAME\s+)?DATE\b', before, re.I):
                continue
            first, second, year = map(int, match.groups())
            possible = {(value.year, value.month, value.day) for value in near_today
                        if year == value.year and (value.month, value.day) in ((first, second), (second, first))}
            if len(possible) == 1:
                add(match, *possible.pop())
    for match in local_update.finditer(text):
        weekday = '一二三四五六日'.index(match[3].replace('天', '日'))
        if (int(match[1]), int(match[2]), weekday) == (today.month, today.day, today.weekday()):
            candidates.append((current, match.start()))
    return max(candidates, key=lambda item: (item[0], -item[1]), default=None)


def source_excerpt(text, published, cutoff, current):
    if len(text) < 100:
        return None
    if published:
        if not cutoff <= published <= current:
            return None
        stamp = article_stamp.search(text[:4500])
        start = max(0, stamp.start() - 100) if stamp else 0
        evidence = {'published_on': published}
    else:
        dated = dated_content(text, cutoff, current)
        if not dated:
            return None
        value, offset = dated
        # The lead often identifies the subject and explains table columns.
        start = 0 if offset < 1500 else offset - 240
        evidence = {'content_on': value}
    # Keep whole lines: a half row loses its column/competition context.
    if start:
        start = text.rfind('\n', 0, start) + 1
    excerpt = text[start:start + 2800]
    if start + len(excerpt) < len(text) and '\n' in excerpt:
        excerpt = excerpt.rsplit('\n', 1)[0]
    if start and not published:
        excerpt = text[:220] + ' … ' + excerpt
    raw = excerpt.encode('utf-8')
    if len(raw) > 4100:
        excerpt = raw[:4100].decode('utf-8', errors='ignore')
        if '\n' in excerpt:
            excerpt = excerpt.rsplit('\n', 1)[0]
    return {**evidence, 'excerpt': excerpt}


def linked_articles(url, page, cutoff, today):
    # A dated index link is a discovery signal. Only the fetched article may supply evidence.
    host = urlsplit(url).hostname
    for href, parts in page.links:
        link = urljoin(url, href)
        try:
            link = search_url(link)
        except ValueError:
            continue
        stamp = first_date([' '.join(parts), urlsplit(link).path])
        if link != url and urlsplit(link).hostname == host and stamp and cutoff <= stamp <= today:
            yield link


async def search_hits(client, provider, query, time_range):
    params = {'q': query, 'format': 'json', 'safesearch': '1'}
    if time_range:
        params['time_range'] = time_range
    try:
        raw, _, _ = await fetch_bytes(client, provider, 262144, params=params)
        rows = json.loads(raw)['results']
        return rows if isinstance(rows, list) else None
    except (KeyError, TypeError, ValueError, httpx.HTTPError):
        return None


async def research(response: Response, query: str = Query(min_length=2, max_length=180),
                   recent_days: int = Query(30, ge=1, le=30),
                   x_expected_artifact: str = Header(default='')):
    artifact = os.getenv('PROJECT_TOOL_ARTIFACT', '')
    if artifact and x_expected_artifact != artifact:
        raise HTTPException(409, '工具运行版本不匹配')
    response.headers['X-Project-Tool-Artifact'] = artifact
    query = query.strip()
    if (len(query) < 2 or any(ord(c) < 32 for c in query)
            or any(word.startswith(('!', ':')) for word in query.split())
            or re.search(r'(?i)https?://|\b(?:bearer|api[_ -]?key|password|token)\s*[:=]|sk-[a-z0-9_-]{12,}', query)):
        raise HTTPException(422, '需要公开查询词')
    provider = os.getenv('SEARCH_PROVIDER_URL')
    if not provider or slots.locked():
        raise HTTPException(503, '搜索服务当前不可用')
    counts = Counter()
    sources = {}
    urls, links = [], []
    today = datetime.now(timezone(timedelta(hours=8))).date()
    cutoff, current = (today - timedelta(days=recent_days)).isoformat(), today.isoformat()
    try:
        async with slots, asyncio.timeout(18):
            async with httpx.AsyncClient(timeout=6, trust_env=False, follow_redirects=False) as index:
                ranked = await asyncio.gather(
                    search_hits(index, provider, query, 'day' if recent_days == 1 else 'month'),
                    search_hits(index, provider, query, None),
                )
            if all(rows is None for rows in ranked):
                raise HTTPException(503, '搜索服务未返回有效结果')
            candidates = {}
            for position in range(12):
                for rows in ranked:
                    if not rows or position >= len(rows) or not isinstance(rows[position], dict):
                        continue
                    item = rows[position]
                    try:
                        url = search_url(item['url'])
                    except (KeyError, TypeError, ValueError):
                        counts['invalid_url'] += 1
                        continue
                    candidates.setdefault(url, str(item.get('title', ''))[:160])
            counts['index_hits'] = len(candidates)
            transport = PublicTransport(retries=0, trust_env=False,
                                        limits=httpx.Limits(max_connections=6, max_keepalive_connections=0))
            async with httpx.AsyncClient(transport=transport, timeout=5, trust_env=False) as client:
                discovered = {}

                async def collect(url, title, follow_links):
                    page_result = await original(client, url, counts)
                    if page_result is None:
                        return
                    canonical, page = page_result
                    text = article_text(page.text)
                    published = publication_date(page.publication, text, page.structured, canonical)
                    evidence = source_excerpt(text, published, cutoff, current)
                    if evidence:
                        sources[url] = {
                            'title': (' '.join(page.title) or title)[:160], 'url': canonical,
                            'read_at': datetime.now(timezone.utc).isoformat(),
                            **evidence,
                        }
                        # The parent tasks finish out of order. Rank by input URL after the fetch.
                    elif follow_links:
                        discovered[url] = list(dict.fromkeys(linked_articles(canonical, page, cutoff, current)))[:2]
                        counts['no_recent_publication'] += 1

                urls = list(candidates)[:12]
                await asyncio.gather(*(collect(url, candidates[url], True) for url in urls))
                links = list(dict.fromkeys(
                    link for url in urls for link in discovered.get(url, ()) if link not in candidates
                ))[:6]
                if len({source['url'] for source in sources.values()}) < 3:
                    await asyncio.gather(*(collect(link, '', False) for link in links))
    except TimeoutError:
        counts['deadline'] += 1
    ordered = {}
    for url in (*urls, *links):
        if url in sources:
            source = sources[url]
            ordered.setdefault(source['url'], source)
    sources = list(ordered.values())[:3]
    logger.info('project_search_summary counts=%s sources=%d', dict(counts), len(sources))
    return {'status': 'sources_read' if sources else 'no_readable_sources', 'sources': sources,
            'external_content_is_untrusted': True}


def excerpt_window(text, focus='', offset=0):
    lines = text.splitlines()
    if not lines:
        return '', 0
    start = min(offset, len(text))
    if start < len(text) and start and text[start] != '\n' and text[start - 1] != '\n':
        line_start = text.rfind('\n', 0, start) + 1
        line_end = text.find('\n', start)
        if line_end < 0:
            line_end = len(text)
        if line_end - line_start <= 3300:
            start = line_start
    if focus and not offset:
        terms = set(re.findall(r'[a-z0-9]{2,}|[\u4e00-\u9fff]{2,}', focus.casefold()))
        expanded = set()
        for term in terms:
            if '\u4e00' <= term[0] <= '\u9fff':
                expanded.update(term[i:i+2] for i in range(len(term)-1))
            else:
                expanded.add(term)
        positions, position = [], 0
        for line in lines:
            positions.append(position)
            position += len(line) + 1
        best = max(range(len(lines)), key=lambda i: sum(t in ' '.join(lines[max(0,i-1):i+5]).casefold()
                                                       for t in expanded))
        start = positions[max(0, best-2)]
    end = min(len(text), start + 3300)
    if end < len(text):
        boundary = text.rfind('\n', start, end)
        if boundary > start + 100:
            end = boundary
    return text[start:end], end


@app.get('/research', operation_id='project_web', description='Separate search discovery and original page reading.')
async def web(response: Response, operation: str = '', query: str = '', period: str = '',
              url: str = '', focus: str = '', offset: int = 0, recent_days: int = 30,
              x_expected_artifact: str = Header(default='')):
    if not operation:
        return await research(response, query, recent_days, x_expected_artifact)
    artifact = os.getenv('PROJECT_TOOL_ARTIFACT', '')
    if artifact and x_expected_artifact != artifact:
        raise HTTPException(409, '工具运行版本不匹配')
    response.headers['X-Project-Tool-Artifact'] = artifact
    common = {'operation': operation, 'external_content_is_untrusted': True}
    if operation == 'search':
        if (not 2 <= len(query.strip()) <= 180 or period not in ('', 'day', 'week', 'month', 'year')
                or url or focus or offset or any(ord(c) < 32 for c in query)
                or any(word.startswith(('!', ':')) for word in query.split())
                or re.search(r'(?i)https?://|\b(?:bearer|api[_ -]?key|password|token)\s*[:=]', query)):
            raise HTTPException(422, '搜索参数不符合约定')
        provider = os.getenv('SEARCH_PROVIDER_URL')
        if not provider:
            raise HTTPException(503, '搜索暂不可用')
        async with httpx.AsyncClient(timeout=8, trust_env=False, follow_redirects=False) as client:
            rows = await search_hits(client, provider, query, period or None)
        if rows is None:
            return {**common, 'status': 'unavailable', 'candidates': []}
        candidates = {}
        for row in rows:
            try:
                target = search_url(row['url'])
                candidates.setdefault(target, {'url': target, 'title': str(row.get('title', ''))[:160],
                                               'snippet': str(row.get('content', ''))[:350]})
            except (ValueError, TypeError, KeyError):
                continue
        return {**common, 'status': 'candidates' if candidates else 'no_results',
                'candidates': list(candidates.values())[:8], 'originals_read': False}
    if operation != 'read' or query or period or len(focus) > 180 or not 0 <= offset <= 200000:
        raise HTTPException(422, '阅读参数不符合约定')
    try:
        public_host(url)
    except ValueError:
        raise HTTPException(422, '需要公开 HTTPS 网页') from None
    async with httpx.AsyncClient(transport=PublicTransport(retries=0, trust_env=False),
                                 timeout=8, trust_env=False, follow_redirects=False) as client:
        result = await original(client, url, Counter())
    if result is None:
        return {**common, 'status': 'unreadable', 'url': url, 'sources': []}
    canonical, page = result
    text = article_text(page.text)[:200000]
    excerpt, end = excerpt_window(text, focus, offset)
    if len(excerpt.strip()) < 80:
        return {**common, 'status': 'no_readable_text', 'url': canonical, 'sources': []}
    links = {}
    for href, label in page.links:
        try:
            target = search_url(urljoin(canonical, href))
            if target != canonical:
                links.setdefault(target, {'url': target, 'title': ' '.join(label)[:80]})
        except ValueError:
            continue
    # Links are discovery only. They do not count as read sources.
    source = {'url': canonical, 'title': ' '.join(page.title)[:160], 'excerpt': excerpt,
              'read_at': datetime.now(timezone.utc).isoformat(),
              'published_on': publication_date(page.publication, text, page.structured, canonical),
              'next_offset': end if end < len(text) else None}
    return {**common, 'status': 'source_read', 'sources': [source],
            'links': list(links.values())[:3]}
