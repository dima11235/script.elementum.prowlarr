import io
import aiohttp
import asyncio
import time
from torf import Torrent, Magnet
from http import HTTPStatus
from urllib.parse import urljoin
from torf import MagnetError

import utils
from logger import log


#  if we didn't get a magnet uri, attempt to resolve the magnet uri.
#  todo for some reason Elementum cannot resolve the link that gets proxied through Prowlarr.
#  So we will resolve it manually for Elementum for now.
#  In actuality, this should be fixed within Elementum
async def uri_to_magnets_uniq_torrents(torrents, p_dialog_cb=None, *, seen=None, limit=None, deadline=None, paused=None):
    if not torrents or limit == 0:
        return []
    results = []
    processed = 0
    hashes = set(seen or ())
    seen_uris = set()
    if paused is None:
        paused = set()
    providers = {}

    async def resolve(item):
        provider = item['provider']
        async with providers.setdefault(provider, asyncio.Lock()):
            if provider in paused:
                return None
            remaining = deadline - time.monotonic() if deadline is not None else 10
            if remaining <= 0:
                return None
            try:
                return await asyncio.wait_for(get_magnet(session, item['uri'], provider, paused), min(10, remaining))
            except asyncio.TimeoutError:
                log.warning(f"Torrent download for {provider}: timed out")
                return None

    async with aiohttp.ClientSession() as session:
        session.headers['User-Agent'] = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_9_3) AppleWebKit/537.36 ' \
                                        '(KHTML, like Gecko) Chrome/35.0.1916.47 Safari/537.36'
        start = 0
        while start < len(torrents):
            if deadline is not None and time.monotonic() >= deadline:
                break
            width = min(4, limit - len(results)) if limit is not None else 4
            batch = []
            for item in torrents[start:start + width]:
                key = item['provider'], item['uri']
                if key not in seen_uris:
                    seen_uris.add(key)
                    batch.append(item)
            start += width
            magnets = await asyncio.gather(*(resolve(item) for item in batch))
            for item, magnet in zip(batch, magnets):
                processed += 1
                if p_dialog_cb:
                    p_dialog_cb(processed, len(torrents), message=utils.translation(32751).format(processed, len(torrents)))
                if not magnet:
                    continue
                try:
                    hash_ = get_info_hash(magnet)
                except MagnetError:
                    continue
                if hash_ and hash_ not in hashes:
                    hashes.add(hash_)
                    results.append(dict(item, uri=magnet))
                    if limit is not None and len(results) >= limit:
                        return results
    return results


async def get_magnet(session, original_uri, provider, paused):
    magnet_prefix = 'magnet:'
    uri = original_uri
    start = time.monotonic()
    for redirect_count in range(6):
        if len(uri) >= len(magnet_prefix) and uri[0:7] == magnet_prefix:
            response_time = time.monotonic() - start
            log.debug(f"{response_time:.3f}s redirect-{redirect_count} magnet")
            return uri
        try:
            async with session.get(uri, allow_redirects=False, timeout=10) as resp:
                if resp.status == HTTPStatus.OK and resp.content_type == 'application/x-bittorrent':
                    data = await resp.read()
                    torrent = Torrent.read_stream(io.BytesIO(data))
                    magnet = str(torrent.magnet())
                    response_time = time.monotonic() - start
                    log.debug(f"{response_time:.3f}s magnet")
                    return magnet
                elif resp.status in (301, 302, 303, 307, 308):
                    uri = urljoin(uri, resp.headers['Location'])
                elif resp.status == HTTPStatus.TOO_MANY_REQUESTS:
                    retry_after = resp.headers.get('Retry-After', '')
                    log.warning(f"Torrent download for {provider}: HTTP 429, Retry-After="
                                f"{int(retry_after) if retry_after.isdecimal() else None}")
                    paused.add(provider)
                    break
                else:
                    log.warning(f"Torrent download for {provider}: HTTP {resp.status}")
                    break
        except Exception as error:
            log.warning(f"Torrent download for {provider}: {type(error).__name__}")
            break
    return None


def get_info_hash(magnet):
    return Magnet.from_string(magnet).infohash
