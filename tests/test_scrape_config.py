"""Tests for search_config — multi-profile schema + last_scrape backward-compat."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from compass.services.scrape_config import (
    Blacklist,
    Dealbreakers,
    GlobalIntent,
    JobSearchIntent,
    ProfileConfig,
    TierQuota,
    INTENT_VERSION,
    load,
    save,
)


# ─── ProfileConfig dataclass ──────────────────────────────


def test_profile_config_defaults():
    p = ProfileConfig(id="x", label="Test")
    assert p.enabled is True
    assert p.radius_km == 30
    assert p.blacklist.companies == []
    assert p.dealbreakers.country_must_be == []
    assert p.tier_quota.tier_1 == 5


def test_profile_location_string_combines_city_country():
    p = ProfileConfig(id="x", label="Test", city="Berlin", country="Germany")
    assert p.location_string == "Berlin, Germany"


def test_profile_location_string_falls_back():
    p = ProfileConfig(id="x", label="Test", city="Remote")
    assert p.location_string == "Remote"


def test_profile_from_dict_safe_with_partial_input():
    p = ProfileConfig.from_dict({"label": "X"})
    assert p.label == "X"
    assert p.id  # auto-slugged
    assert p.enabled is True


def test_profile_from_dict_filters_non_strings():
    p = ProfileConfig.from_dict(
        {"label": "Y", "keywords": ["a", 5, "b", None, "c"]}
    )
    assert p.keywords == ["a", "b", "c"]


def test_profile_round_trip_jsonable():
    p1 = ProfileConfig(
        id="de-electrical",
        label="德国-电气",
        city="Berlin",
        country="Germany",
        keywords=["Power Engineer"],
        dealbreakers=Dealbreakers(country_must_be=["Germany"]),
        tier_quota=TierQuota(tier_1=10, tier_2=5, tier_3=3),
    )
    j = p1.to_jsonable()
    p2 = ProfileConfig.from_dict(j)
    assert p1 == p2


# ─── JobSearchIntent envelope ─────────────────────────────


def test_intent_default_empty():
    intent = JobSearchIntent()
    assert intent.version == INTENT_VERSION
    assert intent.profiles == []
    assert intent.enabled_profiles() == []


def test_intent_enabled_profiles_filters():
    intent = JobSearchIntent(
        profiles=[
            ProfileConfig(id="a", label="A", enabled=True),
            ProfileConfig(id="b", label="B", enabled=False),
            ProfileConfig(id="c", label="C", enabled=True),
        ]
    )
    enabled_ids = [p.id for p in intent.enabled_profiles()]
    assert enabled_ids == ["a", "c"]


def test_intent_find_by_id():
    intent = JobSearchIntent(profiles=[ProfileConfig(id="x", label="X")])
    assert intent.find("x").label == "X"
    assert intent.find("nope") is None


def test_intent_round_trip(tmp_path):
    intent1 = JobSearchIntent(
        global_=GlobalIntent(user_languages=["en", "zh"], exclude_categories=["bartender"]),
        profiles=[
            ProfileConfig(
                id="de", label="German", city="Berlin", country="Germany",
                keywords=["ML Engineer"],
            )
        ],
    )
    save(tmp_path, intent1)
    intent2 = load(tmp_path)
    assert intent2.version == INTENT_VERSION
    assert intent2.global_.user_languages == ["en", "zh"]
    assert intent2.global_.exclude_categories == ["bartender"]
    assert len(intent2.profiles) == 1
    assert intent2.profiles[0].keywords == ["ML Engineer"]


# ─── Backward compat: last_scrape.json migration ──────────


def test_load_falls_back_to_last_scrape_when_intent_missing(tmp_path):
    """If intent file missing but last_scrape.json exists, lazy-migrate."""
    last = {
        "groups": [
            {
                "name": "德国-柏林",
                "enabled": True,
                "locations": ["Berlin, Germany"],
                "keywords": ["ML Engineer", "Data Scientist"],
            }
        ]
    }
    (tmp_path / "last_scrape.json").write_text(
        json.dumps(last, ensure_ascii=False), encoding="utf-8"
    )
    intent = load(tmp_path)
    assert len(intent.profiles) == 1
    p = intent.profiles[0]
    assert p.city == "Berlin"
    assert p.country == "Germany"
    assert p.keywords == ["ML Engineer", "Data Scientist"]
    assert p.user_prompt == ""  # left empty for user to fill
    # country_must_be auto-populated from location
    assert "Germany" in p.dealbreakers.country_must_be


def test_migration_splits_multi_location_groups(tmp_path):
    """One group with 2 locations → 2 profiles (each single-location)."""
    last = {
        "groups": [
            {
                "name": "德国",
                "enabled": True,
                "locations": ["Berlin, Germany", "Frankfurt, Germany"],
                "keywords": ["Engineer"],
            }
        ]
    }
    (tmp_path / "last_scrape.json").write_text(
        json.dumps(last), encoding="utf-8"
    )
    intent = load(tmp_path)
    assert len(intent.profiles) == 2
    cities = sorted(p.city for p in intent.profiles)
    assert cities == ["Berlin", "Frankfurt"]


def test_load_returns_empty_when_no_files(tmp_path):
    intent = load(tmp_path)
    assert intent.profiles == []


def test_load_handles_corrupt_intent_file(tmp_path):
    (tmp_path / "last_scrape.json").write_text("{not json", encoding="utf-8")
    intent = load(tmp_path)
    # Falls back to empty (or last_scrape if present)
    assert isinstance(intent, JobSearchIntent)


def test_intent_jsonable_has_global_key():
    """Make sure 'global_' Python attribute serializes as 'global' (matches schema)."""
    intent = JobSearchIntent(global_=GlobalIntent(user_languages=["en"]))
    j = intent.to_jsonable()
    assert "global" in j
    assert j["global"]["user_languages"] == ["en"]


def test_real_user_last_scrape_format(tmp_path):
    """Smoke test using the user's actual last_scrape.json shape (2 groups)."""
    real = {
        "groups": [
            {
                "name": "德国-法兰克福",
                "enabled": True,
                "locations": ["Frankfurt, Germany"],
                "keywords": [
                    "Electrical Engineer",
                    "Power Engineer",
                    "Financial Advisor",
                    "Data Analyst",
                ],
                "hours_old": 168,
            },
            {
                "name": "德国-柏林",
                "enabled": True,
                "locations": ["Berlin, Germany"],
                "keywords": ["Electrical Engineer", "Data Analyst"],
                "hours_old": 168,
            },
        ]
    }
    (tmp_path / "last_scrape.json").write_text(
        json.dumps(real, ensure_ascii=False), encoding="utf-8"
    )
    intent = load(tmp_path)
    assert len(intent.profiles) == 2
    labels = {p.label for p in intent.profiles}
    assert any("法兰克福" in l for l in labels)
    assert any("柏林" in l for l in labels)
    # Each profile has the country dealbreaker auto-set
    for p in intent.profiles:
        assert p.dealbreakers.country_must_be == ["Germany"]
