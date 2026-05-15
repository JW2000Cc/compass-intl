"""Tests for ADR-0016 in-place: conflict_detector single-sourcing on
last_scrape.json with legacy last_scrape.json fallback."""
from __future__ import annotations

import json

import pytest
from flask import Flask

from compass.services import conflict_detector


@pytest.fixture
def app(tmp_path):
    """Minimal Flask app context so current_app.config['SETTINGS'].data_dir resolves."""
    class _Settings:
        data_dir = tmp_path
    app = Flask(__name__)
    app.config["SETTINGS"] = _Settings()
    return app


# ─── Primary source: last_scrape.json ──────────────


def test_reads_from_job_search_config_when_present(app, tmp_path):
    (tmp_path / "last_scrape.json").write_text(
        json.dumps({
            "version": 1,
            "profiles": [
                {
                    "id": "de", "label": "DE", "enabled": True,
                    "city": "Berlin", "country": "Germany",
                    "keywords": ["ML Engineer", "Power Engineer"],
                }
            ],
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    with app.app_context():
        kws, locs = conflict_detector._scrape_keywords_and_locations()
    assert "ml engineer" in kws
    assert "power engineer" in kws
    assert any("berlin" in l for l in locs)


def test_skips_disabled_profiles(app, tmp_path):
    (tmp_path / "last_scrape.json").write_text(
        json.dumps({
            "profiles": [
                {"id": "a", "enabled": True, "city": "Berlin",
                 "country": "Germany", "keywords": ["ML"]},
                {"id": "b", "enabled": False, "city": "Milan",
                 "country": "Italy", "keywords": ["FA"]},
            ],
        }),
        encoding="utf-8",
    )
    with app.app_context():
        kws, locs = conflict_detector._scrape_keywords_and_locations()
    assert "ml" in kws
    assert "fa" not in kws
    assert any("berlin" in l for l in locs)
    assert not any("milan" in l for l in locs)


# ─── Legacy fallback: last_scrape.json ──────────────────


def test_falls_back_to_last_scrape_when_no_config(app, tmp_path):
    (tmp_path / "last_scrape.json").write_text(
        json.dumps({
            "groups": [
                {"locations": ["Berlin, Germany"], "keywords": ["Power Engineer"]}
            ]
        }),
        encoding="utf-8",
    )
    with app.app_context():
        kws, locs = conflict_detector._scrape_keywords_and_locations()
    assert "power engineer" in kws
    assert any("berlin" in l for l in locs)


def test_returns_empty_when_no_files(app, tmp_path):
    with app.app_context():
        kws, locs = conflict_detector._scrape_keywords_and_locations()
    assert kws == []
    assert locs == []


def test_corrupt_config_falls_through_to_legacy(app, tmp_path):
    """Corrupt primary should not crash; legacy fallback kicks in."""
    (tmp_path / "last_scrape.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "last_scrape.json").write_text(
        json.dumps({
            "groups": [{"locations": ["Zurich"], "keywords": ["FA"]}]
        }),
        encoding="utf-8",
    )
    with app.app_context():
        kws, locs = conflict_detector._scrape_keywords_and_locations()
    assert "fa" in kws
    assert any("zurich" in l for l in locs)


