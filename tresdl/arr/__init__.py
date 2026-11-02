"""Sonarr/Radarr integration (see ``README.md`` and ``docs/sonarr-radarr.md``).

The local server exposes:

* a Newznab-compatible indexer at ``/arr/newznab/api`` (:mod:`.indexer`,
  :mod:`.releases`), so Sonarr/Radarr can search the 3cat catalogue, and
* a SABnzbd-compatible download-client API at ``/sabnzbd/api`` (and ``/api``)
  (:mod:`.sabnzbd`), so they hand the releases they pick to our
  ``DownloadManager`` and import the finished folder.

:func:`.router.create_arr_router` builds the routes; ``tresdl.server.create_app``
mounts them before the static UI.
"""
