import unittest
from unittest.mock import AsyncMock

from app import SearchGateway, UpstreamUnavailable, ranked_results


def result(title, url, content='A public result.', **flags):
    return dict(title=title, url=url, content=content, **flags)


class SearchTests(unittest.IsolatedAsyncioTestCase):
    def test_filters_and_ranks_without_modifying_the_query(self):
        rows = [
            result('Others', 'https://other.example/path'),
            result('<b>上海</b>天气', 'https://example.com/a?utm_campaign=x&id=8', '上海预报'),
            result('重复', 'http://example.com/a?id=8#section'),
            result('广告', 'https://ad.example/x', sponsored=True),
            result('私人', 'http://127.0.0.1/local'),
            result('无摘要', 'https://example.com/empty', ''),
        ]
        self.assertEqual(ranked_results('上海天气', rows), [
            {'title': '上海 天气', 'url': 'https://example.com/a?id=8', 'snippet': '上海预报'},
        ])

    def test_time_words_do_not_make_unrelated_results_relevant(self):
        self.assertEqual(ranked_results('武汉今天的天气', [
            result('今天最新视频', 'https://example.com/irrelevant', '今天更新，请看'),
            result('武汉天气预报', 'https://example.com/weather', '武汉市今日气象预报'),
        ]), [{'title': '武汉天气预报', 'url': 'https://example.com/weather', 'snippet': '武汉市今日气象预报'}])
        self.assertEqual(ranked_results('latest PostgreSQL jsonb documentation', [
            result('Latest news today', 'https://example.com/no'),
        ]), [])

    async def test_unrelated_upstream_success_is_degraded_and_not_cached(self):
        api = SearchGateway('unused')
        api._get = AsyncMock(return_value={'results': [result('今天的视频', 'https://example.com/no')]})
        for _ in range(2):
            value, cached = await api.search('武汉今天的天气', 'day')
            self.assertEqual(value, {'status': 'degraded', 'results': []})
            self.assertFalse(cached)
        self.assertEqual(api._get.await_count, 2)
        api._get.assert_awaited_with('武汉今天的天气', 'day')

    async def test_cache_has_a_ttl_and_keeps_time_filters_separate(self):
        now = [1]
        api = SearchGateway('unused', ttl=10, capacity=1, clock=lambda: now[0])
        api._get = AsyncMock(return_value={'results': [result('Demo query', 'https://example.com/a')]})
        self.assertFalse((await api.search('demo query'))[1])
        self.assertTrue((await api.search('demo query', limit=1))[1])
        now[0] = 12
        self.assertFalse((await api.search('demo query'))[1])
        self.assertFalse((await api.search('demo query', 'day'))[1])
        self.assertFalse((await api.search('demo query'))[1])
        self.assertEqual(api._get.await_count, 4)
        api._get.assert_awaited_with('demo query', None)

    async def test_empty_and_failed_searches_never_enter_the_cache(self):
        api = SearchGateway('unused')
        api._get = AsyncMock(side_effect=[
            {'results': [], 'unresponsive_engines': [['engine', 'timeout']]},
            UpstreamUnavailable(),
            {'results': []},
        ])
        self.assertEqual((await api.search('demo query'))[0]['status'], 'degraded')
        with self.assertRaises(UpstreamUnavailable):
            await api.search('demo query')
        self.assertEqual((await api.search('demo query'))[0]['status'], 'no_results')
        self.assertEqual(api._get.await_count, 3)

    async def test_clients_cannot_override_engines_or_supply_query_commands(self):
        api = SearchGateway('unused')
        api._get = AsyncMock()
        with self.assertRaises(ValueError):
            await api.search('!bing example')
        with self.assertRaises(ValueError):
            await api.search('example query', 'invalid period')
        api._get.assert_not_awaited()
