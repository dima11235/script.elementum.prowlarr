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
                'version': '0.4.2'}[key]

    def getSetting(self, key):
        return SETTINGS[key]

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
    async def search(self, items, *, max_results=20):
        cli = types.SimpleNamespace(
            search_tv_smart=AsyncMock(return_value=items), close_session=AsyncMock(),
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
            return await prowlarr.search_prowlarr(prowlarr.PDialog('Search'), payload, 'episode')

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
            self.item('Unrelated Show WEB-DL', 'e', 200),
            self.item('Death Comes to Pemberley with no seeds', 'f', 0),
        ]
        results = await self.search(items)
        self.assertEqual([items[i]['name'] for i in (0, 2, 3)], [r['name'] for r in results])
        self.assertEqual('Tracker', results[0]['provider'])
        self.assertTrue(all(r['provider'] == 'Tracker (string 32755)' for r in results[1:]))

    async def test_fallback_after_exact_link_fails_respects_result_limit(self):
        broken = torrent('Death Comes to Pemberley S01E01', uri='magnet:?xt=invalid')
        backup = self.item('Death Comes to Pemberley season pack', 'b', 8)
        extra = self.item('Смерть приходит в Пемберли HDTV', 'c')
        self.assertEqual([backup], await self.search([broken, backup, extra], max_results=1))

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

    async def test_episode_search_sends_both_titles_and_keeps_exact_yearless_match(self):
        calls = []

        async def respond(request):
            if request.path == '/api/v1/indexer':
                return web.json_response([{'id': 1, 'name': 'Tracker', 'enable': True, 'protocol': 'torrent'}])
            if request.path == '/api/v1/indexerstatus':
                return web.json_response([])
            title = request.query['q']
            calls.append(title)
            if title.startswith('English'):
                return web.Response(status=429)
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
        self.assertCountEqual(['English', 'English 2013', 'Русское', 'Русское 2013'], calls)
        self.assertEqual(['Русское.S01E01.1080p'], [item['name'] for item in results])

    def test_download_key_stays_on_prowlarr_origin(self):
        cli = client.ProwlarrClient('https://prowlarr.example/base/', 'synthetic')
        other = 'https://tracker.example/base/1/download'
        self.assertEqual(other, cli.download_uri(other))
        self.assertEqual('https://prowlarr.example/base/1/download?apikey=synthetic',
                         cli.download_uri('1/download'))


class ParseTests(unittest.TestCase):
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
