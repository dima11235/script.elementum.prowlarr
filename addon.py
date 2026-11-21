"""Kodi entry point and Elementum provider callbacks."""
import os
import sys

import xbmc
import xbmcaddon
import xbmcgui

sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'resources', 'lib'))
from prowlarr import Client, ProwlarrError, search_queries  # noqa: E402

ADDON = xbmcaddon.Addon('script.elementum.prowlarr')

SORT_MODES = ('resolution', 'seeds', 'size', 'balanced')
RESOLUTION_SETTINGS = ((5, 'include_resolution_2160'), (4, 'include_resolution_1440'),
                       (3, 'include_resolution_1080p'), (2, 'include_resolution_720p'),
                       (1, 'include_resolution_480p'), (0, 'include_resolution_unknown'))
RELEASE_SETTINGS = (('bluray', 'include_release_bluray'), ('web', 'include_release_web'),
                    ('hdtv', 'include_release_hdtv'), ('dvd', 'include_release_dvd'),
                    ('tvrip', 'include_release_tvrip'), ('unknown', 'include_release_unknown'),
                    ('screener', 'include_release_screener'))


def localize(identifier):
    return ADDON.getLocalizedString(identifier)


def client():
    return Client(ADDON.getSetting('host'), ADDON.getSetting('api_key'),
                  timeout=ADDON.getSettingInt('timeout'))


def size_range(kind):
    def value(setting):
        try:
            return max(0.0, float(ADDON.getSetting(setting) or 0))
        except (TypeError, ValueError):
            return 0.0
    return (value(f'{kind}_size_min'), value(f'{kind}_size_max'))


def included(mapping):
    included_items = {value for value, setting in mapping if ADDON.getSettingBool(setting)}
    return included_items or None


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
            season=payload.get('season') if isinstance(payload, dict) else None,
            episode=payload.get('episode') if isinstance(payload, dict) else None,
            size_ranges={kind: size_range(kind)} if kind in ('movie', 'season', 'episode') else None,
            include_unknown_size=ADDON.getSettingBool('include_unknown_size'),
            include_resolutions=included(RESOLUTION_SETTINGS),
            include_release_types=included(RELEASE_SETTINGS),
            require_keywords=ADDON.getSetting('require_keywords'),
            exclude_keywords=ADDON.getSetting('exclude_keywords'),
            sort_by=SORT_MODES[max(0, min(3, ADDON.getSettingInt('sort_by')))])
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
