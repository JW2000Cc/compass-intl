"""Integration tests for ADR-0016: scraper.merge_into_db gating + matched_profile_id."""
from __future__ import annotations

import pytest

from compass.models import Job, ReflectionEvent
from compass.services.scrape_config import (
    Dealbreakers,
    GlobalIntent,
    ProfileConfig,
)
from compass.services.scraper import merge_into_db


_link_counter = [0]


def _payload(**kw) -> dict:
    """jobspy-shaped payload (not a Job ORM object — pre-merge stage)."""
    _link_counter[0] += 1
    base = {
        "title": "ML Engineer",
        "company": "ACME",
        "link": f"https://example.com/job/{_link_counter[0]}",
        "location": "Berlin, Germany",
        "source": "linkedin",
        "description": "ML role with Python.",
        "is_remote": False,
    }
    base.update(kw)
    return base


def _german_profile() -> ProfileConfig:
    return ProfileConfig(
        id="de-electrical",
        label="德国-电气",
        city="Berlin",
        country="Germany",
        keywords=["ML Engineer", "Power Engineer"],
        dealbreakers=Dealbreakers(country_must_be=["Germany"]),
    )


# ─── Without profile: backward-compatible old behavior ────


def test_merge_without_profile_no_gate_filtering(session):
    """Pre-ADR-0016 callers (no profile) should see no behavior change."""
    payloads = [
        _payload(title="ML Engineer", location="Milano, Italy"),
        _payload(title="ML Engineer", location="Berlin, Germany"),
    ]
    n_new, n_upd, n_blk, n_gate = merge_into_db(session, payloads)
    assert n_new == 2  # Italy job NOT filtered when no profile context
    assert n_gate == 0
    session.flush()
    italian = session.query(Job).filter(Job.location == "Milano, Italy").one()
    assert italian.matched_profile_id is None  # no profile = no tag


# ─── With profile: gate kicks in ──────────────────────────


def test_merge_with_profile_drops_italian_job(session):
    """The user's actual痛点: Italian job leaks into German search."""
    p = _german_profile()
    payloads = [
        _payload(title="ML Engineer", location="Milano, Italy"),
        _payload(title="ML Engineer", location="Berlin, Germany"),
    ]
    n_new, _, _, n_gate = merge_into_db(session, payloads, profile=p)
    assert n_new == 1
    assert n_gate == 1
    session.flush()
    saved = session.query(Job).all()
    assert len(saved) == 1
    assert "Berlin" in saved[0].location


def test_merge_sets_matched_profile_id_on_new(session):
    p = _german_profile()
    payloads = [_payload(title="ML Engineer", location="Berlin, Germany")]
    merge_into_db(session, payloads, profile=p)
    session.flush()
    saved = session.query(Job).one()
    assert saved.matched_profile_id == "de-electrical"


def test_merge_writes_hard_gate_blocked_event(session):
    p = _german_profile()
    payloads = [_payload(location="Milano, Italy")]
    merge_into_db(session, payloads, profile=p)
    session.flush()
    events = session.query(ReflectionEvent).filter_by(kind="hard_gate_blocked").all()
    assert len(events) == 1
    assert events[0].payload_json["profile_id"] == "de-electrical"
    assert any("country" in r for r in events[0].payload_json["blocked_by"])


def test_merge_filters_off_topic_role_via_active_keywords(session):
    """Bartender job at correct location still blocked by active_keywords."""
    p = _german_profile()  # keywords=[ML Engineer, Power Engineer]
    payloads = [
        _payload(
            title="Bartender",
            location="Berlin, Germany",
            description="Restaurant night shift, no engineering.",
        )
    ]
    n_new, _, _, n_gate = merge_into_db(session, payloads, profile=p)
    assert n_new == 0
    assert n_gate == 1


def test_merge_lazy_backfills_profile_id_on_existing(session):
    """A job from a pre-ADR-0016 ad-hoc scrape (no profile_id) should get
    tagged when later recalled by a profile-aware scrape."""
    # First merge: legacy ad-hoc, no profile
    pl = _payload(title="ML Engineer", location="Berlin, Germany")
    merge_into_db(session, [pl])
    session.flush()
    job = session.query(Job).one()
    assert job.matched_profile_id is None

    # Second merge: same job, this time with profile
    p = _german_profile()
    merge_into_db(session, [pl], profile=p)
    session.flush()
    session.refresh(job)
    assert job.matched_profile_id == "de-electrical"


def test_merge_with_global_exclude_categories(session):
    """global.exclude_categories applies even when profile blacklist is empty."""
    p = _german_profile()
    g = GlobalIntent(exclude_categories=["bartender"])
    payloads = [
        _payload(
            title="Senior Bartender",
            location="Berlin, Germany",
            description="Mixology required.",
        ),
        _payload(title="ML Engineer", location="Berlin, Germany"),
    ]
    n_new, _, _, n_gate = merge_into_db(session, payloads, profile=p, global_intent=g)
    assert n_new == 1
    assert n_gate == 1


def test_merge_blacklist_legacy_path_still_works(session):
    """Old blacklist arg still works alongside the new profile gate."""
    p = _german_profile()
    blacklist = {"companies": ["BadCorp"], "keywords": []}
    payloads = [
        _payload(company="BadCorp", location="Berlin, Germany"),
        _payload(company="GoodCorp", location="Berlin, Germany"),
    ]
    n_new, _, n_blk, n_gate = merge_into_db(
        session, payloads, blacklist=blacklist, profile=p
    )
    # Legacy blacklist runs BEFORE gate, so BadCorp counts as blacklist-blocked
    assert n_blk == 1
    assert n_new == 1


# ─── Multi-profile dedupe (5-08 IntegrityError fix) ────────


def test_same_url_in_two_merge_calls_doesnt_collide_at_commit(session):
    """5-08 bug: when profile A scraped a job, then profile B scraped the same
    URL within the same session_scope, both got existing=None from session.get()
    (pending objects not in identity map) and called session.add() — at commit
    time SQLite raised UNIQUE constraint failed: jobs.id.

    Fix: merge_into_db flushes after adds, and walks session.new to dedupe."""
    same_link = "https://example.com/job/popular-role"
    p_de = _german_profile()
    # Profile A inserts the job
    n_new1, _, _, _ = merge_into_db(
        session,
        [_payload(title="Senior ML Engineer", link=same_link, location="Berlin, Germany")],
        profile=p_de,
    )
    assert n_new1 == 1
    # Profile B sees the same URL (e.g. via Frankfurt search returning a Berlin job)
    p_de2 = ProfileConfig(
        id="de-fra",
        label="DE-Frankfurt",
        city="Frankfurt",
        country="Germany",
        keywords=["ML Engineer"],
        dealbreakers=Dealbreakers(country_must_be=["Germany"]),
    )
    n_new2, n_upd2, _, _ = merge_into_db(
        session,
        [_payload(title="Senior ML Engineer", link=same_link, location="Berlin, Germany")],
        profile=p_de2,
    )
    # Without the fix this would either still increment n_new (causing UNIQUE
    # collision at commit) or session.flush would raise mid-call. With the fix
    # the second call detects the existing flushed row and counts as update.
    assert n_new2 == 0
    assert n_upd2 == 1
    # Commit succeeds (no IntegrityError)
    session.commit()


def test_same_url_in_one_batch_dedupes(session):
    """Two payloads with identical URL in a single merge_into_db call should
    only insert once. Pre-fix would session.add twice and explode at commit."""
    p = _german_profile()
    link = "https://example.com/job/dup-in-batch"
    payloads = [
        _payload(title="ML Engineer", link=link, location="Berlin, Germany"),
        _payload(title="ML Engineer", link=link, location="Berlin, Germany"),
    ]
    n_new, n_upd, _, _ = merge_into_db(session, payloads, profile=p)
    assert n_new == 1
    assert n_upd == 1  # second one counted as duplicate-update
    session.commit()
