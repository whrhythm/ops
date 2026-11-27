import asyncio
from datetime import datetime, timezone
import unittest
from unittest.mock import patch

from fastapi import Response
import httpx

from app import Page, article_text, dated_content, linked_articles, publication_date, read_page, research, search_url, source_excerpt, web, excerpt_window


class OriginalPagesTest(unittest.TestCase):
    def test_list_card_values_keep_their_entity_when_selecting_a_passage(self):
        page = Page()
        page.feed('<h1>Regional report</h1><ul>'
                  '<li><h3>Alpha station</h3><div>Updated today</div><p>18 units</p></li>'
                  '<li><h3>Beta station</h3><div>Nearby station</div><p>9/14 units</p></li>'
                  '</ul><h2>Related material</h2><p>' + 'More details. ' * 400 + '</p>')
        text = article_text(page.text)
        self.assertIn('Beta station Nearby station 9/14 units', text)
        excerpt, _ = excerpt_window(text, 'Related material')
        self.assertFalse(excerpt.startswith('9/14'))
        if '9/14' in excerpt:
            self.assertIn('Beta station Nearby station 9/14 units', excerpt)
        excerpt, _ = excerpt_window(text, offset=text.index('9/14'))
        self.assertTrue(excerpt.startswith('Beta station'))

    def test_nested_list_preserves_names_without_duplicating_hidden_content(self):
        page = Page()
        page.feed('<ul><li>Parent<ul><li>Child <span>7</span></li></ul></li></ul>'
                  '<nav><ul><li>Navigation</li></ul></nav><p>Conclusion</p>')
        text = article_text(page.text)
        self.assertIn('Parent Child 7', text)
        self.assertNotIn('Navigation', text)
        self.assertEqual(text.count('Child'), 1)
        self.assertIn('Conclusion', text)

    def test_search_returns_candidates_without_fetching_any_original(self):
        async def hits(*args):
            return [{'title': 'Manual', 'url': 'https://example.org/manual', 'content': 'Candidate only'}]
        async def check():
            with patch.dict('os.environ', {'SEARCH_PROVIDER_URL': 'http://index/search'}), \
                    patch('app.search_hits', hits), patch('app.original') as original:
                result = await web(Response(), operation='search', query='manual', x_expected_artifact='')
                self.assertFalse(result['originals_read'])
                self.assertEqual(result['candidates'][0]['snippet'], 'Candidate only')
                original.assert_not_called()
        asyncio.run(check())

    def test_read_does_not_search_or_discard_an_undated_manual(self):
        async def original(*args):
            page = Page()
            page.feed('<title>Manual</title><h1>Setup</h1><p>' + 'Configure the service. ' * 20 + '</p>')
            return 'https://example.org/manual', page
        async def check():
            with patch('app.original', original), patch('app.search_hits') as hits:
                result = await web(Response(), operation='read', url='https://example.org/manual', x_expected_artifact='')
                self.assertEqual(result['status'], 'source_read')
                self.assertIsNone(result['sources'][0]['published_on'])
                self.assertEqual(result['sources'][0]['title'], 'Manual')
                hits.assert_not_called()
        asyncio.run(check())

    def test_focus_and_continuation_read_original_segments(self):
        text = 'Intro\n' + 'General material.\n' * 250 + 'Configuration\nretry_count = 3\n' + 'Appendix\n' * 600
        excerpt, end = excerpt_window(text, 'retry_count')
        self.assertIn('retry_count = 3', excerpt)
        next_excerpt, next_end = excerpt_window(text, '', end)
        self.assertGreater(next_end, end)
        self.assertNotIn('retry_count = 3', next_excerpt)

    def test_table_rows_keep_column_and_section_boundaries(self):
        page = Page()
        page.feed('<title>Official results</title><h2>Cup A</h2><table>'
                  '<tr><th>Date</th><th>Opponent</th><th>FT</th><th>PTS</th></tr>'
                  '<tr><td>Tue 11/10</td><td>vs Red</td><td>2/2</td><td>11</td></tr>'
                  '</table><h2>Cup B</h2><p>Different competition</p>')
        body = article_text(page.text)
        self.assertIn('Date | Opponent | FT | PTS\nTue 11/10 | vs Red | 2/2 | 11\nCup B', body)
        self.assertEqual(page.title, ['Official results'])
        self.assertEqual(dated_content(body, '2026-11-04', '2026-11-11')[0], '2026-11-10')

    def test_excerpt_does_not_cut_a_row_midway(self):
        text = '2026-11-11\n' + ('Column A | Column B | Value\n' * 150)
        excerpt = source_excerpt(text, None, '2026-11-04', '2026-11-11')['excerpt']
        self.assertTrue(excerpt.endswith('Value'))

    def test_rewrites_only_http_index_links_to_https(self):
        self.assertEqual(search_url('http://example.org/story#top'), 'https://example.org/story')
        for address in ('ftp://example.org/story', 'https://user@example.org/story',
                        'https://example.org:8080/story'):
            with self.subTest(address=address), self.assertRaises(ValueError):
                search_url(address)

    def test_uses_a_publisher_timestamp_not_an_unlabelled_page_date(self):
        page = Page()
        page.feed('<time itemprop="datePublished" datetime="2026-11-09T23:15:00+08:00">发布日期</time>')
        self.assertEqual(publication_date(page.publication, '2026-11-10'), '2026-11-09')
        self.assertEqual(publication_date([], '编辑部2026年11月10日 18时发布'), '2026-11-10')
        self.assertEqual(publication_date([], '报道 2026-11-10 17:12:44 来源：中国天气网'), '2026-11-10')
        self.assertIsNone(publication_date([], '当前时间：2026-11-10'))

    def test_an_index_can_discover_an_article_without_dating_the_index(self):
        page = Page()
        page.feed('<a href="/news/one">更新说明<em>2026-11-10 07:12</em></a>'
                  '<a href="https://elsewhere.example.org/story">2026-11-10</a>'
                  '<a href="/news/old">2025-01-01</a>')
        self.assertIsNone(publication_date(page.publication, ' '.join(page.text)))
        self.assertEqual(list(linked_articles('https://example.org/', page, '2026-11-01', '2026-11-10')),
                         ['https://example.org/news/one'])

    def test_only_accepts_structured_publication_for_the_fetched_page(self):
        page = Page()
        page.feed('<script type="application/ld+json">{broken</script>'
                  '<script type="application/ld+json">'
                  '{"@graph":[{"url":"/story/related","datePublished":"2026-11-11"},'
                  '{"mainEntityOfPage":{"@id":"https://example.org/story/current/"},"datePublished":"2026-11-10"},'
                  '{"url":"/story/updated","dateModified":"2026-11-11"}]}</script>新闻内容')
        for path, expected in (('/story/current/', '2026-11-10'), ('/story/updated', None), ('/story/missing', None)):
            self.assertEqual(publication_date([], ' '.join(page.text), page.structured,
                                              'https://example.org' + path), expected)
        self.assertEqual(page.text, ['新闻内容'])

    def test_direct_result_keeps_rank_when_a_newer_discovered_page_finishes_first(self):
        now = datetime.now(timezone.utc).date().isoformat()
        index = 'https://example.org/index'
        direct = 'https://example.org/direct'
        discovered = 'https://example.org/article'

        async def hit(*args):
            return [{'title': 'Index', 'url': index}, {'title': 'Relevant original', 'url': direct}]

        async def open_page(client, url, counts):
            page = Page()
            if url == index:
                page.feed(f'<a href="{discovered}">{now}</a>')
            else:
                page.feed(f'<meta name="pubdate" content="{now}"><p>{"正文" * 70}</p>')
            return url, page

        async def check():
            with patch.dict('os.environ', {'SEARCH_PROVIDER_URL': 'http://index/search'}), \
                    patch('app.search_hits', hit), patch('app.original', open_page):
                reply = await research(Response(), '测试词', 1, '')
                self.assertEqual([row['url'] for row in reply['sources']], [direct, discovered])

        asyncio.run(check())

    def test_a_related_article_cannot_date_or_supply_links_for_the_current_page(self):
        body = article_text([
            '正文内容' * 35, '文中链接 https://example.org/mentioned',
            '上一篇文章：', '2026-11-10 17:12:44 来源：近期文章',
        ])
        self.assertIsNone(publication_date([], body))
        self.assertNotIn('近期文章', body)
        self.assertNotIn('https://example.org/mentioned', body)

    def test_dated_original_content_does_not_fabricate_a_publication_date(self):
        bounds = ('2026-11-04', '2026-11-11')
        text = 'Earlier heading and calendar details. ' * 4 + 'Wednesday · 11 Nov 2026 Rows from the schedule'
        result = source_excerpt(text, None, *bounds)
        self.assertEqual(result['content_on'], '2026-11-11')
        self.assertNotIn('published_on', result)
        self.assertIn('Rows from the schedule', result['excerpt'])
        self.assertIsNone(source_excerpt(text, '2024-11-11', *bounds))
        self.assertEqual(dated_content('4/18/2026 earlier entry, 11/9/2026 latest entry', *bounds)[0], '2026-11-09')
        self.assertIsNone(dated_content('11/9/2026 ambiguous', *bounds))
        result = source_excerpt(article_text([
            'Original page with a visible data table and its summary about the requested entity.',
            'It was recorded today. NAME DATE MIN TOTAL 1 Entry 11/10/2026 24 12',
            'Related Searches More news dated November 11, 2026',
        ]), None, *bounds)
        self.assertEqual(result['content_on'], '2026-11-10')
        self.assertNotIn('More news', result['excerpt'])
        self.assertEqual(dated_content('11月11日(星期三) 11时更新', *bounds)[0], '2026-11-11')
        self.assertIsNone(dated_content('11月11日(星期二) 11时更新', *bounds))

    def test_time_element_keeps_the_original_offset_alongside_visible_text(self):
        page = Page()
        page.feed('<time datetime="2026-11-11T13:30:00Z">1:30 pm</time>'
                  '<time datetime="2026-11-11T14:00:00">2:00 pm</time>')
        self.assertEqual(page.text, ['[2026-11-11T13:30:00+00:00]', '1:30 pm', '2:00 pm'])

    def test_weekday_and_cutoff_disambiguate_yearless_result_rows(self):
        text = ('Player and team. ' * 8 + 'Date OPP Result MIN PTS Tue 11/10 vs Opponent L 29 11 '
                'Sun 11/8 @ Previous W 33 22')
        result = source_excerpt(text, None, '2026-11-04', '2026-11-11')
        self.assertEqual(result['content_on'], '2026-11-10')
        self.assertIn('Tue 11/10 vs Opponent', result['excerpt'])
        self.assertIsNone(dated_content('Wed 11/10 vs Opponent', '2026-11-04', '2026-11-11'))
        self.assertEqual(dated_content('Thu 12/31 v. Opponent', '2026-12-29', '2027-01-02')[0],
                         '2026-12-31')
        self.assertIsNone(dated_content('Tue 11/10 Navigation Date', '2026-11-04', '2026-11-11'))

    def test_https_redirects_are_bounded_and_downgrades_are_rejected(self):
        async def check():
            urls = []

            def reply(request):
                urls.append(str(request.url))
                if request.url.path == '/start':
                    return httpx.Response(301, headers={'location': '/story'})
                if request.url.path == '/story':
                    return httpx.Response(200, text='Original')
                return httpx.Response(302, headers={'location': 'http://example.org/story'})

            async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
                self.assertEqual(await read_page(client, 'https://example.org/start'),
                                 ('https://example.org/story', 'Original'))
                with self.assertRaisesRegex(ValueError, 'unsafe_redirect'):
                    await read_page(client, 'https://example.org/down')
            self.assertEqual(urls, ['https://example.org/start', 'https://example.org/story',
                                    'https://example.org/down'])

        asyncio.run(check())
