"""Future Sonarr/Radarr integration (design + stubs only).

Nothing in this package is wired into the running application yet. The plan
(see ``README.md`` next to this file) is for the local server to expose:

* a Newznab-compatible indexer at ``/arr/newznab/api`` so Sonarr/Radarr can
  search the 3cat catalogue, and
* a SABnzbd-compatible download-client API at ``/sabnzbd/api`` so they can
  hand the "releases" they picked back to our ``DownloadManager`` and import
  the finished files.

``router.py`` contains the endpoint skeletons (all answer HTTP 501). To enable
the integration later, ``tresdl.server.create_app`` will do
``app.include_router(tresdl.arr.router.router)`` before mounting the static UI.
"""
