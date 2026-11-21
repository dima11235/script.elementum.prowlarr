"""Prowlarr JSON and Torznab client. No Kodi imports or third-party dependencies."""
import json
import re
import socket
from base64 import b32decode
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from time import monotonic
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from xml.etree import ElementTree


class ProwlarrError(Exception):
    """Safe diagnostic: never includes credentials or download URLs."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def integer(value, default=0):
    try:
        return max(0, int(value))
    except (ValueError, TypeError, OverflowError):
        return default


def resolution(title):
    for pattern, level in [(r'2160p|\b4k\b|\buhd\b', 5), (r'1440p', 4),
                           (r'1080[pi]', 3), (r'720p', 2), (r'480p', 1)]:
        if re.search(pattern, title, re.IGNORECASE):
            return level
    return 0


RELEASE_CATEGORIES = (
    ('screener', r'\b(cam|hdcam|telesync|\bts\b|\btc\b|dvdscr|screener|trailer|workprint|vhs)\b'),
    ('bluray', r'blu.?ray|bd(remux|rip)|remux'),
    ('web', r'web[ ._-]?(dl|rip)'),
    ('hdtv', r'\b(hdtv|hdtc|pdtv|dsr(ip)?)\b'),
    ('dvd', r'\bdvd(rip|r|5|9)?\b'),
    ('tvrip', r'\b(hdtvrip|tvrip|iptv|satrip)\b'),
)


def release_category(title):
    """Shortened Jackett-style release type detection for result filtering."""
    for name, pattern in RELEASE_CATEGORIES:
        if re.search(pattern, title, re.IGNORECASE):
            return name
    return 'unknown'


def human_size(size):
    value = float(size)
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if value < 1024 or unit == 'TiB':
            return f'{value:.2f} {unit}'
        value /= 1024


def title_from_payload(payload, language=''):
    if isinstance(payload, str):
        return payload.strip()
    titles = {str(key).lower(): value for key, value in (payload.get('titles') or {}).items()}
    original = str(payload.get('title') or '').strip()
    if original.isascii():
        english = original or titles.get('en')
    else:
        english = titles.get('en') or original
    return str(payload.get('query') or titles.get(language.lower()) or english or
               next(iter(titles.values()), '')).strip()


def search_queries(payload, kind='search', language='', season_fallback=False):
    title = title_from_payload(payload, language)
    if not title:
        return []
    if isinstance(payload, str) or kind == 'search':
        return [title]
    if kind == 'movie':
        year = integer(payload.get('year'))
        return [f'{title} {year}' if year else title]
    season = payload.get('season')
    if season is None:
        return [title]
    season_query = f'{title} S{integer(season):02d}'
    episode = payload.get('episode')
    if kind == 'episode' and episode is not None:
        query = f'{season_query}E{integer(episode):02d}'
        return [query, season_query] if season_fallback else [query]
    return [season_query]


def tv_fallback_queries(payload, language):
    title = title_from_payload(payload, language)
    years = dict.fromkeys(integer(payload.get(key)) for key in ('year', 'season_year', 'show_year'))
    return [f'{title} {year}' for year in years if year] + [title]


SEASON_MARKER = re.compile(
    r'(?<!\w)(?:(?:s(?:eason)?|сезон)[\s._:-]*(\d{1,3})|(\d{1,3})[ ._-]+сезон)'
    r'(?=$|[^\w]|e(?:p)?[ ._-]*\d)', re.IGNORECASE,
)
EPISODE_WORD = r'(?:e(?:p(?:isodes?)?)?|сери[яий]|эпизод(?:ы|ов)?)'
EPISODE_MARKER = re.compile(
    rf'[\s._,:/(\[-]*{EPISODE_WORD}[\s._:-]*(\d{{1,4}})'
    rf'(?:\s*[-–]\s*(?:{EPISODE_WORD}[\s._:-]*)?(\d{{1,4}}))?'
    r'(?=$|[^\w]|e\d)', re.IGNORECASE,
)
COMPACT_EPISODE = re.compile(
    r'(?<!\w)(\d{1,3})x(\d{1,4})(?:[-–](\d{1,4}))?(?!\w)', re.IGNORECASE,
)


def matches_tv(title, season, episode, allow_pack, require_marker):
    has_marker = False
    for marker in COMPACT_EPISODE.finditer(title):
        has_marker = True
        found_season, first, last = marker.groups()
        if int(found_season) == season and episode is not None and int(first) <= episode <= int(last or first):
            return True

    for marker in SEASON_MARKER.finditer(title):
        has_marker = True
        prefix, suffix = marker.groups()
        found_season = prefix or suffix
        if int(found_season) != season:
            continue
        entry = EPISODE_MARKER.match(title, marker.end())
        if entry is None and allow_pack:
            return True
        while entry is not None and episode is not None:
            first, last = entry.groups()
            if int(first) <= episode <= int(last or first):
                return True
            entry = EPISODE_MARKER.match(title, entry.end())
    return not has_marker and not require_marker


def torrent_hash(value):
    value = str(value)
    if re.fullmatch(r'[a-fA-F0-9]{40}', value):
        return value.lower()
    if re.fullmatch(r'[A-Z2-7]{32}', value, re.IGNORECASE):
        return b32decode(value.upper()).hex()
    return ''


def magnet_hash(uri):
    if uri.startswith('magnet:?'):
        for key, value in parse_qsl(urlsplit(uri).query):
            if key.lower() == 'xt' and value.lower().startswith('urn:btih:'):
                digest = torrent_hash(value[9:])
                if digest:
                    return digest
    return ''


def torznab_params(indexer, kind, query):
    title, year, season, episode = query
    modes = {'movie': ('movie', 'movieSearchParams'),
             'season': ('tvsearch', 'tvSearchParams'), 'episode': ('tvsearch', 'tvSearchParams')}
    mode, key = modes.get(kind, ('search', 'searchParams'))
    capabilities = indexer.get('capabilities') or {}
    declared = capabilities.get(key) if isinstance(capabilities, dict) else None
    supported = {str(value).lower() for value in declared} if isinstance(declared, list) else set()
    if 'q' not in supported:
        mode, supported = 'search', set()
    params = {'t': mode, 'q': title}
    if year:
        if 'year' in supported:
            params['year'] = year
        else:
            params['q'] += f' {year}'
    if season is not None:
        if 'season' in supported and (episode is None or 'ep' in supported):
            params['season'] = season
            if episode is not None:
                params['ep'] = episode
        else:
            params['q'] += f' S{season:02d}'
            if episode is not None:
                params['q'] += f'E{episode:02d}'
    return params


TORZNAB_ATTR = '{http://torznab.com/schemas/2015/feed}attr'


def torznab_releases(root, indexer):
    if not isinstance(root, ElementTree.Element) or root.tag != 'rss':
        raise ProwlarrError('Prowlarr returned an invalid Torznab feed')
    for item in root.findall('channel/item'):
        attributes = {node.get('name', '').lower(): node.get('value') for node in item.findall(TORZNAB_ATTR)}
        enclosure = item.find('enclosure')
        yield {
            'title': item.findtext('title'),
            'indexer': indexer.get('name'),
            'magnetUrl': attributes.get('magneturl'),
            'downloadUrl': item.findtext('link') or (enclosure.get('url') if enclosure is not None else None),
            'seeders': attributes.get('seeders'),
            'peers': attributes.get('peers'),
            'size': item.findtext('size') or attributes.get('size') or
                    (enclosure.get('length') if enclosure is not None else None),
            'infoHash': attributes.get('infohash'),
        }


class Client:
    def __init__(self, host, api_key, timeout=60, transport=None):
        parsed = urlsplit(host.strip())
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ProwlarrError('Set a valid Prowlarr HTTP(S) URL without credentials or query parameters')
        if not api_key.strip() or any(char in api_key for char in '\r\n'):
            raise ProwlarrError('Set your Prowlarr API key')
        self.base = host.strip().rstrip('/') + '/'
        self.api_key = api_key.strip()
        self.timeout = max(5, min(180, integer(timeout, 60)))
        self.transport = transport or self._request
        self._indexers = None

    def _request(self, endpoint, params=None):
        torznab = endpoint.endswith('/newznab')
        url = urljoin(self.base, endpoint)
        if params:
            url += '?' + urlencode(params, doseq=True)
        accept = 'application/xml' if torznab else 'application/json'
        request = Request(url, headers={'X-Api-Key': self.api_key, 'Accept': accept})
        # Do not inherit environment proxies or forward authentication on redirects.
        opener = build_opener(ProxyHandler({}), NoRedirect())
        try:
            with opener.open(request, timeout=self.timeout) as response:
                data = response.read(32 * 1024 * 1024 + 1)
                if len(data) > 32 * 1024 * 1024:
                    raise ProwlarrError('Prowlarr response exceeds 32 MiB')
                return ElementTree.fromstring(data) if torznab else json.loads(data)
        except HTTPError as error:
            if error.code in (401, 403):
                raise ProwlarrError('Prowlarr rejected the API key') from None
            if error.code == 429:
                raise ProwlarrError('Prowlarr indexers are temporarily unavailable (HTTP 429)') from None
            raise ProwlarrError(f'Prowlarr returned HTTP {error.code}') from None
        except (TimeoutError, socket.timeout):
            raise ProwlarrError('Prowlarr request timed out') from None
        except URLError:
            raise ProwlarrError('Cannot connect to Prowlarr; check URL, network and certificate') from None
        except (ValueError, ElementTree.ParseError):
            raise ProwlarrError(f'Prowlarr returned invalid {"XML" if torznab else "JSON"}') from None

    def indexers(self):
        if self._indexers is None:
            resources = self.transport('api/v1/indexer')
            if not isinstance(resources, list):
                raise ProwlarrError('Prowlarr returned an invalid indexer list')
            statuses = self.transport('api/v1/indexerstatus')
            blocked = set()
            if not isinstance(statuses, list):
                raise ProwlarrError('Prowlarr returned an invalid indexer status list')
            for status in statuses:
                until = status.get('disabledTill') if isinstance(status, dict) else None
                if not until:
                    continue
                try:
                    deadline = datetime.fromisoformat(until.replace('Z', '+00:00'))
                    if deadline.replace(tzinfo=deadline.tzinfo or timezone.utc) > datetime.now(timezone.utc):
                        blocked.add(integer(status.get('indexerId')))
                except (AttributeError, ValueError, TypeError):
                    continue
            self._indexers = []
            for resource in resources:
                if not isinstance(resource, dict) or not resource.get('enable') \
                        or resource.get('protocol') != 'torrent' \
                        or resource.get('supportsSearch') is False \
                        or integer(resource.get('id')) <= 0 \
                        or integer(resource.get('id')) in blocked:
                    continue
                self._indexers.append(resource)
        if not self._indexers:
            raise ProwlarrError('No enabled torrent indexers available; check Prowlarr and indexer cooldowns')
        return self._indexers

    def is_download_proxy(self, target):
        base = urlsplit(self.base)
        if (target.scheme, target.netloc) != (base.scheme, base.netloc):
            return False
        relative = target.path[len(base.path):] if target.path.startswith(base.path) else ''
        parts = relative.strip('/').split('/')
        return ((len(parts) == 2 and parts[0].isdigit() and parts[1] == 'download')
                or (len(parts) == 5 and parts[:3] == ['api', 'v1', 'indexer']
                    and parts[3].isdigit() and parts[4] == 'download'))

    def download_uri(self, uri):
        if not isinstance(uri, str) or not uri:
            return None
        if uri.startswith('magnet:?'):
            return uri if magnet_hash(uri) else None
        try:
            resolved = urljoin(self.base, uri)
            target = urlsplit(resolved)
        except ValueError:
            return None
        if target.scheme not in ('http', 'https') or not target.hostname or target.username or target.password:
            return None
        if self.is_download_proxy(target):
            query = [(key, value) for key, value in parse_qsl(target.query, keep_blank_values=True)
                     if key.lower() != 'apikey']
            query.append(('apikey', self.api_key))
            return urlunsplit((target.scheme, target.netloc, target.path, urlencode(query), target.fragment))
        return resolved

    def resolve_proxy_magnet(self, uri, deadline=None):
        """Resolve magnet redirects without sending the API key to another origin."""
        if not self.is_download_proxy(urlsplit(uri)):
            return uri
        opener = build_opener(ProxyHandler({}), NoRedirect())
        current = uri
        if deadline is None:
            deadline = monotonic() + min(10, self.timeout)
        for _ in range(3):
            remaining = deadline - monotonic()
            if remaining <= 0:
                return None
            try:
                with opener.open(Request(current), timeout=remaining) as response:
                    # Elementum accepts .torrent URLs and uses the complete metadata.
                    return current if response.headers.get_content_type() == 'application/x-bittorrent' else None
            except HTTPError as error:
                try:
                    location = error.headers.get('Location') if error.code in (301, 302, 303, 307, 308) else None
                finally:
                    error.close()
                if not location:
                    return None
                try:
                    target = self.download_uri(urljoin(current, location))
                except ValueError:
                    return None
                if target is None:
                    return None
                if not self.is_download_proxy(urlsplit(target)):
                    return target if self.api_key not in unquote(target) else None
                current = target
            except (OSError, ValueError):
                return None
        return None

    def parse_release(self, resource):
        if not isinstance(resource, dict) or resource.get('protocol') not in (None, 'torrent'):
            return None
        title = resource.get('title')
        if not isinstance(title, str) or not title.strip():
            return None
        magnet = resource.get('magnetUrl')
        download = resource.get('downloadUrl')
        uri = self.download_uri(magnet) or self.download_uri(download)
        if not uri:
            return None
        size = integer(resource.get('size'))
        seeds = integer(resource.get('seeders'))
        info_hash = magnet_hash(uri) or torrent_hash(resource.get('infoHash'))
        release_type = 9 if re.search(r'blu.?ray|bd.?remux|b[rd]rip', title, re.I) else 0
        if not release_type and re.search(r'web[ ._-]?(?:dl|rip)?', title, re.I):
            release_type = 8
        return {'name': title.strip(), 'uri': uri, 'provider': str(resource.get('indexer') or 'Prowlarr'),
                'seeds': seeds, 'peers': integer(resource.get('peers'), seeds + integer(resource.get('leechers'))),
                'size': human_size(size) if size else 'Unknown', '_size_bytes': size,
                'info_hash': info_hash, 'resolution': resolution(title), 'release_type': release_type,
                'category': release_category(title)}

    SORT_KEYS = {
        'resolution': lambda item: (item['resolution'], item['seeds']),
        'seeds': lambda item: (item['seeds'], item['resolution']),
        'size': lambda item: item['_size_bytes'],
        'balanced': lambda item: item['seeds'] * 3 * (item['resolution'] or 1),
    }

    def _search_indexer(self, indexer, kind, query):
        endpoint = f"api/v1/indexer/{integer(indexer['id'])}/newznab"
        root = self.transport(endpoint, torznab_params(indexer, kind, query))
        return list(torznab_releases(root, indexer))

    def _torznab_search(self, pool, indexers, kind, query, progress):
        names = [str(indexer.get('name') or indexer['id']) for indexer in indexers]
        progress(0, 'indexers', ', '.join(names))
        pending = {pool.submit(self._search_indexer, indexer, kind, query): name
                   for indexer, name in zip(indexers, names)}
        releases = {}
        errors = {}
        for completed, result in enumerate(as_completed(pending), 1):
            name = pending[result]
            try:
                releases[result] = result.result()
            except ProwlarrError as error:
                errors[result] = error
                progress(completed, 'indexer_error', name)
            else:
                progress(completed, 'indexer_results', name, len(releases[result]))
        if len(errors) == len(pending):
            raise errors[next(iter(pending))]
        # Preserve indexer order for releases with identical sort keys.
        return [release for result in pending for release in releases.get(result, ())]

    def search_media(self, payload, kind='search', language='', allow_season_pack=False, limit=50,
                     progress=None, **filters):
        title = title_from_payload(payload, language)
        if not title:
            return []
        metadata = payload if isinstance(payload, dict) else {}
        season = integer(metadata.get('season'), None) if kind in ('season', 'episode') else None
        episode = integer(metadata.get('episode'), None) if kind == 'episode' else None
        year = integer(metadata.get('year')) if kind == 'movie' else 0
        primary = [(title, year, season, episode)]
        if season is not None and episode is not None and allow_season_pack:
            primary.append((title, 0, season, None))
        fallback = []
        if season is not None:
            years = (integer(metadata.get(key)) for key in ('year', 'season_year', 'show_year'))
            fallback = [(title, value, None, None) for value in dict.fromkeys(years) if value]
            fallback.append((title, 0, None, None))
        indexers = self.indexers()
        if progress is None:
            def progress(*_args):
                pass
        per_query = len(indexers)
        total = per_query * len(dict.fromkeys((*primary, *fallback)))
        requested = 0
        with ThreadPoolExecutor(max_workers=6) as pool:
            def fetch(query):
                nonlocal requested

                def report(completed, event, *args):
                    progress(5 + 75 * (requested + completed) // total, event, *args)

                resources = self._torznab_search(pool, indexers, kind, query, report)
                requested += per_query
                return resources

            candidates = self._search(
                fetch, primary, kind=kind, season=season, episode=episode,
                fallback_queries=fallback, allow_season_pack=allow_season_pack, dedup_by_hash=False, **filters,
            )
            if candidates:
                progress(85, 'links')
            return self._resolve_results(pool, candidates, self.result_limit(limit))

    def _resolve_results(self, pool, candidates, limit):
        results = []
        identities = set()
        deadline = monotonic() + min(10, self.timeout)
        # Bound work by the result limit. Try spare candidates when a proxy fails.
        for start in range(0, min(len(candidates), limit * 2), 6):
            if len(results) >= limit:
                break
            batch = candidates[start:min(start + 6, limit * 2)]
            urls = pool.map(lambda result: self.resolve_proxy_magnet(result['uri'], deadline), batch)
            for result, uri in zip(batch, urls):
                if not uri:
                    continue
                digest = magnet_hash(uri) or result['info_hash']
                identity = digest or uri
                if identity in identities:
                    continue
                identities.add(identity)
                results.append({**result, 'uri': uri, 'info_hash': digest})
        return results[:limit]

    @staticmethod
    def result_limit(limit):
        return max(1, min(500, integer(limit, 50)))

    def search(self, queries, kind='search', limit=50, **filters):
        """Keep the JSON search available for diagnostics."""
        ids = [integer(item['id']) for item in self.indexers()]
        categories = [2000] if kind == 'movie' else [5000] if kind in ('episode', 'season') else []

        def fetch(query):
            params = {'query': query, 'type': 'search', 'indexerIds': ids}
            if categories:
                params['categories'] = categories
            resources = self.transport('api/v1/search', params)
            if not isinstance(resources, list):
                raise ProwlarrError('Prowlarr returned an invalid search result')
            return resources

        return self._search(fetch, queries, kind=kind, **filters)[:self.result_limit(limit)]

    def _search(self, fetch, queries, kind='search', min_seeders=1, season=None, episode=None,
                size_ranges=None, include_unknown_size=True, include_resolutions=None,
                include_release_types=None, require_keywords='', exclude_keywords='',
                sort_by='seeds', fallback_queries=(), allow_season_pack=False, dedup_by_hash=True):
        tv = kind in ('episode', 'season') and season is not None
        season = integer(season, -1)
        episode = integer(episode, -1) if kind == 'episode' else None
        size = (size_ranges or {}).get(kind) or (size_ranges or {}).get('search') or (0, 0)
        size_min, size_max = max(0.0, size[0]), max(0.0, size[1])
        required = [word.strip().casefold() for word in require_keywords.split(',') if word.strip()]
        excluded = [word.strip().casefold() for word in exclude_keywords.split(',') if word.strip()]
        unique = {}
        primary = dict.fromkeys(queries)
        fallback = dict.fromkeys(fallback_queries)
        first_query = next(iter(primary), None)
        for query in dict.fromkeys((*primary, *fallback)):
            broad = query not in primary
            if broad and unique:
                break
            try:
                resources = fetch(query)
            except ProwlarrError:
                if unique:
                    break
                raise
            for resource in resources:
                result = self.parse_release(resource)
                if result is None or result['seeds'] < integer(min_seeders):
                    continue
                if tv and not matches_tv(result['name'], season, episode,
                                         kind == 'season' or allow_season_pack, query != first_query):
                    continue
                if include_resolutions is not None and result['resolution'] not in include_resolutions:
                    continue
                if include_release_types is not None and result['category'] not in include_release_types:
                    continue
                if size_min > 0 or size_max > 0:
                    if not result['_size_bytes']:
                        if not include_unknown_size:
                            continue
                    elif result['_size_bytes'] < size_min * 1024 ** 3 \
                            or (size_max > 0 and result['_size_bytes'] > size_max * 1024 ** 3):
                        continue
                name = result['name'].casefold()
                if any(word not in name for word in required) or any(word in name for word in excluded):
                    continue
                identity = (result['info_hash'] if dedup_by_hash else '') or result['uri']
                previous = unique.get(identity)
                if previous is None or previous['seeds'] < result['seeds']:
                    unique[identity] = result
        return sorted(unique.values(), key=self.SORT_KEYS.get(sort_by, self.SORT_KEYS['seeds']), reverse=True)
