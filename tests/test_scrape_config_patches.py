"""Tests for ADR-0016 补丁包 A+B+C+D."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from compass.app import create_app
from compass.routes.jobs import (
    _flag_for,
    _group_profiles_by_country,
    _parse_aliases,
    format_aliases,
)
from compass.services.scrape_config import (
    Dealbreakers,
    JobSearchIntent,
    ProfileConfig,
    load,
)


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPASS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPASS_DB_URL", f"sqlite:///{tmp_path}/test.sqlite")
    app = create_app()
    app.config["TESTING"] = True
    return app


@pytest.fixture
def client(app):
    return app.test_client()


# ─── 补丁 A: hours_old ────────────────────────────────────


def test_profile_default_hours_old_is_one_week():
    p = ProfileConfig(id="x", label="Test")
    assert p.hours_old == 168


def test_profile_round_trip_preserves_hours_old():
    p1 = ProfileConfig(id="x", label="t", hours_old=24)
    p2 = ProfileConfig.from_dict(p1.to_jsonable())
    assert p2.hours_old == 24


def test_profile_hours_old_none_round_trips():
    p1 = ProfileConfig(id="x", label="t", hours_old=None)
    p2 = ProfileConfig.from_dict(p1.to_jsonable())
    assert p2.hours_old is None


def test_migration_carries_hours_old(tmp_path):
    """Old last_scrape group.hours_old should land in new profile.hours_old."""
    legacy = {
        "groups": [
            {
                "name": "DE",
                "enabled": True,
                "locations": ["Berlin, Germany"],
                "keywords": ["ML"],
                "hours_old": 72,
            }
        ]
    }
    (tmp_path / "last_scrape.json").write_text(json.dumps(legacy), encoding="utf-8")
    intent = load(tmp_path)
    assert intent.profiles[0].hours_old == 72


def test_edit_form_saves_hours_old(client, tmp_path):
    client.post("/jobs/search-config/new", data={"label": "t"})
    pid = json.loads((tmp_path / "last_scrape.json").read_text())["profiles"][0]["id"]
    client.post(
        f"/jobs/search-config/{pid}",
        data={"label": "t", "hours_old": "24", "city": "x", "country": "y", "keywords": "kw"},
    )
    saved = json.loads((tmp_path / "last_scrape.json").read_text())["profiles"][0]
    assert saved["hours_old"] == 24


def test_edit_form_blank_hours_old_means_no_filter(client, tmp_path):
    client.post("/jobs/search-config/new", data={"label": "t"})
    pid = json.loads((tmp_path / "last_scrape.json").read_text())["profiles"][0]["id"]
    client.post(
        f"/jobs/search-config/{pid}",
        data={"label": "t", "hours_old": "", "city": "x", "country": "y", "keywords": "kw"},
    )
    saved = json.loads((tmp_path / "last_scrape.json").read_text())["profiles"][0]
    assert saved["hours_old"] is None


# ─── 补丁 B: keyword_aliases ──────────────────────────────


def test_parse_aliases_basic():
    raw = "Power Engineer: Elektroingenieur, Power Eng\nFinancial Advisor: doradca finansowy"
    out = _parse_aliases(raw)
    assert out == {
        "Power Engineer": ["Elektroingenieur", "Power Eng"],
        "Financial Advisor": ["doradca finansowy"],
    }


def test_parse_aliases_empty():
    assert _parse_aliases("") == {}
    assert _parse_aliases("no colon here") == {}
    assert _parse_aliases("only:") == {}  # empty alias list dropped


def test_format_aliases_inverse_of_parse():
    raw = "Power Engineer: Elektroingenieur, Power Eng"
    out = _parse_aliases(raw)
    rendered = format_aliases(out)
    re_parsed = _parse_aliases(rendered)
    assert re_parsed == out


def test_edit_form_saves_keyword_aliases(client, tmp_path):
    client.post("/jobs/search-config/new", data={"label": "t"})
    pid = json.loads((tmp_path / "last_scrape.json").read_text())["profiles"][0]["id"]
    client.post(
        f"/jobs/search-config/{pid}",
        data={
            "label": "t",
            "keywords": "Power Engineer",
            "keyword_aliases": "Power Engineer: Elektroingenieur",
            "city": "Berlin",
            "country": "Germany",
        },
    )
    saved = json.loads((tmp_path / "last_scrape.json").read_text())["profiles"][0]
    assert saved["keyword_aliases"] == {"Power Engineer": ["Elektroingenieur"]}


# ─── 补丁 C: scrape_all ───────────────────────────────────


def test_scrape_all_with_no_enabled_profiles_warns(client, tmp_path):
    client.post("/jobs/search-config/new", data={"label": "x"})  # new profiles default disabled
    resp = client.post("/jobs/search-config/scrape_all", follow_redirects=False)
    assert resp.status_code == 302  # redirect with flash


# ─── 补丁 D: 按国家分组 ──────────────────────────────────


def test_flag_for_known_country():
    assert _flag_for("Germany") == "🇩🇪"
    assert _flag_for("Switzerland") == "🇨🇭"
    assert _flag_for("Italy") == "🇮🇹"


def test_flag_for_unknown_country():
    assert _flag_for("Nowhere") == "🌍"


def test_group_profiles_basic():
    profs = [
        ProfileConfig(id="a", label="德-柏林", country="Germany", enabled=True),
        ProfileConfig(id="b", label="瑞-苏", country="Switzerland", enabled=True),
        ProfileConfig(id="c", label="德-法", country="Germany", enabled=True),
        ProfileConfig(id="d", label="意-米", country="Italy", enabled=False),
    ]
    grouped = _group_profiles_by_country(profs)
    countries = [g[1] for g in grouped]
    assert "Germany" in countries
    assert "Switzerland" in countries
    assert "Italy" in countries
    # Germany has 2 enabled, sorted first (most enabled wins)
    assert countries[0] == "Germany"


def test_group_pushes_no_country_to_end():
    profs = [
        ProfileConfig(id="a", label="无国", country="", enabled=True),
        ProfileConfig(id="b", label="德", country="Germany", enabled=True),
    ]
    grouped = _group_profiles_by_country(profs)
    countries = [g[2] for g in grouped]
    assert countries == ["Germany", "_other"]


def test_overview_renders_grouped(client, tmp_path):
    """C1 (5-08): standalone /search-config/ page restored — see test_routes_scrape_config."""
    resp = client.get("/jobs/search-config/")
    assert resp.status_code == 200
