"""Tests for dealbreaker_gate — deterministic hard filter (ADR-0016)."""
from __future__ import annotations

import pytest

from compass.services.dealbreaker_gate import (
    GateDecision,
    check_job,
    detect_jd_language,
    filter_jobs,
)
from compass.services.scrape_config import (
    Blacklist,
    Dealbreakers,
    GlobalIntent,
    ProfileConfig,
    TierQuota,
)


def _profile(**kw) -> ProfileConfig:
    """Test helper — builds a Berlin/Germany profile with overrides."""
    defaults = dict(
        id="de-test",
        label="Test",
        city="Berlin",
        country="Germany",
        keywords=["ML Engineer", "Data Engineer"],
        dealbreakers=Dealbreakers(country_must_be=["Germany"]),
    )
    defaults.update(kw)
    return ProfileConfig(**defaults)


def _job(**kw) -> dict:
    """Test helper — dict-style job (also valid input to gate)."""
    base = {
        "title": "ML Engineer",
        "company": "ACME",
        "location": "Berlin, Germany",
        "description": "Looking for ML Engineer with Python experience.",
        "is_remote": False,
    }
    base.update(kw)
    return base


# ─── detect_jd_language ───────────────────────────────────


def test_detect_zh_via_cjk():
    txt = "我们正在寻找一位经验丰富的工程师加入团队负责机器学习项目"
    assert detect_jd_language(txt) == "zh"


def test_detect_it_via_stopwords():
    txt = (
        "Cerchiamo un Machine Learning Engineer per la nostra azienda. "
        "Esperienza con Python e sviluppo di modelli. La posizione è "
        "presso il nostro ufficio della società. Esperienza richiesta."
    )
    assert detect_jd_language(txt) == "it"


def test_detect_de_via_stopwords():
    txt = (
        "Wir suchen einen ML Engineer für unser Team. Du wirst mit Python "
        "arbeiten und musst Erfahrung mit der Modellentwicklung haben. "
        "Du arbeitest mit dem Team und für das Unternehmen."
    )
    assert detect_jd_language(txt) == "de"


def test_detect_returns_none_for_short_or_ambiguous():
    assert detect_jd_language("") is None
    assert detect_jd_language("Engineer") is None  # too short / English-ambiguous


# ─── country gate ─────────────────────────────────────────


def test_country_gate_passes_when_match():
    decision = check_job(_job(), _profile())
    assert decision.allow is True
    assert decision.blocked_by == []


def test_country_gate_blocks_italian_job_for_german_profile():
    """The bug from user — Italian job leaks into German search."""
    italian = _job(location="Milano, Italy")
    decision = check_job(italian, _profile())
    assert decision.allow is False
    assert any("country_mismatch" in r for r in decision.blocked_by)


def test_country_gate_passthrough_when_no_country_must_be():
    p = _profile(dealbreakers=Dealbreakers())  # no country gate
    decision = check_job(_job(location="Anywhere"), p)
    assert decision.allow is True


def test_remote_in_region_accepted_when_allowed():
    p = _profile(
        dealbreakers=Dealbreakers(
            country_must_be=["Germany"], allow_remote_in_region=True
        )
    )
    remote_job = _job(location="Remote (EU)", is_remote=True)
    decision = check_job(remote_job, p)
    assert decision.allow is True
    assert any("remote" in n.lower() for n in decision.notes)


def test_remote_blocked_when_disallowed():
    p = _profile(
        dealbreakers=Dealbreakers(
            country_must_be=["Germany"], allow_remote_in_region=False
        )
    )
    remote_job = _job(location="Remote (EU)", is_remote=True)
    decision = check_job(remote_job, p)
    assert decision.allow is False


# ─── country soft-match via profile.city (LinkedIn no-country case) ──


def test_country_inferred_from_city_when_country_omitted_in_location():
    """Real-world LinkedIn case: location='Frankfurt am Main, Hesse' (no Germany).
    Profile city=Frankfurt + country=Germany should soft-match, not block."""
    p = _profile(city="Frankfurt", country="Germany")
    job = _job(location="Frankfurt am Main, Hesse")
    decision = check_job(job, p)
    assert decision.allow is True, decision.blocked_by
    assert any("city" in n.lower() for n in decision.notes)


def test_country_city_inference_does_not_cross_countries():
    """Italian Frankfurt-am-Oder shouldn't pass for Italy profile by accident."""
    p = _profile(city="Berlin", country="Germany")
    job = _job(location="Milano, Italy")  # contains "Italy" but no Berlin
    decision = check_job(job, p)
    assert decision.allow is False  # city mismatch + country mismatch


def test_country_inference_requires_profile_country_in_must_be():
    """If profile.country differs from country_must_be, don't soft-match."""
    p = _profile(
        city="Frankfurt", country="Austria",  # mismatched
        dealbreakers=Dealbreakers(country_must_be=["Germany"]),
    )
    job = _job(location="Frankfurt am Main")  # ambiguous
    decision = check_job(job, p)
    assert decision.allow is False


def test_country_inference_falls_back_to_remote_when_city_absent():
    """City not present in location, but remote+allow_remote → still pass."""
    p = _profile(
        city="Frankfurt", country="Germany",
        dealbreakers=Dealbreakers(
            country_must_be=["Germany"], allow_remote_in_region=True,
        ),
    )
    job = _job(location="Anywhere", is_remote=True)
    decision = check_job(job, p)
    assert decision.allow is True
    assert any("remote" in n.lower() for n in decision.notes)


# ─── language gate ────────────────────────────────────────


def test_blocks_italian_jd_when_language_blocked():
    p = _profile(
        dealbreakers=Dealbreakers(
            country_must_be=["Germany"], language_blocked=["it"]
        )
    )
    italian_jd = _job(
        location="Berlin, Germany",  # location passes
        description=(
            "Cerchiamo un ML Engineer per la nostra società. "
            "Esperienza con Python e sviluppo di modelli della Machine Learning. "
            "La posizione è presso questo ufficio."
        ),
    )
    decision = check_job(italian_jd, p)
    assert decision.allow is False
    assert any("language_blocked" in r for r in decision.blocked_by)


def test_no_language_block_when_not_in_blocklist():
    p = _profile()  # no language_blocked
    italian_jd = _job(description="Cerchiamo della società per progetti.")
    decision = check_job(italian_jd, p)
    # Italian content but no block configured → passes language gate
    assert "language_blocked" not in " ".join(decision.blocked_by)


# ─── blacklist gates ──────────────────────────────────────


def test_company_blacklist_blocks():
    p = _profile(blacklist=Blacklist(companies=["ACME"]))
    decision = check_job(_job(company="ACME Corp"), p)
    assert decision.allow is False
    assert any("company_blacklisted" in r for r in decision.blocked_by)


def test_keyword_blacklist_blocks_in_title():
    p = _profile(blacklist=Blacklist(keywords=["HVAC"]))
    decision = check_job(_job(title="HVAC ML Engineer"), p)
    assert decision.allow is False
    assert any("keyword_blacklisted" in r for r in decision.blocked_by)


def test_global_exclude_categories_block_across_profiles():
    g = GlobalIntent(exclude_categories=["bartender"])
    decision = check_job(
        _job(title="Bartender / Server", description="ML engineer not"),
        _profile(),
        global_=g,
    )
    assert decision.allow is False


# ─── active keyword gate ─────────────────────────────────


def test_blocks_when_no_keyword_matches():
    p = _profile(keywords=["Power Engineer", "Electrical Engineer"])
    decision = check_job(_job(title="Bartender", description="serving drinks"), p)
    assert decision.allow is False
    assert any("no_keyword_match" in r for r in decision.blocked_by)


def test_allows_when_at_least_one_keyword_matches():
    p = _profile(keywords=["Power Engineer", "ML Engineer"])
    decision = check_job(
        _job(title="Senior ML Engineer", description="Python experience required"),
        p,
    )
    assert decision.allow is True


def test_keyword_aliases_count_too():
    p = _profile(
        keywords=["Power Engineer"],
        keyword_aliases={"Power Engineer": ["Elektroingenieur"]},
    )
    decision = check_job(
        _job(title="Elektroingenieur", description="Berlin"), p
    )
    assert decision.allow is True


def test_empty_keywords_means_any_role_allowed():
    p = _profile(keywords=[])
    decision = check_job(_job(title="Random Role"), p)
    assert decision.allow is True


# ─── multi-rule blocking ─────────────────────────────────


def test_multiple_block_reasons_accumulate():
    """Italian job at blacklisted company in profile that also wants only ML roles."""
    p = _profile(
        blacklist=Blacklist(companies=["BadCorp"]),
        keywords=["ML Engineer"],
    )
    bad_job = _job(
        title="Bartender",  # fails active_keyword
        company="BadCorp Italia",  # fails company blacklist
        location="Milano, Italy",  # fails country gate
        description="Serving drinks all night, no engineering required.",
    )
    decision = check_job(bad_job, p)
    assert decision.allow is False
    assert len(decision.blocked_by) >= 3


# ─── filter_jobs convenience ─────────────────────────────


def test_filter_jobs_partitions():
    p = _profile()
    jobs = [
        _job(title="ML Engineer", location="Berlin, Germany"),     # pass
        _job(title="ML Engineer", location="Milano, Italy"),       # block (country)
        _job(
            title="Bartender",
            location="Berlin, Germany",
            description="Serving drinks, restaurant night shift.",
        ),  # block (keyword)
        _job(title="Data Engineer", location="Frankfurt, Germany"),# pass
    ]
    passed, blocked = filter_jobs(jobs, p)
    assert len(passed) == 2
    assert len(blocked) == 2
    assert all(d.profile_id == p.id for d in blocked)


def test_filter_jobs_preserves_input_order():
    p = _profile()
    jobs = [
        _job(title=f"ML Engineer {i}", location="Berlin, Germany")
        for i in range(5)
    ]
    passed, _ = filter_jobs(jobs, p)
    assert [j["title"] for j in passed] == [j["title"] for j in jobs]


# ─── ORM-object compatibility ────────────────────────────


def test_works_with_orm_job_object(session, identity):
    """Gate must accept SQLAlchemy Job objects, not just dicts."""
    from compass.models import Job

    job = Job(
        title="ML Engineer",
        company="ACME",
        link="https://example.com/j",
        location="Milano, Italy",
        description="Rome / Milan based",
    )
    session.add(job)
    session.flush()
    p = _profile()
    decision = check_job(job, p)
    assert decision.allow is False
    assert any("country" in r for r in decision.blocked_by)
