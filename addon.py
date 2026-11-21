"""Kodi entry point and Elementum provider callbacks."""
import os
import sys

import xbmc
import xbmcaddon
import xbmcgui

sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'resources', 'lib'))
from prowlarr import Client, ProwlarrError, search_queries  # noqa: E402

ADDON = xbmcaddon.Addon('script.elementum.prowlarr')


def localize(identifier):
    return ADDON.getLocalizedString(identifier)


def client():
    return Client(ADDON.getSetting('host'), ADDON.getSetting('api_key'),
                  timeout=ADDON.getSettingInt('timeout'))


def search(payload, kind='search'):
    try:
        language = xbmc.getLanguage(xbmc.ISO_639_1) if ADDON.getSettingBool('localized_titles') else ''
        queries = search_queries(payload, kind, language,
                                 season_fallback=ADDON.getSettingBool('season_fallback'))
        if not queries:
            return []
        results = client().search(
            queries, kind=kind, limit=ADDON.getSettingInt('max_results'),
            min_seeders=ADDON.getSettingInt('min_seeders'),
            max_size_gb=float(ADDON.getSetting('max_size_gb') or 0),
            require_keywords=ADDON.getSetting('require_keywords'),
            exclude_keywords=ADDON.getSetting('exclude_keywords'))
        xbmc.log(f'Elementum Prowlarr: returned {len(results)} results', xbmc.LOGINFO)
        return results
    except ProwlarrError as error:
        xbmc.log(f'Elementum Prowlarr: {error}', xbmc.LOGWARNING)
        xbmcgui.Dialog().notification('Elementum Prowlarr', str(error), time=6000)
    except Exception as error:
        # Never log payloads, API keys, download URLs or raw exception messages.
        xbmc.log(f'Elementum Prowlarr: {type(error).__name__}', xbmc.LOGERROR)
        xbmcgui.Dialog().notification('Elementum Prowlarr', localize(32021), time=6000)
    return []


def main():
    if len(sys.argv) == 1:
        ADDON.openSettings()
    elif sys.argv[1] == 'test':
        try:
            indexers = client().indexers()
            xbmcgui.Dialog().ok('Elementum Prowlarr', localize(32020).format(len(indexers)))
        except ProwlarrError as error:
            xbmcgui.Dialog().ok('Elementum Prowlarr', str(error))
    else:
        from elementum.provider import register
        register(lambda payload: search(payload), lambda payload: search(payload, 'movie'),
                 lambda payload: search(payload, 'episode'), lambda payload: search(payload, 'season'))


if __name__ == '__main__':
    main()
