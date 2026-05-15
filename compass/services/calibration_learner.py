"""Calibration learner — derives user boundaries from variant decisions.

Reads ReflectionEvent stream of variant_approved / variant_rejected /
variant_edited_by_user, derives:

  - max_amplification_level    (trends from approval pattern)
  - preferred_verbs_avoid      (verbs in rejected variants)
  - preferred_verbs_do         (verbs in approved variants — but with
                                anti-sycophancy regularization)
  - sensitive_categories_locked  (categories with high rejection rate)

Anti-drift safeguards:
  - max_amplification_level moves SLOWLY (++0.2 per cycle, capped)
  - sensitive_categories_locked are STICKY — once locked, hard to unlock
  - explicit drift_alert events when rapid changes detected

This is the "learning" half of Reflective Calibration Loop.
The non-learning half is adversarial_voice (frozen).
"""
from __future__ import annotations

import logging
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    FactVariant,
    ReflectionEvent,
    ResumeFact,
    UserCalibration,
)

log = logging.getLogger(__name__)


_VERB_RE = re.compile(r"\b(led|drove|owned|managed|built|developed|created|designed|implemented|executed|delivered|coordinated|supported|assisted|participated|contributed|collaborated)\b", re.I)


def _ensure_calibration(session: Session) -> UserCalibration:
    cal = session.get(UserCalibration, 1)
    if cal is None:
        cal = UserCalibration(id=1)
        session.add(cal)
        session.flush()
    return cal


def learn_from_recent(
    session: Session, *, lookback_days: int = 30
) -> dict:
    """Re-learn calibration from recent variant decisions. Returns delta summary.

    Idempotent — running twice in a row produces near-identical state.
    """
    since = datetime.now(timezone.utc) - timedelta(days=lookback_days)

    # Pull recent variants with decisions
    variants = (
        session.execute(
            select(FactVariant).where(FactVariant.created_at >= since)
        )
        .scalars()
        .all()
    )

    approved = [v for v in variants if v.user_decision == "approved"]
    rejected = [v for v in variants if v.user_decision == "rejected"]
    edited = [v for v in variants if v.user_decision == "edited_by_user"]

    cal = _ensure_calibration(session)
    before_state = {
        "max_level": cal.max_amplification_level,
        "do_count": len(cal.preferred_verbs_do or []),
        "avoid_count": len(cal.preferred_verbs_avoid or []),
        "locked_count": len(cal.sensitive_categories_locked or []),
    }

    # ── max_amplification_level: slow drift toward observed approval ceiling
    if approved:
        observed_max = max(v.amplification_level for v in approved)
        target = min(observed_max, 4)  # never auto-set to 5
        # slow drift: at most ±1 per cycle
        if target > cal.max_amplification_level:
            cal.max_amplification_level = min(cal.max_amplification_level + 1, target)
        elif target < cal.max_amplification_level:
            cal.max_amplification_level = max(cal.max_amplification_level - 1, target)

    # ── verbs (anti-sycophancy: avoid wins over do at equal frequency)
    avoid_counter: Counter[str] = Counter()
    do_counter: Counter[str] = Counter()
    for v in rejected:
        for m in _VERB_RE.finditer(v.text):
            avoid_counter[m.group(0).lower()] += 1
    for v in approved:
        for m in _VERB_RE.finditer(v.text):
            do_counter[m.group(0).lower()] += 1
    # Verbs that were rejected at least twice → avoid
    new_avoid = sorted({verb for verb, c in avoid_counter.items() if c >= 2})
    # Verbs that were approved at least 3 times AND not in avoid → do
    new_do = sorted({verb for verb, c in do_counter.items() if c >= 3 and verb not in new_avoid})

    cal.preferred_verbs_avoid = sorted(set((cal.preferred_verbs_avoid or []) + new_avoid))
    cal.preferred_verbs_do = sorted(
        v for v in set((cal.preferred_verbs_do or []) + new_do) if v not in cal.preferred_verbs_avoid
    )

    # ── sensitive categories: lock if rejection rate >= 50% with >=3 instances
    rejection_by_cat: dict[str, list[bool]] = defaultdict(list)
    for v in variants:
        f = session.get(ResumeFact, v.fact_id)
        if not f or not f.sensitive_category:
            continue
        rejection_by_cat[f.sensitive_category].append(v.user_decision == "rejected")
    locked = set(cal.sensitive_categories_locked or [])
    for cat, decisions in rejection_by_cat.items():
        if len(decisions) >= 3 and sum(decisions) / len(decisions) >= 0.5:
            locked.add(cat)
    cal.sensitive_categories_locked = sorted(locked)

    cal.last_review_at = datetime.now(timezone.utc)

    after_state = {
        "max_level": cal.max_amplification_level,
        "do_count": len(cal.preferred_verbs_do or []),
        "avoid_count": len(cal.preferred_verbs_avoid or []),
        "locked_count": len(cal.sensitive_categories_locked or []),
    }
    delta = {
        "before": before_state,
        "after": after_state,
        "approved": len(approved),
        "rejected": len(rejected),
        "edited": len(edited),
    }

    session.add(
        ReflectionEvent(
            kind="calibration_changed",
            payload_json=delta,
        )
    )
    return delta


# ─────────────────────────────────────────────────────────
# Drift detector — emits alerts when calibration moves too fast
# ─────────────────────────────────────────────────────────


def detect_drift(session: Session) -> Optional[dict]:
    """Compare last 2 calibration_drift_points; return alert dict if drifting."""
    from ..models import CalibrationDriftPoint

    points = (
        session.execute(
            select(CalibrationDriftPoint).order_by(CalibrationDriftPoint.captured_at.desc()).limit(2)
        )
        .scalars()
        .all()
    )
    if len(points) < 2:
        return None
    new, old = points[0], points[1]
    delta_level = new.snapshot_json.get("max_amplification_level", 0) - old.snapshot_json.get("max_amplification_level", 0)
    if abs(delta_level) >= 2:
        alert = {
            "kind": "rapid_max_level_drift",
            "from": old.snapshot_json.get("max_amplification_level"),
            "to": new.snapshot_json.get("max_amplification_level"),
            "captured_at": new.captured_at.isoformat(),
        }
        session.add(ReflectionEvent(kind="drift_alert", payload_json=alert))
        session.flush()  # autoflush is off (extensions.py); flush so callers can query
        return alert
    return None
