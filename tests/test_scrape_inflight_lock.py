"""Tests for the in-flight scrape lock.

Prevents the "double-click 一键抓取 → 多个 jobspy 进程并发跑 → LinkedIn 限流"
bug. The lock is a file in data_dir; second concurrent scrape sees the file
and gets a flash + redirect instead of spawning a parallel pipeline.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from compass.routes.jobs import (
    ScrapeInFlightError,
    _scrape_lock,
    _LOCK_NAME,
    _STALE_AFTER_SEC,
)


def test_lock_creates_file_during_use(tmp_path):
    with _scrape_lock(tmp_path):
        assert (tmp_path / _LOCK_NAME).exists()
    assert not (tmp_path / _LOCK_NAME).exists()


def test_lock_clears_on_exception(tmp_path):
    with pytest.raises(ValueError):
        with _scrape_lock(tmp_path):
            assert (tmp_path / _LOCK_NAME).exists()
            raise ValueError("simulated scrape failure")
    assert not (tmp_path / _LOCK_NAME).exists()  # cleared even on exception


def test_concurrent_acquire_raises_inflight_error(tmp_path):
    """Second scrape while first is mid-flight → ScrapeInFlightError."""
    with _scrape_lock(tmp_path):
        # Inside the first scrape's body, the lock file exists
        with pytest.raises(ScrapeInFlightError) as exc_info:
            with _scrape_lock(tmp_path):
                pytest.fail("should never reach here")
        assert "running" in str(exc_info.value).lower()


def test_stale_lock_is_reclaimed(tmp_path):
    """Lock older than _STALE_AFTER_SEC = process probably crashed → reclaim."""
    lock_path = tmp_path / _LOCK_NAME
    lock_path.write_text("pid=99999\nstarted=0", encoding="utf-8")
    # Backdate mtime
    stale_time = time.time() - (_STALE_AFTER_SEC + 60)
    import os
    os.utime(lock_path, (stale_time, stale_time))

    # Acquire should succeed
    with _scrape_lock(tmp_path):
        # Lock now belongs to current process
        content = lock_path.read_text(encoding="utf-8")
        assert "pid=" in content
        assert "started=" in content


def test_lock_pid_recorded(tmp_path):
    import os
    with _scrape_lock(tmp_path):
        content = (tmp_path / _LOCK_NAME).read_text(encoding="utf-8")
        assert f"pid={os.getpid()}" in content


# ─── Integration with route ──────────────────────────────


@pytest.fixture
def app(tmp_path, monkeypatch):
    from compass.app import create_app

    monkeypatch.setenv("COMPASS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPASS_DB_URL", f"sqlite:///{tmp_path}/test.sqlite")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    app = create_app()
    app.config["TESTING"] = True
    return app


def test_scrape_all_short_circuits_when_lock_held(app, tmp_path):
    """If another scrape is mid-flight, scrape_all returns 302 with flash."""
    # Plant a fresh lock file
    lock_path = tmp_path / _LOCK_NAME
    lock_path.write_text("pid=1\nstarted=999999999", encoding="utf-8")
    # Set mtime to "now" so it's not stale
    import os
    now = time.time()
    os.utime(lock_path, (now, now))

    # Plant a profile so scrape_all has something to attempt
    from compass.services.scrape_config import (
        JobSearchIntent, ProfileConfig, save,
    )
    save(
        tmp_path,
        JobSearchIntent(profiles=[
            ProfileConfig(
                id="x", label="t", enabled=True,
                city="Berlin", country="Germany",
                keywords=["ML"],
            )
        ]),
    )

    client = app.test_client()
    resp = client.post("/jobs/search-config/scrape_all", follow_redirects=False)
    assert resp.status_code == 302  # redirect with flash
    # Lock file still there (we didn't release it)
    assert lock_path.exists()


# ─── Route-level wiring (the bug 5-07 missed: lock existed in services but
#     was only wrapped around scrape_all; scrape_now and scrape_direct —
#     the routes the UI buttons actually POST to — were unprotected) ──────


def _plant_live_lock(tmp_path):
    """Helper: drop a fresh, non-stale lock file."""
    import os
    lock_path = tmp_path / _LOCK_NAME
    lock_path.write_text("pid=1\nstarted=0", encoding="utf-8")
    now = time.time()
    os.utime(lock_path, (now, now))
    return lock_path


def _plant_one_profile(tmp_path):
    from compass.services.scrape_config import (
        JobSearchIntent, ProfileConfig, save,
    )
    save(
        tmp_path,
        JobSearchIntent(profiles=[
            ProfileConfig(
                id="x", label="t", enabled=True,
                city="Berlin", country="Germany",
                keywords=["ML"],
            )
        ]),
    )


def test_scrape_now_route_short_circuits_when_lock_held(app, tmp_path, monkeypatch):
    """POST /jobs/scrape (the 📥 抓取 button) MUST honour the in-flight lock.

    Regression: previously scrape_all wrapped the lock but scrape_now didn't,
    so triple-clicking the visible button still spawned 3 parallel pipelines."""
    lock_path = _plant_live_lock(tmp_path)
    _plant_one_profile(tmp_path)

    # Spy: if lock honoured, run_scrape_for_profile must NOT run
    calls = []
    from compass.services import scraper

    def _spy(*a, **kw):
        calls.append(1)
        return {"new": 0, "updated": 0, "blocked": 0, "gate_blocked": 0}

    monkeypatch.setattr(scraper, "run_scrape_for_profile", _spy)

    client = app.test_client()
    resp = client.post(
        "/jobs/scrape",
        data={
            "enabled_1": "on", "locations_1": "Berlin, Germany",
            "keywords_1": "ML", "hours_old_1": "168",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert calls == [], "scrape_now spawned scraper despite live lock"
    assert lock_path.exists(), "lock should not be released by a short-circuit"


def test_scrape_direct_route_short_circuits_when_lock_held(app, tmp_path, monkeypatch):
    """POST /jobs/scrape-direct (sidebar ⚡ 一键抓取) MUST honour the lock."""
    lock_path = _plant_live_lock(tmp_path)
    _plant_one_profile(tmp_path)

    calls = []
    from compass.services import scraper

    def _spy(*a, **kw):
        calls.append(1)
        return {"new": 0, "updated": 0, "blocked": 0, "gate_blocked": 0}

    monkeypatch.setattr(scraper, "run_scrape_for_profile", _spy)

    client = app.test_client()
    resp = client.post("/jobs/scrape-direct", follow_redirects=False)
    assert resp.status_code == 302
    assert calls == [], "scrape_direct spawned scraper despite live lock"
    assert lock_path.exists()


def test_scrape_now_classifies_after_scrape(app, tmp_path, monkeypatch):
    """Regression: scrape_now used to scrape but skip auto-classify, so all
    new jobs landed with tier=None and the list view rendered blank
    ('抓取后一个岗位也没有' bug). Lock NOT held here — happy path."""
    _plant_one_profile(tmp_path)

    classify_calls = []
    from compass.routes import jobs as jobs_routes
    real_classify = jobs_routes._classify_pending_for_profiles

    def _spy(session, profiles, settings):
        classify_calls.append(len(profiles))
        return real_classify(session, profiles, settings)

    monkeypatch.setattr(jobs_routes, "_classify_pending_for_profiles", _spy)

    from compass.services import scraper
    monkeypatch.setattr(
        scraper, "run_scrape_for_profile",
        lambda *a, **kw: {"new": 0, "updated": 0, "blocked": 0, "gate_blocked": 0},
    )

    client = app.test_client()
    resp = client.post(
        "/jobs/scrape",
        data={
            "enabled_1": "on", "locations_1": "Berlin, Germany",
            "keywords_1": "ML", "hours_old_1": "168",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert classify_calls and classify_calls[0] >= 1, (
        "_classify_pending_for_profiles must run after scrape_now"
    )


def test_scrape_direct_classifies_after_scrape(app, tmp_path, monkeypatch):
    _plant_one_profile(tmp_path)

    classify_calls = []
    from compass.routes import jobs as jobs_routes
    real_classify = jobs_routes._classify_pending_for_profiles

    def _spy(session, profiles, settings):
        classify_calls.append(len(profiles))
        return real_classify(session, profiles, settings)

    monkeypatch.setattr(jobs_routes, "_classify_pending_for_profiles", _spy)

    from compass.services import scraper
    monkeypatch.setattr(
        scraper, "run_scrape_for_profile",
        lambda *a, **kw: {"new": 0, "updated": 0, "blocked": 0, "gate_blocked": 0},
    )

    client = app.test_client()
    resp = client.post("/jobs/scrape-direct", follow_redirects=False)
    assert resp.status_code == 302
    assert classify_calls and classify_calls[0] >= 1, (
        "_classify_pending_for_profiles must run after scrape_direct"
    )


def test_scrape_buttons_have_client_side_double_click_guard():
    """The two scrape forms MUST opt into the data-slow-action handler so
    rapid double-clicks are prevented at the browser level (lock is the
    backstop, not the only guard).

    C1 (5-08): the main 📥 抓取 form moved from jobs/list.html into the
    shared partial jobs/searches/_panel.html (rendered both by list.html
    and search-config standalone page). Test follows it.
    """
    from pathlib import Path
    base = Path(__file__).resolve().parents[1]

    panel_html = (base / "compass/templates/jobs/searches/_panel.html").read_text(encoding="utf-8")
    base_html = (base / "compass/templates/base.html").read_text(encoding="utf-8")

    # Main 📥 抓取 form (jobs.scrape_now) — now lives in the shared panel partial
    assert "jobs.scrape_now" in panel_html
    main_form = _form_block_for(panel_html, "jobs.scrape_now")
    assert 'data-slow-action="true"' in main_form, (
        "jobs/searches/_panel.html main scrape form lacks data-slow-action"
    )

    # Sidebar ⚡ 一键抓取 form (jobs.scrape_direct)
    assert "jobs.scrape_direct" in base_html
    sidebar_form = _form_block_for(base_html, "jobs.scrape_direct")
    assert 'data-slow-action="true"' in sidebar_form, (
        "base.html sidebar scrape form lacks data-slow-action"
    )


def _form_block_for(html: str, action_endpoint: str) -> str:
    """Return the <form>...</form> block whose action references endpoint."""
    i = html.find(action_endpoint)
    assert i > 0, f"{action_endpoint} not found"
    start = html.rfind("<form", 0, i)
    end = html.find("</form>", i)
    assert start > 0 and end > 0
    return html[start:end + len("</form>")]


def test_scrape_lock_serializes_under_concurrent_acquire(tmp_path):
    """Regression for TOCTOU race in `_scrape_lock`.

    Before fix: `if lock_path.exists(): raise; lock_path.write_text(...)` had
    a check-then-act window. 5 simultaneous threads could all see "no lock"
    and all enter the critical section. Real-world impact: triple-clicking
    📥 抓取 fanned out 3 parallel jobspy pipelines, hitting LinkedIn rate
    limits and racing the DB writers.

    Fix: O_CREAT|O_EXCL atomic create. Exactly one thread acquires; the rest
    raise ScrapeInFlightError immediately."""
    import threading
    import time

    barrier = threading.Barrier(5)
    succeeded = []
    inflight_rejected = []

    def hit():
        barrier.wait()
        try:
            with _scrape_lock(tmp_path):
                # Hold the critical section briefly so concurrent attempts
                # actually overlap with us.
                time.sleep(0.05)
                succeeded.append(threading.get_ident())
        except ScrapeInFlightError:
            inflight_rejected.append(threading.get_ident())

    threads = [threading.Thread(target=hit) for _ in range(5)]
    for t in threads: t.start()
    for t in threads: t.join()

    assert len(succeeded) == 1, (
        f"Exactly ONE thread must acquire under contention "
        f"(got {len(succeeded)} succeeded / {len(inflight_rejected)} rejected)"
    )
    assert len(inflight_rejected) == 4, (
        f"Other 4 threads must be rejected with ScrapeInFlightError "
        f"(got {len(inflight_rejected)})"
    )
    assert not (tmp_path / _LOCK_NAME).exists(), "lock leaked after exit"
