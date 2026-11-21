import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
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
        transport = Mock(side_effect=lambda endpoint, params=None:
                         indexers if endpoint.endswith('indexer') else
                         [] if endpoint.endswith('indexerstatus') else resources or [])
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
        self.assertEqual(['Фильм 2001'], api.search_queries(payload, 'movie', 'ru'))
        self.assertEqual(['Movie 2001'], api.search_queries(payload, 'movie'))

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

    def test_season_episode_params_only_for_capable_indexers(self):
        capable = {'id': 21, 'enable': True, 'protocol': 'torrent', 'supportsSearch': True,
                   'capabilities': {'tvSearchParams': ['q', 'season', 'ep']}}
        client, transport = self.client([release()])
        client.search(['Show S01E02'], kind='episode', season=1, episode=2)
        self.assertNotIn('season', transport.call_args.args[1])
        resources = [capable]
        transport = Mock(side_effect=lambda endpoint, params=None:
                         resources if endpoint.endswith('indexer') else
                         [] if endpoint.endswith('indexerstatus') else [release()])
        client = api.Client('http://prowlarr.example', 'synthetic', transport=transport)
        client.search(['Show S01E02'], kind='episode', season=1, episode=2)
        self.assertEqual(1, transport.call_args.args[1].get('season'))
        self.assertEqual(2, transport.call_args.args[1].get('episode'))

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
