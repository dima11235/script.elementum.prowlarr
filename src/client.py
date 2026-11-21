#!/usr/bin/env python3.6
# coding=utf-8
import time
import aiohttp
import asyncio
import json
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit, urlunsplit
from enum import Enum, Flag, auto
from http import HTTPStatus
import xml.etree.ElementTree as ElT

import utils
from logger import log
from pdialoghelper import PDialog
from utils import notify, translation, get_icon_path, human_size, get_resolution, get_release_type, get_setting, \
    set_setting


class Cap(Flag):
    QUERY = auto()
    SEASON = auto()
    EPISODE = auto()
    IMDBID = auto()
    TVDBID = auto()
    GENRE = auto()
    YEAR = auto()


class SearchType(Enum):
    COMMON = "search"
    MOVIE = "movie"
    TV = "tvsearch"


flags_mapping = {
    "q": Cap.QUERY,
    "season": Cap.SEASON,
    "ep": Cap.EPISODE,
    "imdbid": Cap.IMDBID,
    "tvdbid": Cap.TVDBID,
    "genre": Cap.GENRE,
    "year": Cap.YEAR
}


def parse_cap(atr_arr):
    r = Cap(0)
    for atr in atr_arr:
        r |= flags_mapping.get(str(atr).lower(), Cap(0))
    return r


class Indexer:

    def __init__(self, root):
        self.id = int(root["id"])
        self.name = root.get("name") or str(self.id)
        capabilities = root.get("capabilities") or {}
        tv_p = capabilities.get("tvSearchParams") or []
        movie_p = capabilities.get("movieSearchParams") or []
        self.tv_enabled = bool(tv_p)
        self.movie_enabled = bool(movie_p)
        self.tv_caps = parse_cap(tv_p)
        self.movie_caps = parse_cap(movie_p)

    def check_t_and_cap(self, t):
        if t == SearchType.MOVIE and self.movie_enabled:
            cap = self.movie_caps
        elif t == SearchType.TV and self.tv_enabled:
            cap = self.tv_caps
        else:
            t = SearchType.COMMON
            cap = Cap.QUERY
        return t, cap


class IndexerResponseError(Exception):
    pass


class IndexerRateLimited(Exception):
    pass


class ProwlarrClient:
    #  get list of configured indexers
    get_indexers_list = "api/v1/indexer"
    query_by_indexer = "api/v1/indexer/{indexer}/newznab"

    def __init__(self, host, api_key):
        self.host = host.rstrip('/') + '/'
        self.api_key = api_key
        self.indexers = []
        self.session = None
        self.deadline = None

    async def create_session(self, host=None):
        if host:
            self.host = host.rstrip('/') + '/'
        timeout = aiohttp.ClientTimeout(total=60)
        # Absolute request URLs also support URL Base with older bundled aiohttp.
        self.session = aiohttp.ClientSession(timeout=timeout)

    async def close_session(self):
        if self.session is not None and not self.session.closed:
            await self.session.close()

    async def send_request(self, url, params=None, json_response=False):
        params = dict(params or {})
        start = time.monotonic()
        remaining = self.deadline - start if self.deadline is not None else 60
        if remaining <= 0:
            raise asyncio.TimeoutError
        timeout = aiohttp.ClientTimeout(total=min(60, remaining))
        async with self.session.get(urljoin(self.host, url), params=params, timeout=timeout,
                                    headers={'X-Api-Key': self.api_key},
                                    allow_redirects=False) as resp:
            log.debug(f"Indexers request status: {resp.status}")
            if resp.status != HTTPStatus.OK:
                if resp.status == HTTPStatus.TOO_MANY_REQUESTS and not json_response:
                    retry_after = resp.headers.get('Retry-After', '')
                    raise IndexerRateLimited(int(retry_after) if retry_after.isdecimal() else None)
                if not json_response:
                    raise IndexerResponseError(resp.status)
                self.proceed_resp_error(resp.status, f"Prowlarr HTTP {resp.status}")
                return
            response_time = time.monotonic() - start
            body = await resp.text()

        log.info(f"Prowlarr returned response in {response_time:.3f}s size {len(body)} b")

        try:
            root = json.loads(body) if json_response else ElT.fromstring(body)
        except (ValueError, ElT.ParseError):
            if not json_response:
                raise IndexerResponseError(502)
            self.proceed_resp_error(502, "Invalid response from Prowlarr")
            return None
        if json_response:
            if not isinstance(root, list):
                self.proceed_resp_error(502, "Invalid indexer list from Prowlarr")
                return None
            return root
        if root.tag == "error":
            code = root.get("code", "")
            raise IndexerResponseError(int(code) if code.isdecimal() else "invalid response")
        set_setting('settings_validated', 'Success')
        return root

    async def request_indexers(self):
        self.indexers.clear()
        set_setting('settings_validated', 'Connecting to Prowlarr')
        try:
            resources = await self.send_request(self.get_indexers_list, json_response=True)
            if resources is None:
                return self.indexers
        except (asyncio.TimeoutError, aiohttp.ClientError):
            self.proceed_resp_error(503, "Cannot connect to Prowlarr")
            return self.indexers
        try:
            statuses = await self.send_request('api/v1/indexerstatus', json_response=True)
        except (asyncio.TimeoutError, aiohttp.ClientError):
            log.warning("Cannot read Prowlarr indexer status")
            statuses = None
        blocked = set()
        for status in statuses or []:
            until = status.get('disabledTill') if isinstance(status, dict) else None
            if not until:
                continue
            try:
                deadline = datetime.fromisoformat(until.replace('Z', '+00:00'))
                if deadline.replace(tzinfo=deadline.tzinfo or timezone.utc) > datetime.now(timezone.utc):
                    blocked.add(int(status['indexerId']))
            except (ValueError, TypeError, KeyError):
                continue
        for resource in resources:
            if (not isinstance(resource, dict) or not resource.get('enable')
                    or str(resource.get('protocol', '')).lower() != 'torrent'
                    or resource.get('supportsSearch') is False):
                continue
            try:
                indexer = Indexer(resource)
            except (KeyError, TypeError, ValueError):
                continue
            if indexer.id > 0:
                if indexer.id in blocked:
                    log.warning(f'Prowlarr indexer {indexer.id} is in cooldown; skipping')
                    continue
                self.indexers.append(indexer)
        if not self.indexers:
            self.proceed_resp_error(404, "No enabled torrent indexers in Prowlarr")
            return self.indexers
        set_setting('settings_validated', 'Success')
        return self.indexers

    async def search_by_indexer(self, indexer, t, title, year, season=None, ep=None, imdb_id=None):
        t, cap = indexer.check_t_and_cap(t)
        # set params
        params = {'t': t.value}
        if imdb_id and (cap & Cap.IMDBID) and get_setting('search_by_imdb_key', bool):
            params["imdbid"] = imdb_id
            season = ep = None
        elif year and (cap & Cap.YEAR):
            params["q"] = title
            params["year"] = year
        else:
            params["q"] = f"{title}"
            if year:
                params["q"] += f" {year}"

        if bool(season) and (cap & Cap.SEASON):
            params["season"] = season
            if bool(ep) and (cap & Cap.EPISODE):
                params["ep"] = ep
        elif bool(season):
            params["q"] += " S{:0>2}".format(season)
            if bool(ep):
                params["q"] += "E{:0>2}".format(ep)

        url = self.query_by_indexer.format(indexer=indexer.id)
        try:
            root = await self.send_request(url, params)
        except IndexerResponseError as error:
            log.warning(f"Prowlarr search for {indexer.name}: status={error.args[0]}")
            return [], indexer
        except asyncio.TimeoutError:
            log.warning(f"Prowlarr search for {indexer.name}: timed out")
            return [], indexer
        except aiohttp.ClientError:
            log.error(f"Prowlarr search for {indexer.name}: connection failed")
            return [], indexer

        if root is None:
            return [], indexer
        torr_list = parse_torrents(root)
        valid = []
        for torrent in torr_list:
            try:
                torrent['uri'] = self.download_uri(torrent['uri'])
            except ValueError:
                log.warning(f'Prowlarr search for {indexer.name}: invalid download URL')
                continue
            torrent['provider'] = f'[COLOR {utils.get_provider_color(indexer.name)}]{indexer.name}[/COLOR]'
            valid.append(torrent)
        log.info(f"Done searching. Got {len(valid)} torrents from {indexer.name}.")
        return valid, indexer

    def download_uri(self, uri):
        """Authenticate only same-origin Prowlarr proxy links, never tracker URLs."""
        from urllib.parse import parse_qsl, urlencode
        resolved = urljoin(self.host, uri)
        base, target = urlsplit(self.host), urlsplit(resolved)
        if (target.scheme, target.netloc) != (base.scheme, base.netloc):
            return resolved
        path = target.path[len(base.path):] if target.path.startswith(base.path) else ''
        parts = path.strip('/').split('/')
        proxy_download = (len(parts) == 2 and parts[0].isdigit() and parts[1] == 'download')
        proxy_download |= (len(parts) == 5 and parts[:3] == ['api', 'v1', 'indexer']
                           and parts[3].isdigit() and parts[4] == 'download')
        if not proxy_download:
            return resolved
        query = [(key, value) for key, value in parse_qsl(target.query, keep_blank_values=True)
                 if key.lower() != 'apikey']
        query.append(('apikey', self.api_key))
        return urlunsplit((target.scheme, target.netloc, target.path, urlencode(query), target.fragment))

    async def search_indexer(self, indexer, kind, queries, fallback):
        results = []
        for group in (queries, fallback):
            for query in group:
                if self.deadline is not None and time.monotonic() >= self.deadline:
                    return results, indexer
                try:
                    rows, _ = await self.search_by_indexer(indexer, kind, *query)
                except IndexerRateLimited as error:
                    retry_after = error.args[0]
                    log.warning(f"Prowlarr search for {indexer.name}: HTTP 429, Retry-After={retry_after}")
                    return results, indexer
                results.extend(rows)
            if len({item['name'] for item in results}) >= 3:
                break
        return results, indexer

    async def search_indexers(self, kind, queries, p_dialog_cb, fallback=()):
        tasks = [self.search_indexer(indexer, kind, queries, fallback) for indexer in self.indexers]
        pending = [indexer.name for indexer in self.indexers]

        def update():
            if p_dialog_cb:
                count = len(tasks) - len(pending)
                p_dialog_cb(count, len(tasks), utils.translation(32754).format(PDialog.default_heading, count, len(tasks)),
                            utils.translation(32752).format(', '.join(pending)))

        results = []
        if tasks:
            update()
            for task in asyncio.as_completed(tasks):
                rows, indexer = await task
                pending.remove(indexer.name)
                results.extend(rows)
                update()
        return results

    async def search_movie(self, titles, year=None, imdb_id=None, p_dialog_cb=None):
        titles = (titles,) if isinstance(titles, str) else titles
        queries = [(title, year, None, None, imdb_id) for title in titles]
        return await self.search_indexers(SearchType.MOVIE, queries, p_dialog_cb)

    async def search_tv(self, titles, year=None, season=None, ep=None, imdb_id=None, p_dialog_cb=None):
        titles = (titles,) if isinstance(titles, str) else titles
        queries = [(title, year, season, ep, imdb_id) for title in titles]
        if get_setting("search_season_on_episode", bool) and season and ep:
            queries.extend((title, year, season, None, imdb_id) for title in titles)
        return await self.search_indexers(SearchType.TV, queries, p_dialog_cb)

    async def search_query(self, title, year=None, imdb_id=None, p_dialog_cb=None):
        return await self.search_indexers(SearchType.COMMON, [(title, year, None, None, imdb_id)], p_dialog_cb)

    async def search_tv_smart(self, titles, ep_year=None, season_year=None, show_year=None, p_dialog_cb=None):
        titles = (titles,) if isinstance(titles, str) else titles
        years = dict.fromkeys(year for year in (ep_year, season_year, show_year) if year)
        queries = [(title, None) for title in titles]
        fallback = [(title, year) for year in years for title in titles]
        return await self.search_indexers(SearchType.TV, queries, p_dialog_cb, fallback)

    @staticmethod
    def proceed_resp_error(code, description):
        notify(translation(32700).format(code), image=get_icon_path())
        log.error(f"got code {code}: {description}")
        set_setting('settings_validated', description)
        return


def parse_torrents(root):
    results = []
    items = root.findall("channel/item")
    for item in items:
        try:
            result = parse_item(item)
        except (ValueError, TypeError, KeyError):
            log.warning("Ignoring malformed Prowlarr torrent item")
            continue
        if result is not None:
            results.append(result)
    return results


def parse_item(item):
    torznab_ns = "http://torznab.com/schemas/2015/feed"

    torznab_elementum_mappings = {
        "tags": {
            "title": "name",
            "jackettindexer": "provider",
            "size": "size",
        },
        "torznab_attrs": {
            "magneturl": "uri",
            "seeders": "seeds",
            "peers": "peers",
            "infohash": "info_hash",
        }
    }

    result = {
        "name": None,
        "provider": "Unknown",
        "size": "Unknown",
        "uri": None,
        "seeds": "0",
        "peers": "0",
        "info_hash": "",
        "language": None,

        # nice to assign correct icons but that can be very time consuming due to the number of indexers in Prowlarr
        "icon": get_icon_path(),

        "_size_bytes": -1
    }

    for ref in item:
        tag = ref.tag
        attrib = ref.attrib
        if tag == "{" + torznab_ns + "}attr":
            val = attrib.get("value")
            if "name" in attrib and "value" in attrib and attrib["name"] and val and \
                    attrib["name"] in torznab_elementum_mappings["torznab_attrs"]:
                json = torznab_elementum_mappings["torznab_attrs"][attrib["name"]]
                result[json] = val
            continue

        if ref.tag in torznab_elementum_mappings["tags"] and ref.text is not None:
            json = torznab_elementum_mappings["tags"][ref.tag]
            val = ref.text.strip()

            result[json] = val

    if result["uri"] is None:
        enclosure = item.find('enclosure')
        result["uri"] = (item.findtext('link') or '').strip()
        if not result["uri"] and enclosure is not None:
            result["uri"] = enclosure.get('url', '').strip()

    if not result["name"] or not result["uri"]:
        log.warning("Ignoring Prowlarr item without title or download URI")
        return None

    provider_color = utils.get_provider_color(result["provider"])

    # result["name"] = result["name"].decode("utf-8") # might be needed for non-english items
    result["seeds"] = int(result["seeds"])
    result["peers"] = int(result["peers"])
    resolution = get_resolution(result["name"])
    result["resolution"] = list(utils.resolutions.keys())[::-1].index(resolution)
    result["_resolution"] = resolution
    result["release_type"] = get_release_type(result["name"])
    result["provider"] = f'[COLOR {provider_color}]{result["provider"]}[/COLOR]'

    if result["size"] != "Unknown":
        result["_size_bytes"] = int(result["size"])
        result["size"] = human_size(result["_size_bytes"])

    return result
