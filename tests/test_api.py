"""Tests for tresdl.api.

Offline tests use ``httpx.MockTransport`` with payloads shaped like the real
3cat answers (probed 2026-11-02). Tests marked ``live`` hit the real API.
"""
from __future__ import annotations

import json
from typing import Any, Callable

import httpx
import pytest

from tresdl import api as api_mod
from tresdl.api import (
    ThreeCatClient,
    ThreeCatError,
    ThreeCatNotFound,
    as_list,
    classify_source,
    clean_episode_title,
    clean_text,
    normalize_for_search,
    parse_date,
    parse_duration,
    parse_mpd,
    parse_season_number,
    parse_url,
    pick_image,
    rewrite_cdn_url,
)


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #

def test_as_list() -> None:
    assert as_list(None) == []
    assert as_list({"a": 1}) == [{"a": 1}]
    assert as_list([1, 2]) == [1, 2]


def test_clean_text_entities_tags_whitespace() -> None:
    assert clean_text("  Tin&amp;Tan  i   <b>amics</b>\n") == "Tin&Tan i amics"
    assert clean_text("Merl&iacute;") == "Merlí"
    assert clean_text("&amp;eacute;s") == "és"
    assert clean_text(None) == ""


@pytest.mark.parametrize("raw,expected", [
    ("T1xC1 - Els peripatètics", ("Els peripatètics", 1, 1)),
    ("T2xC13 - Boeci", ("Boeci", 2, 13)),
    ("t3 x c4: Karl Marx", ("Karl Marx", 3, 4)),
    ("T1xC1", ("Capítol 1", 1, 1)),
    ("Capítol 5 - La tornada", ("La tornada", None, 5)),
    ("Capitol 7", ("Capítol 7", None, 7)),
    ("Telenotícies migdia - 24/01/2011", ("Telenotícies migdia - 24/01/2011", None, None)),
    ("  El&nbsp;que el dia deu a la nit ", ("El que el dia deu a la nit", None, None)),
])
def test_clean_episode_title(raw: str, expected: tuple) -> None:
    assert clean_episode_title(raw) == expected


def test_parse_season_number() -> None:
    assert parse_season_number("PUTEMP_2", "2a Temporada") == 2
    assert parse_season_number("", "3a Temporada") == 3
    assert parse_season_number(None, "Temporada 4") == 4
    assert parse_season_number("PUTEMP_11") == 11
    assert parse_season_number("", "") is None


def test_parse_date() -> None:
    assert parse_date("14/09/2015 21:55:08") == "2015-09-14"
    assert parse_date("1/2/2010") == "2010-02-01"
    assert parse_date("2015-09-14T21:55:08+02:00") == "2015-09-14"
    assert parse_date({"text": "21/07/2024 15:22", "utc": "2024-07-21T15:22:35+02:00"}) == "2024-07-21"
    assert parse_date("31/02/2015") is None
    assert parse_date(None) is None
    assert parse_date("ahir") is None


def test_parse_duration() -> None:
    assert parse_duration("00:54:00:23") == 3240
    assert parse_duration("01:38:16") == 5896
    assert parse_duration("02:05") == 125
    assert parse_duration({"text": "00:01:46", "milisegons": 106000}) == 106
    assert parse_duration({"text": "00:01:46"}) == 106
    assert parse_duration(None) is None
    assert parse_duration("x") is None


def test_pick_image_prefers_169_and_largest() -> None:
    images = [
        {"text": "logo", "mida": "MASTER", "rel_name": "IMG_LOGO"},
        {"text": "small169", "mida": "326x184", "rel_name": "IMAGE"},
        {"text": "big169", "mida": "1014x570", "rel_name": "KEYVIDEO"},
        {"text": "portrait", "mida": "288x422", "rel_name": "IMAGE"},
        {"text": "banner", "mida": "1600X284", "rel_name": "IMAGE"},
    ]
    assert pick_image(images) == "big169"
    # Explicit 16:9 slot (MASTER) beats sized 16:9 thumbnails.
    assert pick_image(images + [{"text": "master169", "mida": "MASTER", "rel_name": "imatge_169"}]) == "master169"
    # Single object instead of list.
    assert pick_image({"text": "only", "mida": "670x378"}) == "only"
    # media.jsp dict shape.
    assert pick_image({"amplada": 670, "alcada": 378, "url": "https://x/y.jpg"}) == "https://x/y.jpg"
    # Only logos -> still returns something.
    assert pick_image([{"text": "logo", "mida": "MASTER", "rel_name": "IMG_LOGO"}]) == "logo"
    assert pick_image(None) is None
    assert pick_image([]) is None


def test_rewrite_cdn_url() -> None:
    old = "https://mp4-high-es.ccma.cat.s3.eu-west-1.amazonaws.com/MP4_ALTA_IPTV_ES/7/9/1683946186997.mp4"
    assert rewrite_cdn_url(old) == "https://mp4-down-high-es.3catvideos.cat/7/9/1683946186997.mp4"
    old = "http://mp4-high-int.ccma.cat.s3.eu-west-1.amazonaws.com/MP4_ALTA_IPTV_MON/1/2/3.mp4"
    assert rewrite_cdn_url(old) == "https://mp4-down-high-int.3catvideos.cat/1/2/3.mp4"
    old = "https://mp4-medium-es.ccma.cat.s3.eu-west-1.amazonaws.com/MP4_MITJA_WEB_ES/1/2/3.mp4"
    assert rewrite_cdn_url(old) == "https://mp4-down-medium-es.3catvideos.cat/1/2/3.mp4"
    old = "https://mp4-medium-int.ccma.cat.s3.eu-west-1.amazonaws.com/MP4_MITJA_WEB_MON/1/2/3.mp4"
    assert rewrite_cdn_url(old) == "https://mp4-down-medium-int.3catvideos.cat/1/2/3.mp4"
    current = "https://mp4-down-high-es.3catvideos.cat/7/9/1.mp4"
    assert rewrite_cdn_url(current) == current


def test_classify_source() -> None:
    dash = classify_source("https://adaptive-int.3catvideos.cat/7/4/1/2/stream.mpd", "DASH")
    assert (dash.kind, dash.height, dash.label) == ("dash", None, "DASH")
    mp4 = classify_source("https://mp4-down-high-es.3catvideos.cat/7/9/1.mp4", "720p")
    assert (mp4.kind, mp4.height) == ("mp4", 720)
    assert classify_source("https://x/a.mp4", "576p").height == 576
    assert classify_source("https://x/master.m3u8", "").kind == "hls"
    assert classify_source("https://x/a.mp4", "").height is None


def test_parse_mpd() -> None:
    mpd = (
        '<MPD xmlns="urn:mpeg:dash:schema:mpd:2011"><Period><AdaptationSet mimeType="video/mp4">'
        '<Representation width="1920" height="1080"/><Representation width="1280" height="720"/>'
        '</AdaptationSet></Period></MPD>'
    )
    info = parse_mpd(mpd)
    assert info.max_height == 1080 and info.protected is False
    drm = mpd.replace("<AdaptationSet", '<AdaptationSet><ContentProtection schemeIdUri="urn:mpeg:dash:mp4protection:2011"/>', 1)
    assert parse_mpd(drm).protected is True
    assert parse_mpd("<MPD/>").max_height is None


def test_normalize_for_search() -> None:
    assert normalize_for_search("Merlí") == "merli"
    assert normalize_for_search("  Telenotícies!! ") == "telenoticies"
    assert normalize_for_search("Pel·lícula") == "pellicula"


# --------------------------------------------------------------------------- #
# URL parsing (no network)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("text,video_id,first_slug", [
    ("https://www.3cat.cat/3cat/merli/", None, "merli"),
    ("https://www.3cat.cat/3cat/merli", None, "merli"),
    ("https://www.3cat.cat/3cat/merli/capitols/", None, "merli"),
    ("https://www.3cat.cat/tv3/polseres-vermelles/", None, "polseres-vermelles"),
    ("https://www.3cat.cat/tv3/sx3/bola-de-drac/", None, "bola-de-drac"),
    ("www.3cat.cat/3cat/dawson-creix/", None, "dawson-creix"),
    ("3cat.cat/3cat/merli/", None, "merli"),
    ("https://www.3cat.cat/3cat/merli-els-peripatetics-capitol-1/video/5549976/", 5549976, None),
    ("https://www.3cat.cat/tv3/sx3/panic/video/6432755/", 6432755, None),
    ("https://www.3cat.cat/3cat/el-festival/video/6293447/?ext=SM_WP", 6293447, None),
    ("https://www.ccma.cat/tv3/alacarta/merli/", None, "merli"),
    ("http://www.ccma.cat/tv3/alacarta/merli/els-peripatetics/video/5549976/", 5549976, None),
    ("https://ccma.cat/tv3/alacarta/polseres-vermelles/capitols/", None, "polseres-vermelles"),
])
def test_parse_url_forms(text: str, video_id: int | None, first_slug: str | None) -> None:
    parsed = parse_url(text)
    assert parsed is not None
    assert parsed.video_id == video_id
    if first_slug is not None:
        assert parsed.slugs[0] == first_slug


@pytest.mark.parametrize("text", ["merli", "54076", "dawson creix", "Polseres vermelles", "", "  "])
def test_parse_url_not_a_url(text: str) -> None:
    assert parse_url(text) is None


def test_parse_url_foreign_site() -> None:
    with pytest.raises(ThreeCatError):
        parse_url("https://www.youtube.com/watch?v=abc")


# --------------------------------------------------------------------------- #
# Client with a mocked transport
# --------------------------------------------------------------------------- #

def _wrap(resposta: dict) -> dict:
    return {"resposta": {"status": "OK", **resposta}}


def _episode_item(vid: int, titol: str, cap: int, cap_t: int, season: int | None,
                  date: str = "14/09/2015 21:55:08") -> dict:
    item: dict[str, Any] = {
        "id": vid, "titol": titol, "capitol": cap, "capitol_temporada": cap_t,
        "data_emissio": date, "durada": "00:54:00:23", "entradeta": "Desc &amp; més",
        "programa": "Merlí",
        "imatges": [{"text": f"https://img/{vid}.jpg", "mida": "1014x570", "rel_name": "KEYVIDEO"}],
    }
    if season is not None:
        # Single object, not a list: must be normalised.
        item["temporades"] = {"id": f"PUTEMP_{season}", "desc": f"{season}a Temporada", "main": True}
    return item


MERLI_EPISODES = [
    _episode_item(5619402, "T2xC1 - Els Presocràtics", 14, 1, 2, "19/09/2016 22:47:35"),
    _episode_item(5549976, "T1xC1 - Els peripatètics", 1, 1, 1),
    _episode_item(5552234, "T1xC2 - Plató", 2, 2, 1, "21/09/2015 21:59:30"),
    _episode_item(5620147, "T2xC2 - Thomas Hobbes", 15, 2, 2, "26/09/2016 22:49:53"),
    _episode_item(5552854, "T1xC3 - Maquiavel", 3, 3, 1, "28/09/2015 21:55:20"),
]

CATALOGUE = _wrap({"items": {"num": 3, "lletra": [
    {"valor": "D", "item": {"id": 3605, "nombonic": "dawson-creix", "titol": "Dawson creix",
                            "entradeta": "Sèrie", "tipologia": "PTVC_PROGRAMA"}},
    {"valor": "M", "item": [
        {"id": 54076, "nombonic": "merli", "titol": " Merl&iacute; ", "entradeta": "Filosofia",
         "info_distribucio": "Tres temporades disponibles",
         "imatges": [{"text": "https://img/merli169.jpg", "mida": "MASTER", "rel_name": "IMATGE_169"}]},
        {"id": 56636, "nombonic": "merlinari", "titol": "Merlinari", "entradeta": ""},
    ]},
    {"valor": "P", "item": [{"id": 46356, "nombonic": "polseres-vermelles", "titol": "Polseres vermelles",
                             "entradeta": "Hospital"}]},
]}})

MEDIA_OK = {
    "informacio": {
        "estat": {"actiu": True, "id": 0}, "id": 5549976, "programa_id": 54076, "capitol": 1,
        "titol": "T1xC1 - Els peripatètics", "programa": "Merlí",
        "durada": {"text": "00:54:00", "milisegons": 3240000},
        "data_emissio": {"text": "14/09/2015 21:55", "utc": "2015-09-14T21:55:08+02:00"},
        "temporada": {"idName": "PUTEMP_1", "name": "1a Temporada", "id": 50954},
        "descripcio": "Arriba el Merlí",
    },
    "media": {"geo": "ESPANYA", "format": "MP4", "url": [
        {"file": "https://mp4-high-es.ccma.cat.s3.eu-west-1.amazonaws.com/MP4_ALTA_IPTV_ES/7/9/1.mp4",
         "label": "576p", "active": False},
        {"file": "https://ott-vod.3catvideos.cat/6/7/5549976/1/stream.mpd", "label": "DASH", "active": True},
    ]},
    "subtitols": {"text": "Català (SPS)", "iso": "ca", "url": "https://statics.3cat.cat/a.vtt", "format": "vtt"},
    "variants": [{"id": "AUD", "nom": "Audiodescripció", "media": {"geo": "TOTS", "url": [
        {"file": "https://mp4-down-high-es.3catvideos.cat/0/5/2.mp4", "label": "576p", "active": False}]}}],
    "imatges": {"amplada": 670, "alcada": 378, "url": "https://img/key.jpg"},
}

MEDIA_FILM = {
    "informacio": {
        "estat": {"actiu": True}, "id": 6431444, "programa_id": None, "capitol": 1,
        "titol": "El que el dia deu a la nit", "programa": "Pel·lícula",
        "durada": {"text": "02:35:28", "milisegons": 9328000},
        "data_emissio": {"text": "01/01/2016 15:37"}, "temporada": None,
    },
    "media": {"geo": "ESPANYA", "url": {"file": "https://adaptive-es.3catvideos.cat/4/4/6431444/9/stream.mpd",
                                        "label": "DASH", "active": True}},
}

MEDIA_INACTIVE = {
    "informacio": {"estat": {"actiu": False, "id": 2}, "id": 1111, "titol": "Caducat"},
    "media": {"geo": "TOTS", "url": []},
}

MPD_PLAIN = '<MPD><Representation height="1080"/><Representation height="720"/></MPD>'
MPD_DRM = '<MPD><ContentProtection schemeIdUri="x"/><Representation height="1080"/></MPD>'


class FakeApi:
    """Routes mocked requests and records them."""

    def __init__(self) -> None:
        self.calls: list[httpx.Request] = []
        self.fail_search = False
        self.flaky: dict[str, int] = {}          # path -> number of 503s to emit first
        self.mpd = MPD_PLAIN
        self.episodes = list(MERLI_EPISODES)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        path = request.url.path
        q = request.url.params
        if self.flaky.get(path, 0) > 0:
            self.flaky[path] -= 1
            return httpx.Response(503, text="busy")
        if request.url.host == "api.3cat.cat":
            if path == "/programestv":
                return httpx.Response(200, json=CATALOGUE)
            if path.startswith("/programestv/"):
                pid = int(path.strip("/").split("/")[1])
                for letter in CATALOGUE["resposta"]["items"]["lletra"]:
                    for item in as_list(letter["item"]):
                        if item["id"] == pid:
                            return httpx.Response(200, json=_wrap({"item": item}))
                return httpx.Response(404, json={"error": {"code": 404, "message": "Client error: 404"}})
            if path == "/cercador/tot":
                if self.fail_search:
                    return httpx.Response(500, text="boom")
                hits = [{"id": 66876, "nombonic": "mags", "titol": "Mags: contes d'Arcàdia"},
                        {"id": 54076, "nombonic": "merli", "titol": "Merlí"}]
                text = q.get("text", "").lower()
                items = [h for h in hits if "merl" in text]
                return httpx.Response(200, json=_wrap({"items": {"num": len(items), "item": items}}))
            if path == "/videos":
                eps = self.episodes
                if q.get("temporada"):
                    eps = [e for e in eps if as_list(e.get("temporades"))
                           and as_list(e["temporades"])[0]["id"] == q["temporada"]]
                size = int(q.get("items_pagina", 1000))
                page = int(q.get("pagina", 1))
                chunk = eps[(page - 1) * size: page * size]
                total_pages = (len(eps) + size - 1) // size
                # The real API returns a bare object when a page has one item.
                payload: Any = chunk[0] if len(chunk) == 1 else chunk
                return httpx.Response(200, json=_wrap({
                    "items": {"num": len(chunk), "item": payload},
                    "paginacio": {"total_items": len(eps), "items_pagina": size,
                                  "pagina_actual": page, "total_pagines": total_pages}}))
        if request.url.host == "api-media.3cat.cat":
            vid = int(q.get("idint", "0"))
            data = {5549976: MEDIA_OK, 6431444: MEDIA_FILM, 1111: MEDIA_INACTIVE}.get(vid)
            if data is None:
                return httpx.Response(404, json={"code": 404, "text": "S'ha produït un error"})
            return httpx.Response(200, content=json.dumps(data).encode("utf-8"),
                                  headers={"content-type": "application/json; charset=utf-8"})
        if path.endswith(".mpd"):
            return httpx.Response(200, text=self.mpd, headers={"content-type": "application/dash+xml"})
        return httpx.Response(404)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeApi:
    monkeypatch.setattr(api_mod.time, "sleep", lambda _s: None)
    return FakeApi()


@pytest.fixture
def client(fake: FakeApi) -> ThreeCatClient:
    return ThreeCatClient(http=httpx.Client(transport=httpx.MockTransport(fake)))


def test_catalogue_normalises_and_caches(client: ThreeCatClient, fake: FakeApi) -> None:
    programs = client.catalogue()
    assert [p.id for p in programs] == [3605, 54076, 56636, 46356]
    merli = programs[1]
    assert merli.title == "Merlí" and merli.slug == "merli"
    assert merli.image == "https://img/merli169.jpg"
    assert merli.info == "Tres temporades disponibles"
    client.catalogue()
    assert sum(1 for c in fake.calls if c.url.path == "/programestv") == 1
    client.catalogue(force=True)
    assert sum(1 for c in fake.calls if c.url.path == "/programestv") == 2


def test_catalogue_ttl_expiry(client: ThreeCatClient, fake: FakeApi, monkeypatch: pytest.MonkeyPatch) -> None:
    client.catalogue()
    monkeypatch.setattr(api_mod, "CATALOGUE_TTL", -1.0)
    client.catalogue()
    assert sum(1 for c in fake.calls if c.url.path == "/programestv") == 2


def test_program_by_slug(client: ThreeCatClient) -> None:
    program = client.program_by_slug("Merli")
    assert program is not None and program.id == 54076
    assert client.program_by_slug("no-existeix") is None


def test_get_program_and_not_found(client: ThreeCatClient) -> None:
    assert client.get_program(46356).title == "Polseres vermelles"
    with pytest.raises(ThreeCatNotFound):
        client.get_program(99999999)


def test_search_ranks_exact_title_first(client: ThreeCatClient) -> None:
    results = client.search("merli")
    assert results[0].id == 54076
    assert 56636 in [p.id for p in results]          # catalogue fuzzy match merged in


def test_search_fallback_fuzzy_accent_insensitive(client: ThreeCatClient, fake: FakeApi) -> None:
    fake.fail_search = True
    assert client.search("MERLÍ")[0].id == 54076
    assert client.search("polseres vermelas")[0].id == 46356   # typo
    assert client.search("dawson")[0].id == 3605
    assert client.search("") == []


def test_list_episodes_paginates_sorts_and_parses(client: ThreeCatClient, fake: FakeApi) -> None:
    client.page_size = 2                                  # 5 items -> 3 pages, last page is an object
    episodes = client.list_episodes(54076)
    assert [e.id for e in episodes] == [5549976, 5552234, 5552854, 5619402, 5620147]
    pages = [c.url.params["pagina"] for c in fake.calls if c.url.path == "/videos"]
    assert pages == ["1", "2", "3"]
    assert all(c.url.params["tipus_contingut"] == "PPD" for c in fake.calls if c.url.path == "/videos")
    first = episodes[0]
    assert first.title == "Els peripatètics" and first.raw_title == "T1xC1 - Els peripatètics"
    assert (first.season_id, first.season_number, first.episode_number, first.absolute_number) == ("PUTEMP_1", 1, 1, 1)
    assert first.air_date == "2015-09-14" and first.duration_seconds == 3240
    assert first.description == "Desc & més"
    assert first.program_title == "Merlí" and first.program_id == 54076
    assert first.image == "https://img/5549976.jpg"


def test_list_episodes_season_filter_and_clips(client: ThreeCatClient, fake: FakeApi) -> None:
    episodes = client.list_episodes(54076, season_id="PUTEMP_2", full_episodes_only=False)
    assert [e.episode_number for e in episodes] == [1, 2]
    call = [c for c in fake.calls if c.url.path == "/videos"][-1]
    # Filtered locally: episodes whose season is only in the title must be included.
    assert "temporada" not in call.url.params
    assert "tipus_contingut" not in call.url.params


def test_list_seasons(client: ThreeCatClient) -> None:
    seasons = client.list_seasons(client.list_episodes(54076))
    assert [(s.id, s.number, s.title, s.episode_count) for s in seasons] == [
        ("PUTEMP_1", 1, "1a Temporada", 3), ("PUTEMP_2", 2, "2a Temporada", 2)]


def test_programme_without_seasons(client: ThreeCatClient, fake: FakeApi) -> None:
    fake.episodes = [
        {"id": 2, "titol": "Telenotícies vespre - 11/02/2011", "capitol": 3160, "capitol_temporada": -1,
         "data_emissio": "11/02/2011 21:00:00", "durada": "00:45:32:18", "programa": "Telenotícies"},
        {"id": 1, "titol": "Telenotícies migdia - 24/01/2011", "capitol": 3146, "capitol_temporada": -1,
         "data_emissio": "24/01/2011 14:30:00", "durada": "00:59:59:19", "programa": "Telenotícies"},
        {"id": 3, "titol": "Sense data"},                 # almost everything missing
    ]
    episodes = client.list_episodes(136938435)
    assert [e.id for e in episodes] == [1, 2, 3]
    assert episodes[0].season_number is None and episodes[0].episode_number is None
    assert episodes[0].absolute_number == 3146 and episodes[0].season_id == ""
    assert episodes[2].air_date is None and episodes[2].duration_seconds is None
    seasons = client.list_seasons(episodes)
    assert len(seasons) == 1 and seasons[0].id == "" and seasons[0].number is None
    assert seasons[0].episode_count == 3


def test_get_media(client: ThreeCatClient) -> None:
    media = client.get_media(5549976)
    assert media.available is True and media.geo == "ESPANYA"
    assert [s.kind for s in media.sources] == ["dash", "mp4"]       # DASH first
    assert media.sources[0].height is None
    assert media.sources[1].url == "https://mp4-down-high-es.3catvideos.cat/7/9/1.mp4"  # CDN rewritten
    assert media.sources[1].height == 576
    assert media.subtitles[0].lang == "ca" and media.subtitles[0].format == "vtt"
    assert len(media.audio_description) == 1 and media.audio_description[0].height == 576


def test_get_media_parse_dash(client: ThreeCatClient, fake: FakeApi) -> None:
    assert client.get_media(5549976, parse_dash=True).sources[0].height == 1080
    fake.mpd = MPD_DRM
    media = client.get_media(5549976, parse_dash=True)
    assert [s.kind for s in media.sources] == ["mp4"]                # DRM DASH dropped
    with pytest.raises(ThreeCatError):
        client.get_media(6431444, parse_dash=True)                    # only a DRM stream


def test_get_media_inactive_and_missing(client: ThreeCatClient) -> None:
    media = client.get_media(1111)
    assert media.available is False and media.sources == []
    with pytest.raises(ThreeCatNotFound):
        client.get_media(424242)


def test_media_is_cached(client: ThreeCatClient, fake: FakeApi) -> None:
    client.get_episode(5549976)
    client.get_media(5549976)
    assert sum(1 for c in fake.calls if c.url.host == "api-media.3cat.cat") == 1


def test_get_episode(client: ThreeCatClient) -> None:
    ep = client.get_episode(5549976)
    assert (ep.program_id, ep.program_title, ep.title) == (54076, "Merlí", "Els peripatètics")
    assert (ep.season_id, ep.season_number, ep.episode_number) == ("PUTEMP_1", 1, 1)
    assert ep.air_date == "2015-09-14" and ep.duration_seconds == 3240
    assert ep.image == "https://img/key.jpg"
    film = client.get_episode(6431444)
    assert film.program_id == 0 and film.program_title == "El que el dia deu a la nit"
    assert film.season_number is None and film.episode_number is None


def test_retries_transient_errors(client: ThreeCatClient, fake: FakeApi) -> None:
    fake.flaky["/programestv"] = 2
    assert len(client.catalogue()) == 4
    fake.flaky["/programestv/54076/"] = 3
    with pytest.raises(ThreeCatError) as err:
        client.get_program(54076)
    assert err.value.status == 503


def test_retries_network_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api_mod.time, "sleep", lambda _s: None)
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        raise httpx.ConnectError("down", request=request)

    client = ThreeCatClient(http=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(ThreeCatError):
        client.get_program(1)
    assert attempts["n"] == 3


def test_default_client_user_agent() -> None:
    client = ThreeCatClient()
    try:
        assert "Mozilla/5.0" in client._http.headers["User-Agent"]
    finally:
        client.close()


@pytest.mark.parametrize("text,kind,check", [
    ("https://www.3cat.cat/3cat/merli/", "program", lambda r: r["program"].id == 54076),
    ("https://www.3cat.cat/3cat/merli/capitols/", "program", lambda r: r["program"].id == 54076),
    ("https://www.ccma.cat/tv3/alacarta/polseres-vermelles/", "program", lambda r: r["program"].id == 46356),
    ("https://www.3cat.cat/3cat/merli-els-peripatetics-capitol-1/video/5549976/", "video",
     lambda r: r["episode"].id == 5549976 and r["program"].id == 54076),
    ("https://www.3cat.cat/3cat/film/video/6431444/", "video",
     lambda r: r["episode"].id == 6431444 and r["program"] is None),
    ("5549976", "video", lambda r: r["episode"].id == 5549976),
    ("54076", "program", lambda r: r["program"].id == 54076),           # not a video id
    ("dawson-creix", "program", lambda r: r["program"].id == 3605),
    ("merli", "search", lambda r: r["results"][0].id == 54076),
    ("https://www.3cat.cat/3cat/merl/", "search", lambda r: r["results"][0].id == 54076),
    ("", "search", lambda r: r["results"] == []),
])
def test_resolve(client: ThreeCatClient, text: str, kind: str, check: Callable[[dict], bool]) -> None:
    result = client.resolve(text)
    assert result["type"] == kind
    assert check(result)


def test_resolve_foreign_url(client: ThreeCatClient) -> None:
    with pytest.raises(ThreeCatError):
        client.resolve("https://example.com/merli/")


# --------------------------------------------------------------------------- #
# Regression tests (adversarial review fixes)
# --------------------------------------------------------------------------- #

def test_series_title_comes_from_programme_not_items(client: ThreeCatClient, fake: FakeApi) -> None:
    items = [_episode_item(9001, "T1xC1 - U", 1, 1, 1), _episode_item(9002, "T1xC2 - Dos", 2, 2, 1)]
    items[0]["programa"] = "Pel·lícula"                 # noisy per-item field
    items[1]["programa"] = "Premis esportius"
    fake.episodes = items
    episodes = client.list_episodes(54076)
    assert {e.program_title for e in episodes} == {"Merlí"}


def test_series_title_falls_back_to_item_when_programme_lookup_fails(client: ThreeCatClient,
                                                                    fake: FakeApi) -> None:
    item = _episode_item(9001, "T1xC1 - U", 1, 1, 1)
    item["programa"] = "Sèrie perduda"
    fake.episodes = [item]
    episodes = client.list_episodes(77777777)          # unknown programme: 404 + not in catalogue
    assert episodes[0].program_title == "Sèrie perduda"


def test_series_title_prefers_matching_programes_tv_entry(client: ThreeCatClient, fake: FakeApi) -> None:
    item = _episode_item(9001, "Capítol 1", 1, 1, None)
    item["programa"] = "Doraemon"
    item["programes_tv"] = [{"id": "999", "titol": "Altres"},
                            {"id": "77777777", "titol": "Doraemon, el gat còsmic"}]
    fake.episodes = [item]
    assert client.list_episodes(77777777)[0].program_title == "Doraemon, el gat còsmic"


def test_seasons_inferred_from_title_join_the_real_season(client: ThreeCatClient, fake: FakeApi) -> None:
    tagged = [_episode_item(100 + n, f"T7xC{n} - Tagged {n}", n, n, 7) for n in (1, 2)]
    inferred = [_episode_item(200 + n, f"T7xC{n} - Inferred {n}", 50 + n, n, None) for n in (3, 4)]
    other = [_episode_item(300, "T2xC1 - Season two", 60, 1, None)]
    extras = [_episode_item(400, "Especial infantils", 70, -1, None)]
    fake.episodes = tagged + inferred + other + extras
    episodes = client.list_episodes(54076)
    seasons = client.list_seasons(episodes)
    assert [(s.id, s.number, s.episode_count) for s in seasons] == [
        ("PUTEMP_2", 2, 1), ("PUTEMP_7", 7, 4), ("", 0, 1)]
    assert seasons[-1].title == "Especials i extres"
    numbers = [s.number for s in seasons if s.number is not None]
    assert len(numbers) == len(set(numbers))
    # A season filter includes the episodes whose season is only in the title.
    assert [e.id for e in client.list_episodes(54076, season_id="PUTEMP_7")] == [101, 102, 203, 204]


def test_extras_of_a_seasonal_programme_are_specials(client: ThreeCatClient, fake: FakeApi) -> None:
    from tresdl import naming

    fake.episodes = [_episode_item(1, "T1xC1 - Primer", 1, 1, 1),
                     _episode_item(2, "Un Vasco en Polonia", 1, -1, None, "19/12/2017 22:00:00")]
    real, special = sorted(client.list_episodes(54076), key=lambda e: e.id)
    assert real.season_number == 1 and special.season_number == 0
    p_real = naming.episode_path("C:/out", real)
    p_special = naming.episode_path("C:/out", special)
    assert "S01E01" in p_real and "S01E01" not in p_special
    assert p_special.replace("\\", "/").endswith(
        "Merlí/Specials/Merlí - 2017-12-19 - Un Vasco en Polonia.mp4")


def test_programme_without_any_season_keeps_absolute_numbering(client: ThreeCatClient,
                                                               fake: FakeApi) -> None:
    from tresdl import naming

    fake.episodes = [{"id": 2, "titol": "Telenotícies vespre", "capitol": 3160, "capitol_temporada": -1,
                      "data_emissio": "11/02/2011 21:00:00", "programa": "Telenotícies"}]
    ep = client.list_episodes(136938435)[0]
    assert ep.season_number is None
    assert naming.episode_path("C:/out", ep).endswith("S01E3160 - Telenotícies vespre.mp4")


def test_other_language_copies_are_left_out(client: ThreeCatClient, fake: FakeApi) -> None:
    ca = _episode_item(1, "T1xC1 - Lleó", 1, 1, 1)
    ca["idiomes"] = [{"id": "PU_CATALA", "desc": "Català", "main": True}]
    en = _episode_item(2, "T1xC1 - Lion", 1, 1, 1)
    en["idiomes"] = {"id": "PU_ANGLES", "desc": "Anglès", "main": True}
    unknown = _episode_item(3, "T1xC2 - Sense idioma", 2, 2, 1)
    fake.episodes = [ca, en, unknown]
    assert [e.id for e in client.list_episodes(54076)] == [1, 3]
    assert {e.id for e in client.list_episodes(54076, languages=None)} == {1, 2, 3}
    # A programme only published in another language is still listed.
    fake.episodes = [en]
    assert [e.id for e in client.list_episodes(54076)] == [2]


def test_title_number_wins_over_capitol_temporada(client: ThreeCatClient, fake: FakeApi,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    item = _episode_item(5549976, "T2xC48 - Connie i el pomer", 100, 100, 2)
    plain = _episode_item(7, "Sense prefix", 30, 5, 1)
    fake.episodes = [item, plain]
    by_id = {e.id: e for e in client.list_episodes(54076)}
    assert (by_id[5549976].season_number, by_id[5549976].episode_number) == (2, 48)
    assert by_id[7].episode_number == 5
    # The single-video route numbers it the same way.
    info = dict(MEDIA_OK["informacio"], titol="T2xC48 - Connie i el pomer", capitol_temporada=100,
                temporada={"idName": "PUTEMP_2", "name": "2a Temporada"})
    monkeypatch.setitem(MEDIA_OK, "informacio", info)
    ep = client.get_episode(5549976)
    assert (ep.season_number, ep.episode_number) == (2, 48)


def _media_client(fake: FakeApi, vid: int, info_changes: dict) -> ThreeCatClient:
    media = json.loads(json.dumps(MEDIA_OK))
    media["informacio"].update({"id": vid, **info_changes})

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api-media.3cat.cat" and request.url.params.get("idint") == str(vid):
            return httpx.Response(200, json=media)
        return fake(request)

    return ThreeCatClient(http=httpx.Client(transport=httpx.MockTransport(handler)))


def test_get_episode_recovers_programme_without_programa_id(fake: FakeApi) -> None:
    c = _media_client(fake, 5622292, {"programa_id": None, "programa": "Merl&iacute;",
                                      "titol": "T2xC15", "temporada": None})
    ep = c.get_episode(5622292)
    assert (ep.program_id, ep.program_title, ep.title) == (54076, "Merlí", "Capítol 15")
    assert (ep.season_number, ep.episode_number) == (2, 15)
    # Not in the catalogue but with a season marker: the name is still the series.
    c = _media_client(fake, 5622292, {"programa_id": None, "programa": "Nissaga: l'herència",
                                      "titol": "T2xC15"})
    ep = c.get_episode(5622292)
    assert (ep.program_id, ep.program_title, ep.season_id) == (0, "Nissaga: l'herència", "PUTEMP_1")


def test_get_episode_and_listing_agree_on_series_title(fake: FakeApi) -> None:
    item = _episode_item(5549976, "T1xC1 - Els peripatètics", 1, 1, 1)
    item["programa"] = "MERLI (sèrie)"
    fake.episodes = [item]
    c = _media_client(fake, 5549976, {"programa": "Merlí, la sèrie"})
    listed = c.list_episodes(54076)[0]
    assert listed.program_title == c.get_episode(5549976).program_title == "Merlí"


@pytest.mark.live
def test_live_series_titles_are_canonical(live: ThreeCatClient) -> None:
    for pid, title in ((55136, "Jo, Ramon Llull"), (70456, "Megamix brutal"), (177212487, "10+2")):
        assert {e.program_title for e in live.list_episodes(pid)} == {title}
    doraemon = live.get_episode(4534331).program_title
    assert doraemon == live.list_episodes(119987484)[0].program_title == "Doraemon, el gat còsmic"


@pytest.mark.live
def test_live_season_groups_have_unique_numbers(live: ThreeCatClient) -> None:
    atrapam = live.list_seasons(live.list_episodes(58216))
    numbers = [s.number for s in atrapam if s.number is not None]
    assert len(numbers) == len(set(numbers))
    by_id = {s.id: s for s in atrapam}
    assert by_id["PUTEMP_7"].episode_count >= 199
    assert by_id[""].number == 0 and by_id[""].episode_count >= 300
    mercats = {s.id: s for s in live.list_seasons(live.list_episodes(59556))}
    assert "" not in mercats and mercats["PUTEMP_1"].episode_count >= 11


@pytest.mark.live
def test_live_languages_and_specials(live: ThreeCatClient) -> None:
    assert len(live.list_episodes(50816)) == 104          # Zoobabu without the English copies
    polonia = live.list_episodes(200164279)
    special = next(e for e in polonia if e.id == 5710198)
    assert special.season_number == 0
    connie = next(e for e in live.list_episodes(50257) if e.id == 4526871)
    assert (connie.season_number, connie.episode_number) == (2, 48)
    nissaga = live.get_episode(5622292)
    assert nissaga.program_id == 73277 and nissaga.program_title.startswith("Nissaga")


# --------------------------------------------------------------------------- #
# Live tests (real network)
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="module")
def live() -> ThreeCatClient:
    client = ThreeCatClient()
    yield client
    client.close()


@pytest.mark.live
def test_live_catalogue_and_slug(live: ThreeCatClient) -> None:
    programs = live.catalogue()
    assert len(programs) > 500
    merli = live.program_by_slug("merli")
    assert merli is not None and merli.id == 54076 and merli.title == "Merlí"


@pytest.mark.live
def test_live_search(live: ThreeCatClient) -> None:
    assert live.search("merlí")[0].id == 54076
    assert live.search("polseres vermelles")[0].id == 46356


@pytest.mark.live
def test_live_merli_seasons(live: ThreeCatClient) -> None:
    episodes = live.list_episodes(54076)
    assert len(episodes) == 40
    seasons = live.list_seasons(episodes)
    assert [s.number for s in seasons] == [1, 2, 3]
    season2 = live.list_episodes(54076, season_id="PUTEMP_2")
    assert len(season2) == 13
    assert [e.episode_number for e in season2] == list(range(1, 14))
    assert episodes[0].title == "Els peripatètics" and episodes[0].air_date == "2015-09-14"


@pytest.mark.live
def test_live_pagination_small_pages(live: ThreeCatClient) -> None:
    live.page_size = 5
    try:
        episodes = live.list_episodes(54076, season_id="PUTEMP_2")   # 13 items -> 3 pages
    finally:
        live.page_size = ThreeCatClient.page_size
    assert len(episodes) == 13 and len({e.id for e in episodes}) == 13


@pytest.mark.live
def test_live_telenoticies_pagination_metadata(live: ThreeCatClient) -> None:
    # Do not fetch all ~19k items: check paging metadata with a tiny page.
    resposta = live._api("videos", programatv_id=136938435, tipus_contingut="PPD", items_pagina=3, pagina=2)
    assert int(resposta["paginacio"]["total_pagines"]) > 1000
    assert len(as_list(resposta["items"]["item"])) == 3


@pytest.mark.live
def test_live_kids_and_one_off(live: ThreeCatClient) -> None:
    # Kids programme without seasons (Viu el Super3).
    super3 = live.list_episodes(74516)
    assert super3 and all(e.season_number is None and e.episode_number is None for e in super3)
    assert live.list_seasons(super3)[0].id == ""
    # Kids series with seasons (Bola de Drac): sorted, first episode is T1xC1.
    season1 = live.list_episodes(48836, season_id="PUTEMP_1")
    assert season1[0].season_number == 1 and season1[0].episode_number == 1
    assert season1[0].title == "Bulma i Son Goku"
    # Kids programme without seasons but with absolute numbers (Doraemon).
    resposta = live._api("videos", programatv_id=119987484, tipus_contingut="PPD", items_pagina=3, pagina=1)
    assert len(as_list(resposta["items"]["item"])) == 3


@pytest.mark.live
def test_live_film_resolution(live: ThreeCatClient) -> None:
    result = live.resolve("https://www.3cat.cat/tv3/sx3/panic/video/6432755/")
    assert result["type"] == "video"
    film = result["episode"]
    assert film.id == 6432755 and film.program_id == 0 and film.title == "Pànic"
    assert film.duration_seconds and film.duration_seconds > 3600


@pytest.mark.live
def test_live_media_clip(live: ThreeCatClient) -> None:
    media = live.get_media(6293447, parse_dash=True)
    assert media.available and media.geo == "TOTS"
    dash = [s for s in media.sources if s.kind == "dash"]
    mp4 = [s for s in media.sources if s.kind == "mp4"]
    assert dash and dash[0].height == 1080
    assert mp4 and mp4[0].height == 720
    assert media.subtitles and media.subtitles[0].lang == "ca"


@pytest.mark.live
def test_live_resolve_forms(live: ThreeCatClient) -> None:
    assert live.resolve("https://www.3cat.cat/3cat/merli/")["program"].id == 54076
    assert live.resolve("https://www.ccma.cat/tv3/alacarta/polseres-vermelles/")["program"].id == 46356
    video = live.resolve("6293447")
    assert video["type"] == "video" and video["program"].id == 136938435
    with pytest.raises(ThreeCatNotFound):
        live.get_media(1)
