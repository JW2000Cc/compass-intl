"""Smoke + form-roundtrip tests for /intent UI routes (ADR-0016 task #13)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from compass.app import create_app


@pytest.fixture
def app(tmp_path, monkeypatch):
    """Build a Flask test app whose data_dir is an isolated tmp_path."""
    monkeypatch.setenv("COMPASS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPASS_DB_URL", f"sqlite:///{tmp_path}/test.sqlite")
    app = create_app()
    app.config["TESTING"] = True
    return app


@pytest.fixture
def client(app):
    return app.test_client()


# ─── Smoke ────────────────────────────────────────────────


def test_overview_empty_returns_200(client):
    """C1 (5-08): /search-config/ is now a standalone page (was a redirect to /jobs/
    in 5-07). Re-extracted because cramming editing UI into the list view made the
    list page a 655-line mess; sidebar 求职意图 link points here directly now."""
    resp = client.get("/jobs/search-config/")
    assert resp.status_code == 200


def test_overview_lists_existing_profiles(client, tmp_path):
    """C1 (5-08): standalone overview page back. Renders the multi-profile form
    via the shared jobs/searches/_panel.html partial."""
    resp = client.get("/jobs/search-config/")
    assert resp.status_code == 200
    assert b"\xe6\xb1\x82\xe8\x81\x8c\xe6\x84\x8f\xe5\x9b\xbe" in resp.data  # 求职意图


def test_new_creates_profile_and_redirects(client):
    resp = client.post("/jobs/search-config/new", data={"label": "新建测试"}, follow_redirects=False)
    assert resp.status_code == 302
    assert "/jobs/search-config/" in resp.headers["Location"]


def test_edit_round_trip(client, tmp_path):
    """5-07: edit GET now redirects (no independent page); save POST still works."""
    client.post("/jobs/search-config/new", data={"label": "电气-柏林"})
    # GET should redirect now (no edit page)
    resp = client.get("/jobs/search-config/dianqi-bolin")
    assert resp.status_code == 302


def test_toggle_flips_enabled(client, tmp_path):
    client.post("/jobs/search-config/new", data={"label": "切换测试"})
    pid = json.loads((tmp_path / "last_scrape.json").read_text())["profiles"][0]["id"]
    # New profiles start disabled
    assert json.loads((tmp_path / "last_scrape.json").read_text())["profiles"][0]["enabled"] is False

    client.post(f"/jobs/search-config/{pid}/toggle")
    assert json.loads((tmp_path / "last_scrape.json").read_text())["profiles"][0]["enabled"] is True

    client.post(f"/jobs/search-config/{pid}/toggle")
    assert json.loads((tmp_path / "last_scrape.json").read_text())["profiles"][0]["enabled"] is False


def test_delete_requires_confirmation(client, tmp_path):
    client.post("/jobs/search-config/new", data={"label": "删除测试"})
    pid = json.loads((tmp_path / "last_scrape.json").read_text())["profiles"][0]["id"]

    # Without confirm: profile survives
    client.post(f"/jobs/search-config/{pid}/delete", data={})
    assert len(json.loads((tmp_path / "last_scrape.json").read_text())["profiles"]) == 1

    # With confirm: profile deleted
    client.post(f"/jobs/search-config/{pid}/delete", data={"confirm": "yes"})
    assert json.loads((tmp_path / "last_scrape.json").read_text())["profiles"] == []


def test_global_section_save(client, tmp_path):
    client.post(
        "/jobs/search-config/global",
        data={
            "user_languages": "en\nzh",
            "exclude_categories": "bartender, nursing",
            "min_salary": "50000",
            "remote_preference": "hybrid",
        },
    )
    intent_data = json.loads((tmp_path / "last_scrape.json").read_text())
    g = intent_data["global"]
    assert g["user_languages"] == ["en", "zh"]
    assert g["exclude_categories"] == ["bartender", "nursing"]
    assert g["min_salary"] == 50000.0
    assert g["remote_preference"] == "hybrid"


def test_edit_nonexistent_profile_redirects(client):
    resp = client.get("/jobs/search-config/nope-not-real")
    assert resp.status_code == 302  # redirect with flash error


def test_jobs_list_js_loads_server_groups_first(client, tmp_path):
    """Regression: scrapeConfig._load() MUST prefer server-initial-groups over
    localStorage. Otherwise stale browser state silently overrides the server's
    last_scrape.json and the user sees an "empty" config UI even though
    profiles exist on disk."""
    intent = {
        "version": 1,
        "global": {
            "user_languages": [],
            "exclude_categories": [],
            "min_salary": None,
            "remote_preference": "any",
        },
        "profiles": [
            {
                "id": "test-de-frankfurt",
                "label": "德国-法兰克福",
                "enabled": True,
                "city": "Frankfurt",
                "country": "Germany",
                "radius_km": 30,
                "keywords": ["Power Engineer"],
                "keyword_aliases": {},
                "user_prompt": "test",
                "blacklist": {"companies": [], "keywords": []},
                "dealbreakers": {
                    "country_must_be": ["Germany"],
                    "language_required": [],
                    "language_blocked": [],
                    "visa_required": False,
                    "allow_remote_in_region": True,
                },
                "tier_quota": {"tier_1": 5, "tier_2": 3, "tier_3": 2},
                "hours_old": 168,
            }
        ],
    }
    (tmp_path / "last_scrape.json").write_text(json.dumps(intent), encoding="utf-8")

    resp = client.get("/jobs/?scrape=1")
    assert resp.status_code == 200
    html = resp.data.decode("utf-8")

    # Server must render the groups
    assert 'id="server-initial-groups"' in html
    assert "德国-法兰克福" in html

    # _load() must check server BEFORE localStorage
    load_idx = html.find("_load() {")
    assert load_idx > 0, "scrapeConfig._load not found in template"
    body = html[load_idx : load_idx + 1500]
    server_idx = body.find("server-initial-groups")
    local_idx = body.find("localStorage.getItem")
    assert 0 < server_idx < local_idx, (
        f"localStorage.getItem ({local_idx}) must come AFTER "
        f"server-initial-groups ({server_idx}) in _load(); otherwise "
        f"stale localStorage shadows the server source of truth."
    )

    # And when server has groups, localStorage cache must be cleared so a
    # subsequent return to the page can't resurrect old state.
    assert "localStorage.removeItem(this.STORAGE_KEY)" in body, (
        "_load() should clear stale localStorage cache when server has groups."
    )


def test_new_profile_id_collision_resolves(client, tmp_path):
    """Two profiles with the same label get auto-suffixed IDs."""
    client.post("/jobs/search-config/new", data={"label": "电气-柏林"})
    client.post("/jobs/search-config/new", data={"label": "电气-柏林"})  # same label
    profiles = json.loads((tmp_path / "last_scrape.json").read_text())["profiles"]
    ids = [p["id"] for p in profiles]
    assert len(ids) == 2
    assert ids[0] != ids[1]


# ─── Regression: form submit MUST preserve advanced fields + global_ ─────


def test_scrape_form_preserves_existing_id_and_advanced_fields(client, tmp_path):
    """Regression for 5-07 silent-wipe bug.

    Before fix: clicking 📥 抓取 (POST /jobs/scrape) regenerated profile IDs
    from labels, wiped global_, and reset tier_quota / radius_km / half of
    dealbreakers — everything the form didn't carry.

    Now: form merges onto existing intent. id_<i> hidden field anchors the
    profile; advanced fields not in form fall back to prior profile.
    """
    import json

    seed = {
        "version": 1,
        "global": {
            "user_languages": ["en", "zh"],
            "exclude_categories": ["nursing"],
            "min_salary": 60000.0,
            "remote_preference": "hybrid",
        },
        "profiles": [
            {
                "id": "stable-id-keep-me",
                "label": "Profile A",
                "enabled": True,
                "city": "Berlin",
                "country": "Germany",
                "radius_km": 75,
                "keywords": ["ML"],
                "keyword_aliases": {},
                "user_prompt": "old prompt",
                "blacklist": {"companies": [], "keywords": []},
                "dealbreakers": {
                    "country_must_be": ["Germany"],
                    "language_required": ["en"],
                    "language_blocked": [],
                    "visa_required": True,
                    "allow_remote_in_region": False,
                },
                "tier_quota": {"tier_1": 9, "tier_2": 7, "tier_3": 5},
                "hours_old": 72,
            }
        ],
    }
    (tmp_path / "last_scrape.json").write_text(json.dumps(seed), encoding="utf-8")

    # Submit the same profile through the scrape form (mock scraper to skip the
    # network round-trip; we only audit the persisted state).
    from compass.services import scraper
    import compass.routes.jobs as jr
    saved_run = []

    def _mock_run(*a, **kw):
        saved_run.append(1)
        return {"new": 0, "updated": 0, "blocked": 0, "gate_blocked": 0}

    monkeypatch_scraper(scraper, _mock_run)

    resp = client.post(
        "/jobs/scrape",
        data={
            # NEW (post-fix) field — anchors merge to existing profile
            "id_1": "stable-id-keep-me",
            "label_1": "Profile A renamed",  # label change should not change id
            "enabled_1": "on",
            "locations_1": "Berlin, Germany",
            "city_1": "Berlin",
            "country_1": "Germany",
            "keywords_1": "ML, NLP",
            "user_prompt_1": "new prompt",
            "hours_old_1": "168",
            "country_must_be_1": "Germany",
            "language_blocked_1": "",
            "blacklist_companies_1": "",
            "blacklist_keywords_1": "",
            "keyword_aliases_1": "",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302

    after = json.loads((tmp_path / "last_scrape.json").read_text())

    # ID preserved
    assert len(after["profiles"]) == 1
    p = after["profiles"][0]
    assert p["id"] == "stable-id-keep-me", "form submit must NOT regenerate stable id"

    # Form fields applied
    assert p["label"] == "Profile A renamed"
    assert p["user_prompt"] == "new prompt"
    assert "NLP" in p["keywords"]
    assert p["hours_old"] == 168

    # Advanced fields not in form preserved
    assert p["radius_km"] == 75
    assert p["tier_quota"] == {"tier_1": 9, "tier_2": 7, "tier_3": 5}
    assert p["dealbreakers"]["language_required"] == ["en"]
    assert p["dealbreakers"]["visa_required"] is True
    assert p["dealbreakers"]["allow_remote_in_region"] is False

    # global_ preserved (NOT reset to defaults)
    g = after["global"]
    assert g["user_languages"] == ["en", "zh"]
    assert g["min_salary"] == 60000.0
    assert g["remote_preference"] == "hybrid"


def monkeypatch_scraper(scraper_mod, fn):
    scraper_mod.run_scrape_for_profile = fn


def test_scrape_form_with_empty_id_creates_new_profile(client, tmp_path):
    """When user adds a brand-new group via the UI '+ 添加一组配置' button,
    id_<i> is empty. Server should slug a fresh id (no collision)."""
    import json
    seed = {"version": 1, "global": {}, "profiles": [
        {"id": "germany-berlin", "label": "Existing", "enabled": True,
         "city": "Berlin", "country": "Germany", "radius_km": 30,
         "keywords": ["ML"], "keyword_aliases": {}, "user_prompt": "",
         "blacklist": {"companies": [], "keywords": []},
         "dealbreakers": {"country_must_be": ["Germany"], "language_required": [],
                          "language_blocked": [], "visa_required": False,
                          "allow_remote_in_region": True},
         "tier_quota": {"tier_1": 5, "tier_2": 3, "tier_3": 2}, "hours_old": 168}
    ]}
    (tmp_path / "last_scrape.json").write_text(json.dumps(seed), encoding="utf-8")

    from compass.services import scraper
    monkeypatch_scraper(scraper, lambda *a, **kw: {"new": 0, "updated": 0, "blocked": 0, "gate_blocked": 0})

    # Both groups submitted; group #2 has no id (newly added)
    resp = client.post(
        "/jobs/scrape",
        data={
            # Existing group survives
            "id_1": "germany-berlin",
            "label_1": "Existing",
            "enabled_1": "on",
            "locations_1": "Berlin, Germany", "city_1": "Berlin", "country_1": "Germany",
            "keywords_1": "ML", "hours_old_1": "168", "country_must_be_1": "Germany",
            # New group — no id_2 sent (or empty); server slugs from country/city
            "id_2": "",
            "label_2": "Brand New",
            "enabled_2": "on",
            "locations_2": "Munich, Germany", "city_2": "Munich", "country_2": "Germany",
            "keywords_2": "Quant", "hours_old_2": "168", "country_must_be_2": "Germany",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302

    after = json.loads((tmp_path / "last_scrape.json").read_text())
    ids = [p["id"] for p in after["profiles"]]
    assert "germany-berlin" in ids
    assert any(p["id"] != "germany-berlin" and p["label"] == "Brand New" for p in after["profiles"])


def test_save_intent_is_concurrent_safe(tmp_path):
    """Regression: 3 concurrent saves used to race on a shared .tmp name and
    log `[Errno 2] No such file` warnings. Now each thread writes a uniquely
    named tmp, then a module-level lock serialises the atomic rename."""
    import threading
    from compass.services.scrape_config import save, load, JobSearchIntent, ProfileConfig

    def make_intent(label):
        return JobSearchIntent(profiles=[
            ProfileConfig(id=label, label=label, enabled=True,
                          city="X", country="Y", keywords=[label])
        ])

    errors = []

    def hit(i):
        try:
            for _ in range(20):
                save(tmp_path, make_intent(f"p{i}"))
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=hit, args=(i,)) for i in range(5)]
    for t in threads: t.start()
    for t in threads: t.join()

    assert not errors, f"concurrent save raised: {errors}"

    # File must be valid JSON containing exactly one of the labels (no half-write)
    final = load(tmp_path)
    assert len(final.profiles) == 1
    assert final.profiles[0].label in {f"p{i}" for i in range(5)}

    # No leftover .tmp files
    leftovers = list(tmp_path.glob("*.tmp*"))
    assert not leftovers, f"unique-tmp scheme should leave no junk: {leftovers}"
