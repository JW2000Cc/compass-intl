"""Tests for tier_classifier — focus on the parts that don't need a real LLM:
parsing/coercion + no-LLM fallback + reflection-event logging."""
from compass.models import Job, JobMatch, ReflectionEvent
from compass.services.tier_classifier import (
    DEFAULT_QUOTAS,
    _coerce_score,
    _coerce_tier,
    classify_job,
    daily_push_distribution,
)


def test_tier_coercion_clamps_out_of_range():
    assert _coerce_tier(0) == 1
    assert _coerce_tier(6) == 5
    assert _coerce_tier(-1) == 1
    assert _coerce_tier("garbage") == 5  # safe default
    assert _coerce_tier(None) == 5
    assert _coerce_tier(3) == 3


def test_score_coercion_clamps_to_0_10():
    assert _coerce_score(-5) == 0
    assert _coerce_score(15) == 10
    assert _coerce_score("garbage") == 0
    assert _coerce_score(None) == 0
    assert _coerce_score(7) == 7


def test_default_push_distribution_matches_quotas():
    """The 70/20/10 ε-exploration is the heart of anti-bubble. Don't drift."""
    dist = daily_push_distribution(total_slots=100)
    assert dist[1] == 70  # core
    assert dist[2] == 20  # anti-bubble sweet spot
    assert dist[3] == 10  # learnable
    assert dist[4] == 0   # only on user-signal
    assert dist[5] == 0   # NEVER push


def test_quotas_sum_to_100_pct():
    """Sanity: tiers 1-3 quotas should fully account for pushable allocation."""
    pushable_sum = sum(DEFAULT_QUOTAS[t] for t in (1, 2, 3))
    assert pushable_sum == 1.0


def test_no_llm_falls_back_to_tier_5(session):
    """Compass refuses to push without LLM — defaults Tier 5 (silently archive)."""
    j = Job(id="j1", title="Bartender", company="Marriott", link="http://x")
    session.add(j)
    session.flush()

    match = classify_job(session, j, provider="claude", api_key="", model="x")
    session.flush()

    assert match.tier == 5
    assert match.score == 0
    assert "no_llm" in (match.model or "").lower() or "error" in (match.reason or "").lower()

    # Should also emit a reflection event
    events = session.query(ReflectionEvent).all()
    assert any("no_llm" in e.kind for e in events)


def test_engineer_to_bartender_blocked_at_tier_5(session):
    """The 'engineer ≠ bartender' hard rule (legacy 'bartender bug' fix from Job Mate).

    We can't test the LLM directly (no API in unit test) but we verify the
    structural defense: when no LLM available, we **never** push it as Tier 1-3.
    """
    j = Job(id="j2", title="Bartender at Hotel", company="Marriott", link="http://x")
    session.add(j)
    session.flush()

    match = classify_job(session, j, provider="claude", api_key="", model="x")
    assert match.tier == 5  # the only safe default

    # And it persists to JobMatch
    persisted = session.query(JobMatch).filter_by(job_id="j2").first()
    assert persisted is not None
    assert persisted.tier == 5
