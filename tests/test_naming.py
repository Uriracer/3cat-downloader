"""Tests for tresdl.naming."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from tresdl.models import Episode
from tresdl.naming import (
    DEFAULT_TEMPLATE,
    MAX_COMPONENT_LENGTH,
    audio_description_path,
    episode_path,
    sanitize,
    subtitle_path,
    validate_template,
)


def make_ep(**kw) -> Episode:
    base = dict(
        id=123, program_id=54076, program_title="Merlí", title="Els peripatètics",
        raw_title="T1xC1 - Els peripatètics", season_id="PUTEMP_1", season_number=1,
        episode_number=1, absolute_number=1, air_date="2015-09-14",
    )
    base.update(kw)
    return Episode(**base)


def rel(root: Path, path: str) -> str:
    return Path(path).relative_to(root).as_posix()


# --------------------------------------------------------------------------- sanitize

@pytest.mark.parametrize("raw, expected", [
    ("Merlí", "Merlí"),
    ('a<b>c"d|e?f*g', "abc'd-efg"),
    ("Polseres vermelles: el retorn", "Polseres vermelles - el retorn"),
    ("a/b\\c", "a-b-c"),
    ("  trailing dots...  ", "trailing dots"),
    ("tab\tand\nnewline\x00", "tab and newline"),
    ("CON", "_CON"),
    ("nul.txt", "_nul.txt"),
    ("com1", "_com1"),
    ("CONSOLE", "CONSOLE"),
    ("", "_"),
    ("...", "_"),
    ("..", "_"),
])
def test_sanitize(raw: str, expected: str) -> None:
    assert sanitize(raw) == expected


def test_sanitize_length_limit() -> None:
    out = sanitize("x" * 400 + ".")
    assert len(out) <= MAX_COMPONENT_LENGTH
    assert not out.endswith((".", " "))


def test_sanitize_truncation_strips_trailing_dot() -> None:
    out = sanitize("a" * (MAX_COMPONENT_LENGTH - 1) + ". b")
    assert not out.endswith(".")


# --------------------------------------------------------------------------- episode_path

def test_default_layout(tmp_path: Path) -> None:
    path = episode_path(str(tmp_path), make_ep())
    assert rel(tmp_path, path) == "Merlí/Season 01/Merlí - S01E01 - Els peripatètics.mp4"
    assert os.path.isabs(path)


def test_default_template_constant_used_when_none(tmp_path: Path) -> None:
    ep = make_ep(season_number=2, episode_number=13, absolute_number=26)
    assert episode_path(str(tmp_path), ep) == episode_path(str(tmp_path), ep, template=DEFAULT_TEMPLATE)
    assert rel(tmp_path, episode_path(str(tmp_path), ep)).endswith("S02E13 - Els peripatètics.mp4")


def test_no_season_uses_season_one_and_absolute(tmp_path: Path) -> None:
    ep = make_ep(season_number=None, season_id="", episode_number=None, absolute_number=7)
    assert rel(tmp_path, episode_path(str(tmp_path), ep)) == \
        "Merlí/Season 01/Merlí - S01E07 - Els peripatètics.mp4"


def test_season_without_episode_uses_absolute(tmp_path: Path) -> None:
    ep = make_ep(season_number=3, episode_number=None, absolute_number=30)
    assert "S03E30" in episode_path(str(tmp_path), ep)


def test_no_numbers_goes_to_specials_with_date(tmp_path: Path) -> None:
    ep = make_ep(season_number=None, episode_number=None, absolute_number=None)
    assert rel(tmp_path, episode_path(str(tmp_path), ep)) == \
        "Merlí/Specials/Merlí - 2015-09-14 - Els peripatètics.mp4"


def test_no_numbers_no_date_uses_id(tmp_path: Path) -> None:
    ep = make_ep(season_number=None, episode_number=None, absolute_number=None, air_date=None)
    path = episode_path(str(tmp_path), ep)
    assert rel(tmp_path, path) == "Merlí/Specials/Merlí - 123 - Els peripatètics.mp4"
    assert "None" not in path


def test_custom_template_with_all_placeholders(tmp_path: Path) -> None:
    ep = make_ep(season_number=2, episode_number=3, absolute_number=16)
    tpl = "{series}/{absolute:03d} {date} [{id}] {title} S{season}E{episode}"
    assert rel(tmp_path, episode_path(str(tmp_path), ep, template=tpl)) == \
        "Merlí/016 2015-09-14 [123] Els peripatètics S2E3.mp4"


def test_template_without_numbers_does_not_need_them(tmp_path: Path) -> None:
    ep = make_ep(season_number=None, episode_number=None, absolute_number=None)
    assert rel(tmp_path, episode_path(str(tmp_path), ep, template="{series}/{title}")) == \
        "Merlí/Els peripatètics.mp4"


def test_never_none_in_filename(tmp_path: Path) -> None:
    ep = make_ep(season_number=None, episode_number=None, absolute_number=None,
                 air_date=None, title="", program_title="")
    for tpl in (DEFAULT_TEMPLATE, "{series}/{date}/{title} {absolute}", "{season!s}/{title}"):
        path = episode_path(str(tmp_path), ep, template=tpl)
        assert "None" not in path


def test_invalid_template_falls_back_to_default(tmp_path: Path) -> None:
    ep = make_ep()
    expected = episode_path(str(tmp_path), ep)
    for bad in ("{nope}/{title}", "{title:02d}", "{series", ""):
        assert episode_path(str(tmp_path), ep, template=bad) == expected


@pytest.mark.parametrize("series, title", [
    ("..", "../../evil"),
    ("../..", "..\\..\\windows"),
    ("C:\\Windows", "/etc/passwd"),
    ("CON", "AUX"),
])
def test_path_stays_inside_root(tmp_path: Path, series: str, title: str) -> None:
    ep = make_ep(program_title=series, title=title)
    for tpl in (DEFAULT_TEMPLATE, "{series}/{title}", "../{title}", "/{series}/../../{title}"):
        path = Path(episode_path(str(tmp_path), ep, template=tpl))
        assert os.path.commonpath([str(tmp_path), str(path)]) == str(tmp_path)
        assert path != tmp_path
        for part in path.relative_to(tmp_path).parts:
            assert part not in (".", "..")


def test_extension_handling(tmp_path: Path) -> None:
    ep = make_ep()
    assert episode_path(str(tmp_path), ep, ext=".mkv").endswith("Els peripatètics.mkv")
    assert episode_path(str(tmp_path), ep, ext="").endswith("Els peripatètics")


def test_title_with_slashes_and_colons(tmp_path: Path) -> None:
    ep = make_ep(title="Amor/odi: 50% *final*?")
    path = episode_path(str(tmp_path), ep)
    assert Path(path).name == "Merlí - S01E01 - Amor-odi - 50% final.mp4"


def test_validate_template() -> None:
    validate_template(DEFAULT_TEMPLATE)
    validate_template("{series}/{title} ({date})")
    for bad in ("{foo}", "{title:02d}", "{series", "", "   "):
        with pytest.raises(ValueError):
            validate_template(bad)


def test_sidecar_paths(tmp_path: Path) -> None:
    video = str(tmp_path / "Merlí - S01E01 - Els peripatètics.mp4")
    assert Path(subtitle_path(video, "ca")).name == "Merlí - S01E01 - Els peripatètics.ca.vtt"
    assert Path(audio_description_path(video)).name == \
        "Merlí - S01E01 - Els peripatètics (Audiodescripció).mp4"


# --------------------------------------------------------------------------- regression

def test_template_must_identify_the_episode() -> None:
    from tresdl.naming import IDENTIFYING_PLACEHOLDERS

    for bad in ("{series}/{series} {season}", "{series}/{series} - {date}", "{series}"):
        with pytest.raises(ValueError, match="identificar"):
            validate_template(bad)
    for good in ("{series}/{id}", "{series}/E{absolute:03d}", "{series}/{title}",
                 "{series}/S{season:02d}E{episode:02d}"):
        validate_template(good)
    assert set(IDENTIFYING_PLACEHOLDERS) == {"episode", "absolute", "title", "id"}


def test_disambiguate_is_deterministic_and_idempotent(tmp_path: Path) -> None:
    from tresdl.naming import disambiguate

    plain = episode_path(str(tmp_path), make_ep())
    once = disambiguate(plain, 6309873)
    assert Path(once).name == "Merlí - S01E01 - Els peripatètics [6309873].mp4"
    assert disambiguate(once, 6309873) == once
    long_name = str(tmp_path / ("x" * MAX_COMPONENT_LENGTH + ".mp4"))
    assert len(Path(disambiguate(long_name, 1)).stem) <= MAX_COMPONENT_LENGTH


def test_season_zero_goes_to_specials(tmp_path: Path) -> None:
    special = make_ep(season_number=0, season_id="", episode_number=None, absolute_number=1,
                      title="Un Vasco en Polonia", air_date="2017-12-19")
    path = Path(episode_path(str(tmp_path), special))
    assert path.parent.name == "Specials"
    assert path.name == "Merlí - 2017-12-19 - Un Vasco en Polonia.mp4"
    real = Path(episode_path(str(tmp_path), make_ep()))
    assert real != path and "S01E01" in real.name
