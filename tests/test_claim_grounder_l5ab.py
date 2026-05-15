"""Tests for ADR-0015 L5a/L5b split — numeric drift detection.

L5a = renderdrift (LLM sampling jitter, snap-back-able)
L5b = substantive fabrication (no near match, hard block)

Threshold = 20% relative diff. Below threshold and a near-match exists → L5a.
Above threshold or no source numbers → L5b.
"""
from __future__ import annotations

import pytest

from compass.models import ResumeFact
from compass.services.claim_grounder import (
    L5Violation,
    _check_numeric_drift,
    _extract_numbers,
    screen_for_l5,
)


def _fact(text: str) -> ResumeFact:
    f = ResumeFact(id="f1", identity_id="i1", kind="experience", text=text)
    return f


# ─── _extract_numbers low-level ───────────────────────────


def test_extract_numbers_picks_up_percentages():
    nums = _extract_numbers("reduced false positives by 23%")
    assert (23.0, "23%") in nums


def test_extract_numbers_skips_4digit_years():
    nums = _extract_numbers("worked at Siemens 2020-2024")
    assert all(token not in ("2020", "2024") for _, token in nums)


def test_extract_numbers_handles_multiplier_units():
    nums = _extract_numbers("scaled to 5K users with 10x throughput")
    values = sorted(v for v, _ in nums)
    assert 5000.0 in values
    assert 10.0 in values  # 10x kept as bare value


def test_extract_numbers_handles_thousands_separator():
    """Bug 5-11: '1,500 users' was split into [1, 500] causing L5b false positive."""
    nums = _extract_numbers("scaled to 1,500 users")
    values = [v for v, _ in nums]
    assert 1500.0 in values
    assert 1.0 not in values
    assert 500.0 not in values


def test_extract_numbers_handles_currency_format():
    nums = _extract_numbers("ARR grew to $50,000 in Q3")
    values = [v for v, _ in nums]
    assert 50000.0 in values


def test_extract_numbers_handles_million_with_commas():
    nums = _extract_numbers("processed 1,000,000 records")
    values = [v for v, _ in nums]
    assert 1_000_000.0 in values


def test_extract_numbers_handles_decimal_with_thousands():
    nums = _extract_numbers("revenue 12,345.67 USD")
    values = [v for v, _ in nums]
    assert 12345.67 in values


def test_l5_no_false_positive_on_thousands_vs_plain():
    """Source '1,500' vs variant '1500' should be exact-match, not L5b."""
    fact = _fact("Scaled platform to 1,500 users")
    violation = _check_numeric_drift(
        "Scaled platform to 1500 users",
        [fact],
    )
    assert violation is None  # both → 1500.0


# ─── _check_numeric_drift direct ──────────────────────────


def test_l5a_close_drift_under_threshold():
    """22 vs 23 (~4% off) → L5a renderdrift."""
    fact = _fact("Reduced false positives by 23%")
    violation = _check_numeric_drift(
        "Reduced false positives by 22%",
        [fact],
    )
    assert violation is not None
    assert violation.kind == "L5a"
    assert violation.auto_fix == "23%"
    assert violation.bad_token == "22%"


def test_l5b_far_drift_above_threshold():
    """50 vs 23 (>100% off) → L5b fabrication."""
    fact = _fact("Reduced false positives by 23%")
    violation = _check_numeric_drift(
        "Reduced false positives by 50%",
        [fact],
    )
    assert violation is not None
    assert violation.kind == "L5b"
    assert violation.auto_fix is None


def test_l5b_when_source_has_no_numbers():
    fact = _fact("Built ML pipeline at Siemens")  # no numbers
    violation = _check_numeric_drift(
        "Built ML pipeline at Siemens, reducing false positives by 23%",
        [fact],
    )
    assert violation is not None
    assert violation.kind == "L5b"


def test_no_violation_when_numbers_match_exactly():
    fact = _fact("Reduced false positives by 23%")
    assert _check_numeric_drift("Reduced FPs by 23%", [fact]) is None


def test_no_violation_when_variant_has_no_numbers():
    fact = _fact("Reduced false positives by 23% over 6 months")
    assert _check_numeric_drift("Improved fraud detection significantly", [fact]) is None


def test_year_numbers_ignored_by_drift():
    """2020-2024 are years, handled separately by sensitive-date branch.
    The drift detector should not fire on them."""
    fact = _fact("Built ETL at Acme 2020-2024")
    # Variant changes employment dates — drift detector ignores; sensitive branch catches it
    violation = _check_numeric_drift(
        "Built ETL at Acme 2018-2024",
        [fact],
    )
    assert violation is None  # drift detector punts; sensitive_category check catches it


# ─── End-to-end via screen_for_l5 ─────────────────────────


def test_screen_for_l5_returns_l5a_for_minor_drift():
    fact = _fact("Reduced false positives by 23% on fraud detection")
    v = screen_for_l5(
        "Reduced false positives by 22% on fraud detection",
        [fact],
    )
    assert v is not None
    assert v.kind == "L5a"
    assert v.bad_token == "22%"
    assert v.auto_fix == "23%"


def test_screen_for_l5_returns_l5b_for_fabricated_metric():
    fact = _fact("Built ML pipeline at Siemens")  # no metrics
    v = screen_for_l5(
        "Built ML pipeline at Siemens, achieving 99.5% accuracy",
        [fact],
    )
    assert v is not None
    assert v.kind == "L5b"


def test_existing_l5_categories_default_to_l5b():
    """Backward-compat: language / degree / title violations remain L5b."""
    fact = _fact("German basic")
    v = screen_for_l5("German B1", [fact])
    assert v is not None
    assert v.kind == "L5b"  # default
    assert v.rule == "language_level_introduction"


# ─── save_variant integration ─────────────────────────────


def test_save_variant_l5a_auto_corrects(session, identity, job):
    """L5a should NOT raise — variant gets snapped back, ReflectionEvent logged."""
    from compass.models import FactVariant, ReflectionEvent
    from compass.services.claim_grounder import save_variant

    fact = ResumeFact(
        identity_id=identity.id,
        kind="experience",
        text="Reduced false positives by 23%",
    )
    session.add(fact)
    session.flush()

    saved = save_variant(
        session,
        fact=fact,
        job_id=job.id,
        variant_text="Reduced false positives by 22%",
    )
    session.flush()

    # Variant text was auto-corrected back to 23%
    assert "23%" in saved.text
    assert "22%" not in saved.text

    # An l5a_drift_corrected event was written
    events = (
        session.query(ReflectionEvent)
        .filter_by(kind="l5a_drift_corrected", related_fact_id=fact.id)
        .all()
    )
    assert len(events) == 1
    assert events[0].payload_json["bad_token"] == "22%"
    assert events[0].payload_json["auto_fix"] == "23%"


def test_save_variant_l5b_still_raises(session, identity, job):
    """L5b (substantive fabrication) keeps the original hard-block behavior."""
    from compass.services.claim_grounder import ClaimGroundingError, save_variant

    fact = ResumeFact(
        identity_id=identity.id,
        kind="experience",
        text="Built ML pipeline at Siemens",
    )
    session.add(fact)
    session.flush()

    with pytest.raises(ClaimGroundingError):
        save_variant(
            session,
            fact=fact,
            job_id=job.id,
            variant_text="Built ML pipeline at Siemens, achieving 99.5% accuracy",
        )
