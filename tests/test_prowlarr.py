"""Tests for the ported script.elementum.prowlarr (ex script.elementum.jackett) code."""
import asyncio
import sys
import types
import runpy
import io
from unittest.mock import AsyncMock, patch
import unittest
from pathlib import Path
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / 'src'

SETTINGS = {
    'host': 'http://prowlarr.example',
    'api_key': 'a' * 32,
    'elementum.prowlarr.use_smart_show_filter': True,
    'elementum.prowlarr.search_season_on_episode': False,
    'elementum.prowlarr.filter_exclude_no_seed': False,
    'elementum.prowlarr.filter_include_resolution_enabled': False,
    'elementum.prowlarr.filter_include_release': False,
    'elementum.prowlarr.filter_size_enabled': False,
    'elementum.prowlarr.filter_keywords_enabled': False,
    'elementum.prowlarr.max_results': 20,
    'elementum.prowlarr.sort_by': 3,
    'elementum.prowlarr.kodi_language': False,
    'elementum.prowlarr.search_by_imdb_key': False,
}


class _Addon:
    def getAddonInfo(self, key):
        return {'id': 'script.elementum.prowlarr', 'name': 'Elementum Prowlarr',
                'path': str(ROOT), 'icon': '', 'profile': str(ROOT),
                'version': '0.4.5'}[key]

    def getSetting(self, key):
        return SETTINGS.get(key, '')

    def getLocalizedString(self, value):
        return f'string {value}'

    def setSetting(self, key, value):
        SETTINGS[key] = str(value)

    def openSettings(self):
        pass


class _DialogProgressBG:
    def create(self, *args, **kwargs):
        pass

    def update(self, *args, **kwargs):
        pass

    def close(self):
        pass


class _Dialog:
    def notification(self, *args, **kwargs):
        pass

    def ok(self, *args, **kwargs):
        pass


def _install_stubs():
    xbmc = types.ModuleType('xbmc')
    xbmc.LOGFATAL = 6
    xbmc.LOGERROR = 4
    xbmc.LOGWARNING = 3
    xbmc.LOGINFO = 1
    xbmc.LOGDEBUG = 0
    xbmc.LOGNONE = -1
    xbmc.ISO_639_1 = 'iso'
    xbmc.getLanguage = lambda fmt=None: 'en'
    xbmc.log = lambda message, level=0: None

    xbmcgui = types.ModuleType('xbmcgui')
    xbmcgui.DialogProgressBG = _DialogProgressBG
    xbmcgui.Dialog = _Dialog

    xbmcaddon = types.ModuleType('xbmcaddon')
    xbmcaddon.Addon = lambda *args, **kwargs: _Addon()

    xbmcvfs = types.ModuleType('xbmcvfs')
    xbmcvfs.translatePath = lambda value: value

    kodi_six = types.ModuleType('kodi_six')
    kodi_six.xbmc = xbmc
    kodi_six.xbmcgui = xbmcgui
    kodi_six.xbmcaddon = xbmcaddon
    kodi_six.xbmcvfs = xbmcvfs

    elementum = types.ModuleType('elementum')
    provider = types.ModuleType('elementum.provider')

    def get_setting(key, converter=str, choices=None):
        value = SETTINGS.get(key)
        if value is None:
            return converter()
        if converter is bool:
            return value if isinstance(value, bool) else str(value).lower() == 'true'
        if converter is float:
            raise TypeError("Elementum does not support float converters")
        return converter(value)

    provider.get_setting = get_setting
    provider.register = lambda *callbacks: None
    elementum.provider = provider

    stubs = {'xbmc': xbmc, 'xbmcgui': xbmcgui, 'xbmcaddon': xbmcaddon,
             'xbmcvfs': xbmcvfs, 'kodi_six': kodi_six, 'elementum': elementum,
             'elementum.provider': provider}
    sys.modules.update(stubs)


_install_stubs()
sys.path.insert(0, str(ROOT / "resources" / "libs"))
sys.path.insert(0, str(SRC))

import client  # noqa: E402
import filter  # noqa: E402
import utils  # noqa: E402
import prowlarr  # noqa: E402
from flatbencode import encode  # noqa: E402
from aiohttp import web  # noqa: E402


def torrent(name, **extra):
    return {'name': name, 'provider': 'Tracker', 'seeds': 5, 'peers': 2,
            '_size_bytes': 1024 ** 3, 'info_hash': '', '_resolution': '1080p',
            'resolution': 3, 'release_type': 'webdl',
            'uri': 'magnet:?xt=urn:btih:' + 'a' * 40, **extra}


class FilterTests(unittest.TestCase):
    def test_smart_filter_accepts_rutracker_range_release_for_season(self):
        results = [torrent('Макдональд и Доддс / McDonald & Dodds / S1E1-2 of 2 [2020, Великобритания]'),
                   torrent('War.2026.S01.Complete.1080p.WEBRip.x265'),
                   torrent('The Jetty 2024 S01 1080p WEB-DL'),
                   torrent('Show S01 no year release')]
        kept = filter.tv_season_episode(results, 1, '', None, None, 2020, 0, 0)
        self.assertEqual(
            ['Макдональд и Доддс / McDonald & Dodds / S1E1-2 of 2 [2020, Великобритания]'],
            [item['name'] for item in kept])

    def test_smart_filter_accepts_matching_episode(self):
        results = [torrent('Макдональд и Доддс / McDonald & Dodds / S1E1-2 of 2 [2020]'),
                   torrent('McDonald.and.Dodds.S01E03.720p.WEBRip')]
        kept = filter.tv_season_episode(results, 1, '', 2, None, 2020, 0, 0)
        self.assertEqual(1, len(kept))
        self.assertIn('S1E1-2', kept[0]['name'])

    def test_explicit_episode_survives_without_release_year(self):
        items = [
            torrent('Death_Comes_To_Pemberley.1x01.HDTV_x264-FoV'),
            torrent('Death.Comes.to.Pemberley.S01E01.1080p.HDTV'),
            torrent('Death Comes to Pemberley S1E1-2 HDTV'),
            torrent('Death Comes to Pemberley S01E02 HDTV'),
            torrent('Death Comes to Pemberley S02E01 HDTV'),
            torrent('Death Comes to Pemberley S01 HDTV'),
            torrent('Death Comes to Pemberley S01E01 2025 HDTV'),
        ]
        self.assertEqual(items[:3], filter.tv_season_episode(items, 1, '', 1, 1, 2013, 2013, 2013))

    def test_size_filter_applies_gib_limits_and_includes_unknown(self):
        items = [torrent('Kept'), torrent('Unknown'), torrent('Too big')]
        items[1]['_size_bytes'] = -1
        items[2]['_size_bytes'] *= 2
        with patch.dict(SETTINGS, {
            'elementum.prowlarr.size_movie_min': '0.5',
            'elementum.prowlarr.size_movie_max': '1.5',
            'elementum.prowlarr.size_include_unknown': True,
        }):
            self.assertEqual(items[:2], filter.size('movie', items))

    def test_keywords_filter_requires_and_blocks_configured_words(self):
        items = [torrent('Show WEB'), torrent('Show WEB CAM'), torrent('Show DVD')]
        with patch.dict(SETTINGS, {
            'elementum.prowlarr.keywords_block': 'CAM',
            'elementum.prowlarr.keywords_require': 'WEB',
        }):
            self.assertEqual(items[:1], filter.keywords(items))

    def test_seed_filter(self):
        kept = filter.seed([torrent('A', seeds=0), torrent('B', seeds=7)])
        self.assertEqual(['B'], [item['name'] for item in kept])


class ClientTests(unittest.TestCase):
    def indexer(self, caps):
        resource = {'id': 21, 'name': 'First',
                    'capabilities': {'tvSearchParams': caps, 'movieSearchParams': ['q']}}
        return client.Indexer(resource)

    def test_tvsearch_uses_season_and_episode_params_when_supported(self):
        indexer = self.indexer(['q', 'season', 'ep'])
        captured = {}

        async def fake_send(url, params=None, json_response=False):
            captured['params'] = params
            return ElementTree.fromstring('<rss><channel></channel></rss>')

        cli = client.ProwlarrClient('http://prowlarr.example/', 'synthetic')
        cli.send_request = fake_send
        asyncio.run(cli.search_by_indexer(indexer, client.SearchType.TV, 'Show', None, 1, 2))
        self.assertEqual({'t': 'tvsearch', 'q': 'Show', 'season': 1, 'ep': 2}, captured['params'])

    def test_tvsearch_appends_marker_to_query_without_season_cap(self):
        indexer = self.indexer(['q'])
        captured = {}

        async def fake_send(url, params=None, json_response=False):
            captured['params'] = params
            return ElementTree.fromstring('<rss><channel></channel></rss>')

        cli = client.ProwlarrClient('http://prowlarr.example/', 'synthetic')
        cli.send_request = fake_send
        asyncio.run(cli.search_by_indexer(indexer, client.SearchType.TV, 'Show', None, 1, 2))
        self.assertEqual({'t': 'tvsearch', 'q': 'Show S01E02'}, captured['params'])

    def test_two_titles_preserve_movie_and_regular_tv_parameters(self):
        cli = client.ProwlarrClient('http://prowlarr.example/', 'synthetic')
        cli.indexers = [self.indexer(['q', 'season', 'ep'])]
        seen = []

        async def fake_send(url, params=None, json_response=False):
            seen.append(params)
            return ElementTree.fromstring('<rss><channel/></rss>')

        cli.send_request = fake_send
        titles = ('English', 'Русское')
        asyncio.run(cli.search_movie(titles, 2013))
        self.assertCountEqual([{'t': 'movie', 'q': f'{title} 2013'} for title in titles], seen)
        seen.clear()
        asyncio.run(cli.search_tv(titles, season=1, ep=2))
        self.assertCountEqual([{'t': 'tvsearch', 'q': title, 'season': 1, 'ep': 2}
                               for title in titles], seen)

    def test_smart_search_skips_years_when_primary_titles_find_enough(self):
        cli = client.ProwlarrClient('http://prowlarr.example/', 'synthetic')
        cli.indexers = [self.indexer(['q'])]
        seen = []

        async def fake_send(url, params=None, json_response=False):
            seen.append(params['q'])
            items = ''.join(f'<item><title>{params["q"]} {i}</title>'
                            f'<link>magnet:?xt=urn:btih:{i:040x}</link></item>' for i in range(3))
            return ElementTree.fromstring(f'<rss><channel>{items}</channel></rss>')

        cli.send_request = fake_send
        rows = asyncio.run(cli.search_tv_smart(('English', 'Русское'), 2013, 2013, 2013))
        self.assertEqual(['English', 'Русское'], seen)
        self.assertEqual(6, len(rows))

    def test_year_goes_into_params_only_with_year_cap(self):
        captured = {}

        async def fake_send(url, params=None, json_response=False):
            captured['params'] = params
            return ElementTree.fromstring('<rss><channel></channel></rss>')

        with_year = client.Indexer(
            {'id': 21, 'name': 'Y', 'capabilities': {'movieSearchParams': ['q', 'year']}})
        cli = client.ProwlarrClient('http://prowlarr.example/', 'synthetic')
        cli.send_request = fake_send
        asyncio.run(cli.search_by_indexer(with_year, client.SearchType.MOVIE, 'Film', 2017))
        self.assertEqual({'t': 'movie', 'q': 'Film', 'year': 2017}, captured['params'])

        without_year = client.Indexer(
            {'id': 22, 'name': 'N', 'capabilities': {'movieSearchParams': ['q']}})
        asyncio.run(cli.search_by_indexer(without_year, client.SearchType.MOVIE, 'Film', 2017))
        self.assertEqual({'t': 'movie', 'q': 'Film 2017'}, captured['params'])


class ProviderTests(unittest.TestCase):
    def test_kodi_entrypoint_registers_four_callbacks(self):
        import elementum.provider
        with patch.object(elementum.provider, 'register') as register, patch.dict(
            SETTINGS, {'elementum.prowlarr.enable_debugger': False}
        ), patch.object(sys, 'argv', ['main.py', 'synthetic-payload']), patch.dict(sys.modules):
            runpy.run_path(str(SRC / 'main.py'), run_name='__main__')
        self.assertEqual(4, len(register.call_args.args))

    def test_parse_movie_and_episode_use_available_russian_and_english(self):
        with patch.dict(SETTINGS, {'elementum.prowlarr.kodi_language': True}), patch.object(
            prowlarr.xbmc, 'getLanguage', return_value='ru'
        ):
            for method in ('movie', 'episode', 'season'):
                parsed = prowlarr.parse_payload(method, {
                    'title': 'English', 'titles': {'EN': 'English', 'RU': 'Русское'},
                })
                self.assertEqual(('Русское', 'English'), parsed['search_titles'])
            parsed = prowlarr.parse_payload('movie', {'title': 'English', 'titles': {'en': 'english '}})
            self.assertEqual(('English',), parsed['search_titles'])

    def test_russian_original_is_retained_with_english_translation(self):
        parsed = prowlarr.parse_payload('movie', {'title': 'Русское', 'titles': {'en': 'English'}})
        self.assertEqual(('English', 'Русское'), parsed['search_titles'])

    def test_empty_key_returns_empty_results_and_can_open_settings(self):
        with patch.dict(SETTINGS, api_key=''):
            self.assertEqual([], prowlarr.search('Show'))
            asyncio.run(prowlarr.validate_client())


class EpisodeFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def search(self, items, *, max_results=20, method='episode'):
        cli = types.SimpleNamespace(
            search_tv_smart=AsyncMock(return_value=items), search_movie=AsyncMock(return_value=items),
            close_session=AsyncMock(),
        )
        payload = {
            'search_title': 'Death Comes to Pemberley',
            'search_titles': ('Death Comes to Pemberley', 'Смерть приходит в Пемберли'),
            'season': 1, 'episode': 1, 'year': 2013, 'season_year': 2013, 'show_year': 2013,
        }
        with patch.object(prowlarr, 'get_client', return_value=cli), patch.dict(SETTINGS, {
            'elementum.prowlarr.max_results': max_results,
            'elementum.prowlarr.filter_exclude_no_seed': True,
        }):
            return await prowlarr.search_prowlarr(prowlarr.PDialog('Search'), payload, method)

    @staticmethod
    def item(title, key, seeds=5):
        return torrent(title, seeds=seeds, uri='magnet:?xt=urn:btih:' + key * 40)

    async def test_keeps_exact_first_then_fills_from_same_show(self):
        exact = self.item('Death Comes to Pemberley S01E01', 'a')
        items = [
            exact,
            self.item('Death Comes to Pemberley duplicate', 'a', 100),
            self.item('Death_Comes_To_Pemberley season pack', 'b', 20),
            self.item('Смерть приходит в Пемберли HDTV', 'c', 15),
            self.item('Death Comes to Pemberley another pack', 'd', 10),
            self.item('Vikings.S01E01.1080p', 'e', 200),
            self.item('Death Comes to Pemberley 2013 S01E02', '1', 200),
            self.item('Death Comes to Pemberley with no seeds', 'f', 0),
        ]
        results = await self.search(items)
        self.assertEqual([items[i]['name'] for i in (0, 2, 3)], [r['name'] for r in results])
        self.assertEqual('Tracker', results[0]['provider'])
        self.assertTrue(all(r['provider'] == 'Tracker (string 32755)' for r in results[1:]))

    async def test_unrelated_release_never_fills_an_empty_result(self):
        for method in ('episode', 'season', 'movie'):
            vikings = self.item('Vikings 2013 S01E01', 'f', 100)
            self.assertEqual([], await self.search([vikings], method=method))

    def test_title_gate_accepts_conjunction_variants_and_word_boundaries(self):
        items = [
            self.item('McDonald.and.Dodds.S01E01', 'a'),
            self.item('Макдональд_и_Доддс.S01E01', 'b'),
            self.item('McDonaldson and Dodds S01E01', 'c'),
        ]
        self.assertEqual(items[:2], prowlarr.matching_titles(items, ('McDonald & Dodds', 'Макдональд и Доддс')))

    async def test_fallback_after_exact_link_fails_respects_result_limit(self):
        broken = torrent('Death Comes to Pemberley S01E01', uri='magnet:?xt=invalid')
        backup = self.item('Death Comes to Pemberley season pack', 'b', 8)
        extra = self.item('Смерть приходит в Пемберли HDTV', 'c')
        results = await self.search([broken, backup, extra], max_results=1)
        self.assertEqual([backup['name']], [row['name'] for row in results])
        self.assertEqual('Tracker (string 32755)', results[0]['provider'])

    async def test_enough_exact_matches_needs_no_fallback(self):
        exact = [self.item(f'Death Comes to Pemberley S01E01 choice {k}', k) for k in 'abc']
        backup = self.item('Death Comes to Pemberley season pack', 'd', 100)
        results = await self.search([*exact, backup])
        self.assertCountEqual([r['name'] for r in exact], [r['name'] for r in results])


class WireTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_returns_unique_magnets_through_prowlarr_proxy(self):
        key = 'a' * 32
        magnet = 'magnet:?xt=urn:btih:' + 'b' * 40
        torrent_file = encode({b'info': {
            b'name': b'Synthetic', b'length': 1, b'piece length': 16384, b'pieces': b'a' * 20,
        }})
        file_magnet = str(prowlarr.torrent.Torrent.read_stream(io.BytesIO(torrent_file)).magnet())
        requests = []

        async def respond(request):
            requests.append((request.path, request.query.get('apikey'), request.headers.get('X-Api-Key')))
            if request.path.endswith('/indexer'):
                return web.json_response([
                    {'id': 1, 'name': 'Ready', 'enable': True, 'protocol': 'torrent'},
                    {'id': 2, 'name': 'Paused', 'enable': True, 'protocol': 'torrent'},
                ])
            if request.path.endswith('/indexerstatus'):
                return web.json_response([{'indexerId': 2, 'disabledTill': '2099-01-01T00:00:00Z'}])
            if request.path.endswith('/newznab'):
                return web.Response(text=(
                    '<rss><channel><item><title>Show.1080p</title><link>/base/1/download</link></item>'
                    '<item><title>Show.720p</title><link>' + magnet + '</link></item>'
                    '<item><title>Synthetic.480p</title><link>/base/synthetic.torrent</link></item>'
                    '</channel></rss>'
                ))
            if request.path.endswith('.torrent'):
                return web.Response(body=torrent_file, content_type='application/x-bittorrent')
            return web.Response(status=302, headers={'Location': magnet})

        app = web.Application()
        app.router.add_get('/{path:.*}', respond)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        self.addAsyncCleanup(runner.cleanup)
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        address, port = runner.addresses[0]
        with patch.dict(SETTINGS, host=f'http://{address}:{port}/base'):
            results = await prowlarr.search_prowlarr(prowlarr.PDialog('Search'),
                                                     {'search_title': 'Show'}, 'general')
        self.assertEqual({magnet, file_magnet}, {item['uri'] for item in results})
        self.assertEqual(2, len(results))
        self.assertEqual([
            ('/base/api/v1/indexer', None, key),
            ('/base/api/v1/indexerstatus', None, key),
            ('/base/api/v1/indexer/1/newznab', None, key),
        ], requests[:3])
        self.assertCountEqual([
            ('/base/1/download', key, None),
            ('/base/synthetic.torrent', None, None),
        ], requests[3:])

    async def test_search_deadline_retains_successful_indexer(self):
        async def respond(request):
            if request.path.endswith('/2/newznab'):
                await asyncio.sleep(.35)
            return web.Response(text=(
                '<rss><channel><item><title>Release</title>'
                '<link>magnet:?xt=urn:btih:' + 'c' * 40 + '</link></item></channel></rss>'
            ))

        app = web.Application()
        app.router.add_get('/{path:.*}', respond)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        self.addAsyncCleanup(runner.cleanup)
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        host, port = runner.addresses[0]
        cli = client.ProwlarrClient(f'http://{host}:{port}', 'synthetic')
        cli.indexers = [client.Indexer({'id': i, 'name': name}) for i, name in [(1, 'Fast'), (2, 'Slow')]]
        cli.deadline = prowlarr.time.monotonic() + .15
        await cli.create_session()
        try:
            results = await cli.search_query('Release')
        finally:
            await cli.close_session()
        self.assertEqual(1, len(results))
        self.assertIn('Fast', results[0]['provider'])

    async def test_download_budget_keeps_other_tracker_and_skips_paused_provider(self):
        calls = []
        magnet = 'magnet:?xt=urn:btih:' + 'c' * 40

        async def respond(request):
            calls.append(request.path)
            if request.path == '/limited':
                return web.Response(status=429, headers={'Retry-After': '30'})
            if request.path == '/slow':
                await asyncio.sleep(.2)
            return web.Response(status=302, headers={'Location': magnet})

        app = web.Application()
        app.router.add_get('/{path:.*}', respond)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        self.addAsyncCleanup(runner.cleanup)
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        host, port = runner.addresses[0]
        rows = [
            torrent('Same title', provider='Limited', uri=f'http://{host}:{port}/limited'),
            torrent('Skipped', provider='Limited', uri=f'http://{host}:{port}/skipped'),
            torrent('Same title', provider='Good', uri=f'http://{host}:{port}/good'),
            torrent('Slow', provider='Slow', uri=f'http://{host}:{port}/slow'),
        ]
        results = await prowlarr.torrent.uri_to_magnets_uniq_torrents(
            rows, deadline=prowlarr.time.monotonic() + .1,
        )
        self.assertCountEqual(['/limited', '/good', '/slow'], calls)
        self.assertEqual(['Good'], [row['provider'] for row in results])

    async def test_magnet_limit_stops_before_network_downloads(self):
        exact = torrent('First')
        extra = torrent('Second', uri='http://127.0.0.1:1/not-needed')
        with patch.object(prowlarr.torrent, 'get_magnet', wraps=prowlarr.torrent.get_magnet) as resolve:
            results = await prowlarr.torrent.uri_to_magnets_uniq_torrents([exact, extra], limit=1)
        self.assertEqual([exact], results)
        self.assertEqual(1, resolve.await_count)

    async def test_episode_search_sends_both_titles_and_keeps_exact_yearless_match(self):
        calls = []
        active = set()
        overlap = []

        async def respond(request):
            if request.path == '/api/v1/indexer':
                return web.json_response([
                    {'id': 1, 'name': 'RuTracker.org', 'enable': True, 'protocol': 'torrent',
                     'implementation': 'RuTracker'},
                    {'id': 2, 'name': 'Tracker 2', 'enable': True, 'protocol': 'torrent'},
                ])
            if request.path == '/api/v1/indexerstatus':
                return web.json_response([])
            title = request.query['q']
            indexer = int(request.path.split('/')[4])
            calls.append((indexer, title))
            self.assertNotIn(indexer, active)
            active.add(indexer)
            overlap.append(len(active))
            await asyncio.sleep(.02)
            active.remove(indexer)
            if indexer == 1:
                return web.Response(status=429, headers={'Retry-After': '30'})
            if title.startswith('English'):
                return web.Response(text='<rss><channel/></rss>')
            return web.Response(text=(
                '<rss><channel><item><title>Русское.S01E01.1080p</title>'
                '<link>magnet:?xt=urn:btih:' + 'c' * 40 + '</link></item></channel></rss>'
            ))

        app = web.Application()
        app.router.add_get('/{path:.*}', respond)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        self.addAsyncCleanup(runner.cleanup)
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        address, port = runner.addresses[0]
        payload = prowlarr.parse_payload('episode', {
            'title': 'English', 'titles': {'ru': 'Русское', 'en': 'English'},
            'season': 1, 'episode': 1, 'year': 2013, 'season_year': 2013, 'show_year': 2013,
        })
        with patch.dict(SETTINGS, host=f'http://{address}:{port}'):
            results = await prowlarr.search_prowlarr(prowlarr.PDialog('Search'), payload, 'episode')
        self.assertEqual(['English'], [q for i, q in calls if i == 1])
        self.assertEqual(['English', 'Русское', 'English 2013', 'Русское 2013'], [q for i, q in calls if i == 2])
        self.assertIn(2, overlap)
        self.assertEqual(['Русское.S01E01.1080p'], [item['name'] for item in results])

    def test_download_key_stays_on_prowlarr_origin(self):
        cli = client.ProwlarrClient('https://prowlarr.example/base/', 'synthetic')
        other = 'https://tracker.example/base/1/download'
        self.assertEqual(other, cli.download_uri(other))
        self.assertEqual('https://prowlarr.example/base/1/download?apikey=synthetic',
                         cli.download_uri('1/download'))

    async def test_rutracker_recovery_uses_test_only_for_prowlarr_cooldown(self):
        definition = {'id': 31, 'name': 'RuTracker.org', 'enable': True, 'implementation': 'RuTracker',
                      'capabilities': {'tvSearchParams': ['q', 'season', 'ep']},
                      'fields': [{'name': 'password', 'value': '********'}]}
        cooldown = 'Indexer is disabled till tomorrow due to recent failures.'
        quota = 'User configurable Indexer Query Limit of 1 in last 24 hour(s) reached.'
        calls = []

        async def respond(request):
            self.assertEqual('synthetic', request.headers.get('X-Api-Key'))
            if request.path.endswith('/newznab'):
                params = dict(request.query)
                calls.append(params)
                if retry_succeeds and params['q'] == 'English' and calls.count(params) == 2:
                    return web.Response(text=(
                        '<rss><channel><item><title>Recovered</title>'
                        '<link>magnet:?xt=urn:btih:' + 'c' * 40 + '</link></item></channel></rss>'
                    ))
                return web.Response(status=429, headers={'Retry-After': '300'},
                                    text=f'<error code="429" description="{description}"/>')
            if request.method == 'GET' and request.path.endswith('/31'):
                calls.append('definition')
                return web.json_response(definition)
            self.assertEqual('/base/api/v1/indexer/test', request.path)
            self.assertEqual('POST', request.method)
            self.assertEqual(definition, await request.json())
            calls.append('test')
            return web.Response(status=test_status)

        app = web.Application()
        app.router.add_route('*', '/base/api/v1/{path:.*}', respond)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        self.addAsyncCleanup(runner.cleanup)
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        host, port = runner.addresses[0]
        cli = client.ProwlarrClient(f'http://{host}:{port}/base', 'synthetic')
        await cli.create_session()
        self.addAsyncCleanup(cli.close_session)
        cli.indexers = [client.Indexer(definition)]
        english = {'t': 'tvsearch', 'q': 'English', 'season': '1', 'ep': '2'}
        russian = dict(english, q='Русское')
        cases = [
            (cooldown, 200, True, [english, 'definition', 'test', english, russian], ['Recovered']),
            (cooldown, 200, False, [english, 'definition', 'test', english], []),
            (cooldown, 400, False, [english, 'definition', 'test'], []),
            (quota, 200, False, [english], []),
        ]
        with patch.object(client, 'RUTRACKER_RETRY_DELAY', .01):
            for description, test_status, retry_succeeds, expected_calls, expected_names in cases:
                with self.subTest(description=description, test_status=test_status, retry_succeeds=retry_succeeds):
                    calls.clear()
                    results = await cli.search_tv(('English', 'Русское'), season=1, ep=2)
                    self.assertEqual(expected_calls, calls)
                    self.assertEqual(expected_names, [row['name'] for row in results])


class ParseTests(unittest.TestCase):
    def test_feed_keeps_alternative_links_with_the_same_title(self):
        links = ['magnet:?xt=urn:btih:' + key * 40 for key in 'ab']
        content = ''.join(f'<item><title>Same name</title><link>{uri}</link></item>' for uri in links)
        rows = client.parse_torrents(ElementTree.fromstring(f'<rss><channel>{content}</channel></rss>'))
        self.assertEqual(links, [row['uri'] for row in rows])

    def test_parse_item_maps_torznab_attributes(self):
        magnet = 'magnet:?xt=urn:btih:' + 'b' * 40
        xml = f'''<rss xmlns:torznab="http://torznab.com/schemas/2015/feed"><channel><item>
            <title>Show S01E02 1080p</title>
            <size>1073741824</size>
            <jackettindexer>Tracker</jackettindexer>
            <torznab:attr name="magneturl" value="{magnet}"/>
            <torznab:attr name="seeders" value="9"/>
            <torznab:attr name="peers" value="11"/>
            <torznab:attr name="infohash" value="{'c' * 40}"/>
        </item></channel></rss>'''
        item = client.parse_item(ElementTree.fromstring(xml).find('channel/item'))
        self.assertEqual('Show S01E02 1080p', item['name'])
        self.assertEqual(magnet, item['uri'])
        self.assertEqual(9, item['seeds'])
        self.assertEqual(11, item['peers'])
        self.assertEqual(4, item['resolution'])
        self.assertIn('Tracker', item['provider'])


class UtilsTests(unittest.TestCase):
    def test_size_sort_uses_bytes(self):
        items = [dict(torrent('Small'), size='900 MB', _size_bytes=900_000_000),
                 dict(torrent('Large'), size='10 GB', _size_bytes=10_000_000_000)]
        with patch.dict(SETTINGS, {'elementum.prowlarr.sort_by': 2}):
            self.assertEqual(items[::-1], prowlarr.sort_results(items))

    def test_resolution_detection(self):
        self.assertEqual('1080p', utils.get_resolution('Show.S01E02.1080p.WEB-DL'))
        self.assertEqual('4k', utils.get_resolution('Movie 2160p BluRay'))
        self.assertEqual(utils.UNKNOWN, utils.get_resolution('Something quiet'))

    def test_setting_prefix_is_prowlarr(self):
        self.assertEqual('elementum.prowlarr.', utils._plugin_setting_prefix)

    def test_season_name_guard(self):
        self.assertEqual('', utils.check_season_name('Show', 'Season 1'))
        self.assertEqual('', utils.check_season_name('Any title', 'season special'))
        self.assertEqual('pilot', utils.check_season_name('Show', 'Pilot'))


if __name__ == '__main__':
    unittest.main()
