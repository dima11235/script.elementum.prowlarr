"""Prowlarr API v1 client. No Kodi imports or third-party dependencies."""
import json
import re
import socket
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


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
    return str(titles.get(language.lower()) or payload.get('query') or
               payload.get('title') or titles.get('en') or next(iter(titles.values()), '')).strip()


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
        self._caps = {}

    def _request(self, endpoint, params=None):
        url = urljoin(self.base, endpoint)
        if params:
            url += '?' + urlencode(params, doseq=True)
        request = Request(url, headers={'X-Api-Key': self.api_key, 'Accept': 'application/json'})
        # Do not inherit environment proxies or forward authentication on redirects.
        opener = build_opener(ProxyHandler({}), NoRedirect())
        try:
            with opener.open(request, timeout=self.timeout) as response:
                data = response.read(32 * 1024 * 1024 + 1)
                if len(data) > 32 * 1024 * 1024:
                    raise ProwlarrError('Prowlarr response exceeds 32 MiB')
                return json.loads(data)
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
        except (ValueError, UnicodeError):
            raise ProwlarrError('Prowlarr returned invalid JSON') from None

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
                except (ValueError, TypeError):
                    continue
            self._caps = {}
            self._indexers = []
            for resource in resources:
                if not isinstance(resource, dict) or not resource.get('enable') \
                        or resource.get('protocol') != 'torrent' \
                        or resource.get('supportsSearch') is False \
                        or integer(resource.get('id')) <= 0 \
                        or integer(resource.get('id')) in blocked:
                    continue
                caps = resource.get('capabilities')
                self._caps[integer(resource.get('id'))] = caps if isinstance(caps, dict) else {}
                self._indexers.append(resource)
        if not self._indexers:
            raise ProwlarrError('No enabled torrent indexers available; check Prowlarr and indexer cooldowns')
        return self._indexers

    def supported_params(self, ids, kind):
        """Search parameters every selected indexer declares support for."""
        key = ('movieSearchParams' if kind == 'movie'
               else 'tvSearchParams' if kind in ('episode', 'season')
               else 'searchParams')
        aliases = {'ep': 'episode'}
        supported = None
        for indexer_id in ids:
            caps = self._caps.get(indexer_id, {})
            values = caps.get(key) if isinstance(caps, dict) else None
            if not isinstance(values, list):
                return set()
            declared = {aliases.get(str(value).lower(), str(value).lower()) for value in values}
            supported = declared if supported is None else supported & declared
        return supported or set()

    def download_uri(self, uri):
        if not isinstance(uri, str) or not uri:
            return None
        if uri.startswith('magnet:?'):
            return uri if 'xt=urn:btih:' in uri.lower() else None
        resolved = urljoin(self.base, uri)
        base, target = urlsplit(self.base), urlsplit(resolved)
        if target.scheme not in ('http', 'https') or target.username or target.password:
            return None
        if (target.scheme, target.netloc) != (base.scheme, base.netloc):
            return resolved
        relative = target.path[len(base.path):] if target.path.startswith(base.path) else ''
        parts = relative.strip('/').split('/')
        is_proxy = (len(parts) == 2 and parts[0].isdigit() and parts[1] == 'download')
        is_proxy |= (len(parts) == 5 and parts[:3] == ['api', 'v1', 'indexer']
                     and parts[3].isdigit() and parts[4] == 'download')
        if is_proxy:
            query = [(key, value) for key, value in parse_qsl(target.query, keep_blank_values=True)
                     if key.lower() != 'apikey']
            query.append(('apikey', self.api_key))
            return urlunsplit((target.scheme, target.netloc, target.path, urlencode(query), target.fragment))
        return resolved

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
        info_hash = resource.get('infoHash') or ''
        if not re.fullmatch(r'(?:[a-fA-F0-9]{40}|[A-Z2-7a-z]{32})', str(info_hash)):
            info_hash = ''
        release_type = 9 if re.search(r'blu.?ray|bd.?remux|b[rd]rip', title, re.I) else 0
        if not release_type and re.search(r'web[ ._-]?(?:dl|rip)?', title, re.I):
            release_type = 8
        return {'name': title.strip(), 'uri': uri, 'provider': str(resource.get('indexer') or 'Prowlarr'),
                'seeds': seeds, 'peers': seeds + integer(resource.get('leechers')),
                'size': human_size(size) if size else 'Unknown', '_size_bytes': size,
                'info_hash': info_hash, 'resolution': resolution(title), 'release_type': release_type,
                'category': release_category(title)}

    SORT_KEYS = {
        'resolution': lambda item: (item['resolution'], item['seeds']),
        'seeds': lambda item: (item['seeds'], item['resolution']),
        'size': lambda item: item['_size_bytes'],
        'balanced': lambda item: item['seeds'] * 3 * (item['resolution'] or 1),
    }

    def search(self, queries, kind='search', limit=50, min_seeders=1, season=None, episode=None,
               size_ranges=None, include_unknown_size=True, include_resolutions=None,
               include_release_types=None, require_keywords='', exclude_keywords='',
               sort_by='seeds'):
        ids = [integer(item['id']) for item in self.indexers()]
        categories = [2000] if kind == 'movie' else [5000] if kind in ('episode', 'season') else []
        supported = self.supported_params(ids, kind)
        size = (size_ranges or {}).get(kind) or (size_ranges or {}).get('search') or (0, 0)
        size_min, size_max = max(0.0, size[0]), max(0.0, size[1])
        required = [word.strip().casefold() for word in require_keywords.split(',') if word.strip()]
        excluded = [word.strip().casefold() for word in exclude_keywords.split(',') if word.strip()]
        unique = {}
        for query in dict.fromkeys(queries):
            params = {'query': query, 'type': 'search', 'indexerIds': ids}
            if categories:
                params['categories'] = categories
            # Parameters go only to indexers that declare them; the title query
            # already contains SxxEyy, so unsupported indexers stay covered.
            if season is not None and integer(season) and 'season' in supported:
                params['season'] = integer(season)
            if episode is not None and integer(episode) and kind == 'episode' and 'episode' in supported:
                params['episode'] = integer(episode)
            resources = self.transport('api/v1/search', params)
            if not isinstance(resources, list):
                raise ProwlarrError('Prowlarr returned an invalid search result')
            for resource in resources:
                result = self.parse_release(resource)
                if result is None or result['seeds'] < integer(min_seeders):
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
                identity = result['info_hash'].casefold() or result['uri']
                previous = unique.get(identity)
                if previous is None or previous['seeds'] < result['seeds']:
                    unique[identity] = result
        results = sorted(unique.values(), key=self.SORT_KEYS.get(sort_by, self.SORT_KEYS['seeds']),
                         reverse=True)
        return results[:max(1, min(500, integer(limit, 50)))]
