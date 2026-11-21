import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
from threading import Event
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from xml.etree import ElementTree
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


api = load('prowlarr_tested', ROOT / 'resources/lib/prowlarr.py')
builder = load('prowlarr_builder_tested', ROOT / 'build.py')


def release(**extra):
    return {'title': 'Movie 1080p WEB-DL', 'protocol': 'torrent',
            'downloadUrl': '/21/download?link=opaque&file=movie', 'seeders': 5,
            'leechers': 2, 'size': 1024 ** 3, 'indexer': 'Tracker',
            'infoHash': 'a' * 40, **extra}


class ProwlarrTests(unittest.TestCase):
    def client(self, resources=None):
        indexers = [{'id': 21, 'enable': True, 'protocol': 'torrent', 'supportsSearch': True},
                    {'id': 0, 'enable': True, 'protocol': 'torrent'},
                    {'id': 22, 'enable': False, 'protocol': 'torrent'},
                    {'id': 23, 'enable': True, 'protocol': 'usenet'}]
        if resources is None:
            resources = []
        transport = Mock(side_effect=lambda endpoint, params=None:
                         indexers if endpoint.endswith('indexer') else
                         [] if endpoint.endswith('indexerstatus') else resources)
        return api.Client('https://prowlarr.example/base', 'synthetic', transport=transport), transport

    def test_real_torrent_indexers_and_movie_categories(self):
        client, transport = self.client([release()])
        results = client.search(['Movie'], kind='movie')
        self.assertEqual(1, len(results))
        self.assertEqual(('api/v1/search', {'query': 'Movie', 'type': 'search',
                                          'indexerIds': [21], 'categories': [2000]}),
                         transport.call_args.args)
        self.assertEqual(5, results[0]['seeds'])
        self.assertEqual(7, results[0]['peers'])
        self.assertEqual(3, results[0]['resolution'])
        self.assertEqual(8, results[0]['release_type'])

    def test_media_uses_per_indexer_torznab_and_survives_an_indexer_error(self):
        indexers = [
            {'id': 21, 'enable': True, 'protocol': 'torrent', 'name': 'First',
             'capabilities': {'tvSearchParams': ['q', 'season', 'ep']}},
            {'id': 22, 'enable': True, 'protocol': 'torrent', 'name': 'Second',
             'capabilities': {'tvSearchParams': ['q', 'season']}},
        ]
        magnet = 'magnet:?xt=urn:btih:' + 'a' * 40
        xml = f'''<rss xmlns:torznab="http://torznab.com/schemas/2015/feed"><channel>
            <item><title>Show S01E02</title><size>1048576</size>
                <enclosure url="{magnet}" length="1048576"/>
                <torznab:attr name="seeders" value="3"/>
                <torznab:attr name="peers" value="8"/>
            </item>
            <item><title>Show S01E03</title><link>{magnet}</link></item>
        </channel></rss>'''

        def transport(endpoint, params=None):
            if endpoint == 'api/v1/indexer':
                return indexers
            if endpoint == 'api/v1/indexerstatus':
                return []
            if '/22/' in endpoint:
                raise api.ProwlarrError('Prowlarr indexers are temporarily unavailable (HTTP 429)')
            return ElementTree.fromstring(xml)

        fetch = Mock(side_effect=transport)
        client = api.Client('http://prowlarr.example', 'synthetic', transport=fetch)
        results = client.search_media({'title': 'Show', 'season': 1, 'episode': 2}, kind='episode')
        self.assertEqual(['Show S01E02'], [item['name'] for item in results])
        self.assertEqual(('First', 3, 8, magnet),
                         tuple(results[0][key] for key in ('provider', 'seeds', 'peers', 'uri')))
        fetch.assert_any_call('api/v1/indexer/21/newznab', {'t': 'tvsearch', 'q': 'Show', 'season': 1, 'ep': 2})
        fetch.assert_any_call('api/v1/indexer/22/newznab', {'t': 'tvsearch', 'q': 'Show S01E02'})

    def test_progress_reports_fast_indexer_while_slow_indexer_is_pending(self):
        slow_can_finish = Event()
        indexers = [{'id': 21, 'enable': True, 'protocol': 'torrent', 'name': 'Slow'},
                    {'id': 22, 'enable': True, 'protocol': 'torrent', 'name': 'Fast'}]
        events = []

        def transport(endpoint, params=None):
            if endpoint == 'api/v1/indexer':
                return indexers
            if endpoint == 'api/v1/indexerstatus':
                return []
            if '/21/' in endpoint:
                self.assertTrue(slow_can_finish.wait(timeout=3))
                raise api.ProwlarrError('Unavailable')
            return ElementTree.fromstring('<rss><channel><item><title>Movie</title></item></channel></rss>')

        def progress(percent, event, *args):
            events.append((percent, event, *args))
            if event == 'indexer_results' and args[0] == 'Fast':
                slow_can_finish.set()

        client = api.Client('http://prowlarr.example', 'synthetic', transport=transport)
        client.search_media('Movie', progress=progress)
        self.assertEqual([
            (5, 'indexers', 'Slow, Fast'),
            (42, 'indexer_results', 'Fast', 1),
            (80, 'indexer_error', 'Slow'),
        ], events)

    def test_progress_window_is_the_only_dialog(self):
        gui = Mock()
        settings = Mock()
        settings.getSettingInt.return_value = 0
        settings.getLocalizedString.side_effect = {
            32064: 'Starting', 32065: 'Checking: {}', 32021: 'Failed',
        }.get
        addon_api = Mock()
        addon_api.Addon.return_value = settings
        with patch.dict(sys.modules, {'xbmc': Mock(), 'xbmcgui': gui, 'xbmcaddon': addon_api, 'prowlarr': api}):
            with patch.object(sys, 'path', sys.path[:]):
                addon = load('addon_tested', ROOT / 'addon.py')

        def search_media(*_args, progress, **_kwargs):
            progress(5, 'indexers', 'Tracker')
            return [{'name': 'Movie'}]

        client = Mock()
        with patch.object(addon, 'client', return_value=client):
            client.search_media.side_effect = search_media
            self.assertEqual([{'name': 'Movie'}], addon.search('Movie'))
            gui.DialogProgressBG.return_value.update.assert_called_once_with(5, message='Checking: Tracker')
            gui.Dialog.return_value.notification.assert_not_called()
            client.search_media.side_effect = api.ProwlarrError('Unavailable')
            self.assertEqual([], addon.search('Movie'))
            gui.DialogProgressBG.return_value.update.assert_any_call(100, message='Unavailable')
            client.search_media.side_effect = RuntimeError
            self.assertEqual([], addon.search('Movie'))
            gui.DialogProgressBG.return_value.update.assert_any_call(100, message='Failed')
        self.assertEqual(3, gui.DialogProgressBG.return_value.close.call_count)
        gui.Dialog.return_value.notification.assert_not_called()

    def test_torznab_movie_uses_typed_year_when_available(self):
        movie = ('Movie', 2001, None, None)
        indexer = {'capabilities': {'movieSearchParams': ['q', 'year']}}
        self.assertEqual({'t': 'movie', 'q': 'Movie', 'year': 2001}, api.torznab_params(indexer, 'movie', movie))
        self.assertEqual({'t': 'search', 'q': 'Movie 2001'}, api.torznab_params({}, 'movie', movie))

    def test_proxy_resolves_magnet_and_rejects_credentials_in_external_redirects(self):
        client, _ = self.client()
        proxy = 'https://prowlarr.example/base/21/download?link=opaque&apikey=synthetic'
        magnet = 'magnet:?xt=urn:btih:' + 'a' * 40
        for location, expected in ((magnet, magnet),
                                   ('https://tracker.example/item?key=%73ynthetic', None)):
            opener = Mock()
            opener.open.side_effect = HTTPError(proxy, 302, '', {'Location': location}, io.BytesIO())
            with patch.object(api, 'build_opener', return_value=opener):
                self.assertEqual(expected, client.resolve_proxy_magnet(proxy))
            self.assertEqual(1, opener.open.call_count)
            self.assertEqual({}, opener.open.call_args.args[0].headers)

    def test_proxy_preserves_torrent_url(self):
        client, _ = self.client()
        proxy = 'https://prowlarr.example/base/21/download?link=opaque&apikey=synthetic'
        opener = Mock()
        response = opener.open.return_value
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.headers.get_content_type.return_value = 'application/x-bittorrent'
        with patch.object(api, 'build_opener', return_value=opener):
            self.assertEqual(proxy, client.resolve_proxy_magnet(proxy))

    def test_media_tries_another_source_with_same_hash_when_proxy_fails(self):
        digest = 'a' * 40
        magnet = 'magnet:?xt=urn:btih:' + digest
        root = ElementTree.fromstring(f'''<rss xmlns:torznab="http://torznab.com/schemas/2015/feed"><channel>
            <item><title>Movie fast</title><link>/base/21/download?link=missing</link>
                <torznab:attr name="seeders" value="50"/>
                <torznab:attr name="infohash" value="{digest}"/>
            </item>
            <item><title>Movie playable</title><link>{magnet}</link>
                <torznab:attr name="seeders" value="3"/>
            </item>
        </channel></rss>''')
        client, _ = self.client(root)
        opener = Mock()
        opener.open.side_effect = HTTPError('https://prowlarr.example', 502, '', {}, io.BytesIO())
        with patch.object(api, 'build_opener', return_value=opener):
            result = client.search_media('Movie', limit=1)
        self.assertEqual([magnet], [item['uri'] for item in result])

    def test_no_enabled_indexer_is_reported(self):
        client = api.Client('http://prowlarr.example', 'synthetic', transport=Mock(return_value=[]))
        with self.assertRaisesRegex(api.ProwlarrError, 'No enabled torrent'):
            client.indexers()

    def test_no_indexer_shape_guessing(self):
        client = api.Client('http://prowlarr.example', 'synthetic', transport=Mock(return_value={}))
        with self.assertRaises(api.ProwlarrError):
            client.indexers()

    def test_cooldowns_are_excluded_before_search(self):
        transport = Mock(side_effect=[
            [{'id': 21, 'enable': True, 'protocol': 'torrent'},
             {'id': 23, 'enable': True, 'protocol': 'torrent'}],
            [{'indexerId': 21, 'disabledTill': '2099-01-01T00:00:00Z'}],
            [release()],
        ])
        client = api.Client('http://prowlarr.example', 'synthetic', transport=transport)
        self.assertEqual(1, len(client.search(['Movie'])))
        self.assertEqual([23], transport.call_args.args[1]['indexerIds'])

    def test_queries(self):
        self.assertEqual(['Matrix'], api.search_queries('Matrix'))
        self.assertEqual(['Movie 1999'], api.search_queries({'title': 'Movie', 'year': 1999}, 'movie'))
        payload = {'title': 'Show', 'season': 0, 'episode': 2}
        self.assertEqual(['Show S00E02'], api.search_queries(payload, 'episode'))
        self.assertEqual(['Show S00E02', 'Show S00'],
                         api.search_queries(payload, 'episode', season_fallback=True))
        self.assertEqual(['Show S00'], api.search_queries(payload, 'season'))
        self.assertEqual([], api.search_queries('  '))

    def test_localized_titles(self):
        payload = {'title': 'Movie', 'titles': {'RU': 'Фильм', 'en': 'Movie'}, 'year': 2001}
        self.assertEqual(['Фильм 2001', 'Movie 2001'], api.search_queries(payload, 'movie', 'ru'))
        self.assertEqual(['Movie 2001'], api.search_queries(payload, 'movie'))

    def test_search_covers_localized_and_english_titles(self):
        payload = {'title': 'Interstellar', 'titles': {'ru': 'Интерстеллар', 'en': 'Interstellar'},
                   'query': 'Interstellar', 'year': 2014}
        self.assertEqual(['Интерстеллар', 'Interstellar'], api.search_titles(payload, 'ru', 'movie'))
        self.assertEqual(['Интерстеллар 2014', 'Interstellar 2014'],
                         api.search_queries(payload, 'movie', 'ru'))
        self.assertEqual(['Interstellar'], api.search_titles(payload, 'ru', 'search'))
        russian = {'title': 'Движение вверх', 'titles': {'en': 'Upward Motion'}, 'query': 'Upward Motion'}
        self.assertEqual(['Движение вверх', 'Upward Motion'], api.search_titles(russian, 'ru', 'movie'))
        self.assertEqual(['Query'], api.search_titles('Query', 'ru', 'search'))
        same = {'title': 'Movie', 'titles': {'ru': 'Movie'}}
        self.assertEqual(['Movie'], api.search_titles(same, 'ru', 'movie'))

    def test_media_searches_every_title_on_each_indexer(self):
        indexers = [{'id': 21, 'enable': True, 'protocol': 'torrent', 'name': 'A'},
                    {'id': 22, 'enable': True, 'protocol': 'torrent', 'name': 'B'}]
        endpoints = []

        def transport(endpoint, params=None):
            if endpoint == 'api/v1/indexer':
                return indexers
            if endpoint == 'api/v1/indexerstatus':
                return []
            endpoints.append((endpoint, params))
            return ElementTree.fromstring(
                '<rss xmlns:torznab="http://torznab.com/schemas/2015/feed"><channel>'
                '<item><title>Movie 1080p WEB-DL</title>'
                '<torznab:attr name="seeders" value="7"/></item>'
                '</channel></rss>')

        client = api.Client('http://prowlarr.example', 'synthetic', transport=transport)
        payload = {'title': 'Interstellar', 'titles': {'ru': 'Интерстеллар'}, 'query': 'Interstellar',
                   'year': 2014}
        client.search_media(payload, kind='movie', language='ru', limit=10)
        searched = [params['q'] for endpoint, params in endpoints if endpoint.endswith('/newznab')]
        self.assertEqual(sorted(searched),
                         ['Interstellar 2014', 'Interstellar 2014',
                          'Интерстеллар 2014', 'Интерстеллар 2014'])
        self.assertEqual({'21', '22'},
                         {endpoint.split('/')[3] for endpoint, _ in endpoints if '/newznab' in endpoint})

    def test_download_proxy_authorized_once_and_not_external(self):
        client, _ = self.client()
        for uri in ['/base/21/download?apikey=old&link=opaque',
                    '/base/api/v1/indexer/21/download?link=opaque']:
            url = client.download_uri(uri)
            self.assertEqual(['synthetic'], parse_qs(urlsplit(url).query)['apikey'])
        for uri in ['https://tracker.example/download', '//tracker.example/download']:
            self.assertNotIn('synthetic', client.download_uri(uri))
        self.assertEqual('https://prowlarr.example/unrelated/download',
                         client.download_uri('/unrelated/download'))

    def test_invalid_uri_is_ignored(self):
        client, _ = self.client()
        for uri in ['file:///etc/passwd', 'javascript:alert(1)', 'magnet:?xt=invalid', None]:
            self.assertIsNone(client.download_uri(uri))
        self.assertEqual('magnet:?xt=urn:btih:' + 'a' * 40,
                         client.download_uri('magnet:?xt=urn:btih:' + 'a' * 40))

    def test_bad_results_do_not_hide_good_result(self):
        client, _ = self.client([{}, None, release(title=None), release(protocol='usenet'),
                                release(downloadUrl=None), release()])
        self.assertEqual(1, len(client.search(['Movie'])))

    def test_filters_deduplication_sort_and_limit(self):
        client, _ = self.client([release(seeders=1), release(seeders=10),
                                release(title='Movie CAM', infoHash='b' * 40),
                                release(title='Movie 2160p', infoHash='c' * 40, size=10 * 1024 ** 3),
                                release(title='Other', infoHash='d' * 40)])
        results = client.search(['Movie', 'Movie'], min_seeders=2,
                                size_ranges={'search': (0, 2)},
                                require_keywords='movie,web', exclude_keywords='cam', limit=1)
        self.assertEqual(1, len(results))
        self.assertEqual(10, results[0]['seeds'])

    def test_release_category_detection(self):
        samples = {'Bluray REMUX 2160p': 'bluray', 'Movie WEB-DL 1080p': 'web',
                   'Show HDTV x264': 'hdtv', 'Film DVDRip': 'dvd', 'Series TVRip': 'tvrip',
                   'Movie CAM': 'screener', 'Telesync release': 'screener', 'Mystery release': 'unknown'}
        for title, expected in samples.items():
            self.assertEqual(expected, api.release_category(title), title)

    def test_resolution_filter_excludes_unchecked_tiers(self):
        client, _ = self.client([release(title='Movie 2160p', infoHash='a' * 40),
                                 release(infoHash='b' * 40), release(title='Movie 480p', infoHash='c' * 40)])
        results = client.search(['Movie'], include_resolutions={3})
        self.assertEqual(1, len(results))
        self.assertEqual(3, results[0]['resolution'])

    def test_release_type_filter_hides_screener_by_default(self):
        client, _ = self.client([release(), release(title='Movie CAM', infoHash='b' * 40),
                                 release(title='Movie Trailer', infoHash='c' * 40)])
        results = client.search(['Movie'], include_release_types={'bluray', 'web', 'hdtv',
                                                                  'dvd', 'tvrip', 'unknown'})
        self.assertEqual(1, len(results))
        self.assertEqual('web', results[0]['category'])

    def test_size_limits_per_kind_and_unknown_size(self):
        resources = [release(size=1024 ** 3), release(size=100 * 1024 ** 3, infoHash='b' * 40),
                     release(size=None, infoHash='c' * 40)]
        client, _ = self.client(resources)
        results = client.search(['Movie'], kind='movie', size_ranges={'movie': (0.5, 30)},
                                include_unknown_size=False)
        self.assertEqual(1, len(results))
        client, _ = self.client(resources)
        results = client.search(['Movie'], kind='movie', size_ranges={'movie': (0.5, 30)})
        self.assertEqual(2, len(results))

    def test_sort_modes(self):
        low = release(title='Movie 480p', infoHash='a' * 40, size=1024 ** 3, seeders=1)
        high = release(title='Movie 2160p', infoHash='b' * 40, size=50 * 1024 ** 3, seeders=3)
        tiny = release(infoHash='c' * 40, seeders=10)
        for mode, expected_name in (('resolution', 'Movie 2160p'), ('seeds', 'Movie 1080p WEB-DL'),
                                    ('size', 'Movie 2160p'), ('balanced', 'Movie 1080p WEB-DL')):
            client, _ = self.client([low, high, tiny])
            results = client.search(['Movie'], sort_by=mode, limit=1)
            self.assertEqual(expected_name, results[0]['name'], mode)

    def test_tv_search_uses_query_and_filters_wrong_episodes(self):
        client, transport = self.client([
            release(title='Show S01E02', infoHash='a' * 40),
            release(title='Show S01E03', infoHash='b' * 40),
            release(title='Show S02E02', infoHash='c' * 40),
            release(title='Show Season 1', infoHash='d' * 40),
        ])
        results = client.search(['Show S01E02'], kind='episode', season=1, episode=2)
        self.assertEqual(['Show S01E02'], [item['name'] for item in results])
        self.assertEqual({'query': 'Show S01E02', 'type': 'search',
                          'indexerIds': [21], 'categories': [5000]}, transport.call_args.args[1])

    def test_tv_broad_fallback_requires_matching_episode_or_pack(self):
        client, transport = self.client()
        client.indexers()
        transport.side_effect = [[], [release(title='Other 2020')],
                                 [release(title='Show S01E01-E03')]]
        payload = {'title': 'Show', 'year': 2020, 'season_year': 2019, 'show_year': 2019}
        result = client.search(['Show S01E02'], kind='episode', season=1, episode=2,
                               fallback_queries=api.tv_fallback_queries(payload, ''))
        self.assertEqual(['Show S01E01-E03'], [item['name'] for item in result])
        self.assertEqual('Show 2019', transport.call_args.args[1]['query'])

    def test_season_query_accepts_matching_pack_when_enabled(self):
        client, transport = self.client()
        client.indexers()
        transport.side_effect = [[], [release(title='Show Season 1'),
                                     release(title='Show Season 2', infoHash='b' * 40),
                                     release(title='Show 2020', infoHash='c' * 40)]]
        result = client.search(['Show S01E02', 'Show S01'], kind='episode', season=1, episode=2,
                               allow_season_pack=True)
        self.assertEqual(['Show Season 1'], [item['name'] for item in result])

    def test_episode_fallback_recognizes_localized_and_compact_titles(self):
        candidates = [
            'Show 1x02',
            'Show.S01E01E02.1080p',
            'Шоу (1 сезон, серии 1–4 из 8)',
            'Шоу Сезон: 1 / Серия: 2',
            'Show 1x03',
            'Show.S01E01E03.1080p',
            'Шоу Сезон 1 [Серии 3-8]',
            'Шоу 2 сезон, серия 2',
        ]
        client, transport = self.client()
        client.indexers()
        transport.side_effect = [[], [
            release(title=title, infoHash=f'{index:040x}')
            for index, title in enumerate(candidates)
        ]]
        result = client.search(['Show S01E02'], kind='episode', season=1, episode=2,
                               fallback_queries=['Show'], allow_season_pack=True)
        self.assertCountEqual(candidates[:4], [item['name'] for item in result])

    def test_season_search_does_not_treat_numbered_episodes_as_packs(self):
        client, _ = self.client([
            release(title='Шоу 1 сезон 1080p', infoHash='a' * 40),
            release(title='Шоу Сезон 1, серия 2', infoHash='b' * 40),
            release(title='Show 1x02', infoHash='c' * 40),
            release(title='Show.S01E02-1080p', infoHash='d' * 40),
        ])
        result = client.search(['Show S01'], kind='season', season=1)
        self.assertEqual(['Шоу 1 сезон 1080p'], [item['name'] for item in result])

    def test_optional_search_error_keeps_primary_results(self):
        client, transport = self.client()
        client.indexers()
        transport.side_effect = [[release(title='Show S01E02')], api.ProwlarrError('HTTP 429')]
        result = client.search(['Show S01E02', 'Show S01'], kind='episode', season=1, episode=2,
                               allow_season_pack=True)
        self.assertEqual(['Show S01E02'], [item['name'] for item in result])

    def test_deduplicates_magnets_with_different_parameters(self):
        uri = 'magnet:?xt=urn:btih:' + 'a' * 40
        client, _ = self.client([release(magnetUrl=uri + '&dn=one', infoHash=None),
                                 release(magnetUrl=uri + '&dn=two', seeders=10)])
        results = client.search(['Movie'])
        self.assertEqual([10], [item['seeds'] for item in results])

    def test_english_title_fallback(self):
        payload = {'title': 'Фильм', 'titles': {'en': 'Movie', 'ru': 'Фильм'}}
        self.assertEqual('Movie', api.title_from_payload(payload))
        self.assertEqual('Фильм', api.title_from_payload(payload, 'ru'))

    def test_movie_titles_prefer_localized_over_query(self):
        payload = {'title': 'Interstellar', 'titles': {'ru': 'Интерстеллар', 'en': 'Interstellar'},
                   'query': 'Interstellar', 'year': 2014}
        self.assertEqual('Интерстеллар', api.title_from_payload(payload, 'ru', 'movie'))
        self.assertEqual(['Интерстеллар 2014', 'Interstellar 2014'],
                         api.search_queries(payload, 'movie', 'ru'))
        self.assertEqual('Interstellar', api.title_from_payload(payload, 'ru', 'search'))
        self.assertEqual('Interstellar', api.title_from_payload(payload, '', 'movie'))
        russian = {'title': 'Движение вверх', 'titles': {'en': 'Upward Motion'}, 'query': 'Upward Motion'}
        self.assertEqual('Движение вверх', api.title_from_payload(russian, 'ru', 'movie'))
        self.assertEqual('Движение вверх', api.title_from_payload(russian, '', 'movie'))
        self.assertEqual('Upward Motion', api.title_from_payload(russian, '', 'search'))

    def test_null_numeric_fields(self):
        client, _ = self.client()
        result = client.parse_release(release(size=None, seeders=None, leechers='bad'))
        self.assertEqual(0, result['seeds'])
        self.assertEqual('Unknown', result['size'])

    def test_host_validation(self):
        for host in ['', 'file:///tmp', 'https://user:pass@prowlarr.example',
                     'https://prowlarr.example?apikey=anything']:
            with self.assertRaises(api.ProwlarrError):
                api.Client(host, 'synthetic')

    def test_request_uses_header_and_url_base(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = json.dumps([]).encode()
        opener = Mock()
        opener.open.return_value = response
        with patch.object(api, 'build_opener', return_value=opener):
            client = api.Client('https://prowlarr.example/base', 'synthetic')
            self.assertEqual([], client._request('api/v1/search', {'query': 'Фильм'}))
        request = opener.open.call_args.args[0]
        self.assertTrue(request.full_url.startswith('https://prowlarr.example/base/api/v1/search?'))
        self.assertEqual('synthetic', request.get_header('X-api-key'))
        self.assertNotIn('apikey', request.full_url)

    def test_error_redacts_server_response_and_url(self):
        for code in (401, 403, 302, 500):
            opener = Mock()
            opener.open.side_effect = HTTPError('https://example?apikey=hidden', code, 'hidden', {}, None)
            with patch.object(api, 'build_opener', return_value=opener):
                client = api.Client('https://prowlarr.example', 'synthetic')
                with self.assertRaises(api.ProwlarrError) as raised:
                    client._request('api/v1/indexer')
            self.assertNotIn('hidden', str(raised.exception))

    def test_zip_is_reproducible_and_has_only_installable_files(self):
        with tempfile.TemporaryDirectory() as directory:
            path = builder.build(directory)
            first = path.read_bytes()
            builder.build(directory)
            self.assertEqual(first, path.read_bytes())
            with ZipFile(path) as archive:
                expected = {f'script.elementum.prowlarr/{name}' for name in builder.PACKAGE_FILES}
                self.assertEqual(expected, set(archive.namelist()))
                self.assertTrue(archive.testzip() is None)
                self.assertFalse(any('.git' in name or '/tests/' in name for name in archive.namelist()))

    def test_mismatched_tag_cannot_be_released(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', {'GITHUB_REF': 'refs/tags/v9.9.9'}):
            with self.assertRaisesRegex(ValueError, 'Git tag'):
                builder.build(directory)


if __name__ == '__main__':
    unittest.main()
