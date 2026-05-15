"""Tests for calibration_learner — the **slow drift** safeguard.

Reflective Flywheel's anti-Engagement defense relies on calibration drifting
SLOWLY (±1 per cycle, not jumping). These tests verify that property holds."""
from datetime import datetime, timedelta, timezone

from compass.models import (
    CalibrationDriftPoint,
    FactVariant,
    ReflectionEvent,
    ResumeFact,
    UserCalibration,
)
from compass.services.calibration_learner import (
    _ensure_calibration,
    detect_drift,
    learn_from_recent,
)


def _add_variant(session, decision: str, level: int, fact_id: str = "f1", text: str = "led the team") -> FactVariant:
    v = FactVariant(
        fact_id=fact_id,
        text=text,
        amplification_level=level,
        user_decision=decision,
    )
    session.add(v)
    session.flush()
    return v


def test_max_level_drifts_at_most_one_per_cycle(session):
    """Even if user approves L4, calibration only goes from 2 → 3, not 2 → 4."""
    cal = _ensure_calibration(session)
    cal.max_amplification_level = 2
    session.flush()

    # User approves several L4 variants — extreme case
    for _ in range(5):
        _add_variant(session, decision="approved", level=4)

    delta = learn_from_recent(session, lookback_days=30)
    cal_after = session.get(UserCalibration, 1)

    # Drift should be at most +1 (slow drift safeguard)
    assert cal_after.max_amplification_level <= 3
    assert delta["before"]["max_level"] == 2
    assert delta["after"]["max_level"] <= 3


def test_max_level_never_auto_promotes_to_5(session):
    """L5 is the hard ban. Calibration should never set max_level to 5 even
    if every approval is L5 (which shouldn't happen anyway because save_variant
    blocks L5 — but defense in depth)."""
    cal = _ensure_calibration(session)
    cal.max_amplification_level = 4
    session.flush()

    # Hypothetically — variants at level 5 (in practice these never get saved)
    for _ in range(5):
        _add_variant(session, decision="approved", level=5)

    learn_from_recent(session, lookback_days=30)
    cal_after = session.get(UserCalibration, 1)
    assert cal_after.max_amplification_level <= 4  # capped


def test_rejected_verbs_get_locked_to_avoid_list(session):
    """If user rejects 'led' twice in variants, calibration should learn to avoid it."""
    cal = _ensure_calibration(session)
    cal.preferred_verbs_avoid = []
    session.flush()

    _add_variant(session, decision="rejected", level=3, text="Led the platform migration")
    _add_variant(session, decision="rejected", level=3, text="Led customer-facing analytics")

    learn_from_recent(session, lookback_days=30)
    cal_after = session.get(UserCalibration, 1)
    assert "led" in cal_after.preferred_verbs_avoid


def test_drift_alert_triggers_on_jump(session):
    """If calibration jumps 2 levels between snapshots, raise drift_alert."""
    now = datetime.now(timezone.utc)
    session.add(CalibrationDriftPoint(
        snapshot_json={"max_amplification_level": 1},
        captured_at=now - timedelta(days=14),
    ))
    session.add(CalibrationDriftPoint(
        snapshot_json={"max_amplification_level": 4},  # jumped 3
        captured_at=now,
    ))
    session.flush()

    alert = detect_drift(session)
    assert alert is not None
    assert alert["kind"] == "rapid_max_level_drift"

    # Should also emit a ReflectionEvent
    events = session.query(ReflectionEvent).filter_by(kind="drift_alert").all()
    assert len(events) >= 1


def test_no_drift_alert_when_calibration_stable(session):
    """No alert when changes are within slow-drift band."""
    now = datetime.now(timezone.utc)
    session.add(CalibrationDriftPoint(
        snapshot_json={"max_amplification_level": 2},
        captured_at=now - timedelta(days=14),
    ))
    session.add(CalibrationDriftPoint(
        snapshot_json={"max_amplification_level": 3},  # +1 = slow drift OK
        captured_at=now,
    ))
    session.flush()

    alert = detect_drift(session)
    assert alert is None


def test_learn_idempotent(session):
    """Running learn_from_recent twice in a row should produce same final state."""
    cal = _ensure_calibration(session)
    cal.max_amplification_level = 2
    session.flush()

    _add_variant(session, decision="approved", level=3)
    _add_variant(session, decision="rejected", level=3, text="Led the team")

    learn_from_recent(session, lookback_days=30)
    state_a = session.get(UserCalibration, 1).max_amplification_level

    learn_from_recent(session, lookback_days=30)
    state_b = session.get(UserCalibration, 1).max_amplification_level

    assert state_a == state_b  # idempotent
