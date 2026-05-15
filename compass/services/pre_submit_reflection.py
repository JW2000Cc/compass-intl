"""Pre-submit reflection — Adversarial Voice surfaced at high-commitment moments.

Why this exists:
    Variant-creation already runs Adversarial Voice (services/adversarial_voice.py).
    But objections live on individual variants, scattered across the resume workbench.
    By the time the user clicks "applied", they may have forgotten the objections that
    were raised when they accepted aggressive rewrites.

    This module aggregates the relevant signals at the *moment of high commitment*
    — clicking "applied" on a job — and surfaces them in one read-only screen.

    Frozen, never-trained reverse channel:
        * uses NO calibration data
        * uses NO learned preferences
        * shows the user the objections raised by the (frozen) Adversarial Voice
        * shows the user's value-regression baseline they may have drifted from
        * shows recent calibration drift trajectory

    Read-only friction. NEVER blocks. User sees and confirms or backs out.

This is the **submit-时反思摘要** that 件 4/6 of Stage 1 requested.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from ..models import (
    CalibrationDriftPoint,
    FactVariant,
    Job,
    JobMatch,
    ReflectionEvent,
    UserCalibration,
)


@dataclass
class ObjectionDigest:
    """Aggregated objections across recent variants user has approved."""
    count: int
    examples: list[str] = field(default_factory=list)  # up to 3
    high_level_count: int = 0  # variants level >= 3 with objection


@dataclass
class DriftDigest:
    """Has the calibration silently drifted since user last reviewed?"""
    delta_level: int  # current - first known
    days_span: int
    direction: str  # "loosening" | "tightening" | "stable"


@dataclass
class TierDigest:
    """How does this job's tier compare to the user's typical applied tier?"""
    this_tier: int | None
    typical_applied_tier: float | None  # mean
    is_outlier_high: bool  # this is a higher-stretch tier than usual
    is_outlier_low: bool   # this is below user's usual baseline


@dataclass
class PreSubmitReflection:
    """Full payload rendered in the confirm screen."""
    job_id: str
    job_title: str
    company: str
    objections: ObjectionDigest
    drift: DriftDigest | None
    tier: TierDigest
    days_since_value_review: int | None
    six_months_voice: str
    has_anything_to_say: bool


def build_reflection(session: Session, job_id: str) -> Optional[PreSubmitReflection]:
    """Build a reflection digest for a specific job at apply-time.

    Returns None if the job doesn't exist. Otherwise always returns a
    reflection — `has_anything_to_say` indicates whether it's worth showing.
    Caller decides whether to render or pass through.
    """
    job = session.get(Job, job_id)
    if job is None:
        return None

    # ── 1. Recent objections on user's approved variants ──
    since_objections = datetime.now(timezone.utc) - timedelta(days=21)
    variants = session.execute(
        select(FactVariant)
        .where(
            FactVariant.adversarial_objection.is_not(None),
            FactVariant.user_decision.in_(["approved", "edited_by_user"]),
            FactVariant.created_at >= since_objections,
        )
        .order_by(desc(FactVariant.created_at))
    ).scalars().all()

    examples = [v.adversarial_objection for v in variants[:3] if v.adversarial_objection]
    high_lvl = sum(1 for v in variants if (v.amplification_level or 0) >= 3)
    objections = ObjectionDigest(
        count=len(variants),
        examples=examples,
        high_level_count=high_lvl,
    )

    # ── 2. Calibration drift since baseline ──
    points = session.execute(
        select(CalibrationDriftPoint).order_by(CalibrationDriftPoint.captured_at.asc())
    ).scalars().all()
    drift: DriftDigest | None = None
    if len(points) >= 2:
        first_lvl = points[0].snapshot_json.get("max_amplification_level", 0)
        last_lvl = points[-1].snapshot_json.get("max_amplification_level", 0)
        delta = last_lvl - first_lvl
        first_at = points[0].captured_at
        last_at = points[-1].captured_at
        # captured_at may be naive; treat as UTC.
        if first_at.tzinfo is None:
            first_at = first_at.replace(tzinfo=timezone.utc)
        if last_at.tzinfo is None:
            last_at = last_at.replace(tzinfo=timezone.utc)
        days_span = max(1, (last_at - first_at).days)
        if delta >= 1:
            direction = "loosening"
        elif delta <= -1:
            direction = "tightening"
        else:
            direction = "stable"
        drift = DriftDigest(delta_level=delta, days_span=days_span, direction=direction)

    # ── 3. Tier comparison ──
    matches = session.execute(
        select(JobMatch).where(JobMatch.job_id == job_id).order_by(JobMatch.tier.asc())
    ).scalars().all()
    this_tier = matches[0].tier if matches else None

    # Mean applied tier across last ~90 days
    since_apply = datetime.now(timezone.utc) - timedelta(days=90)
    applied_jobs = session.execute(
        select(Job).where(
            Job.status.in_(["applied", "interview", "offer", "rejected"]),
            Job.created_at >= since_apply,
        )
    ).scalars().all()
    if applied_jobs:
        all_matches = session.execute(
            select(JobMatch).where(JobMatch.job_id.in_([j.id for j in applied_jobs]))
        ).scalars().all()
        # best (lowest) tier per job
        best: dict[str, int] = {}
        for m in all_matches:
            prev = best.get(m.job_id)
            if prev is None or m.tier < prev:
                best[m.job_id] = m.tier
        if best:
            typical = sum(best.values()) / len(best)
        else:
            typical = None
    else:
        typical = None

    is_outlier_high = (
        this_tier is not None and typical is not None and this_tier > typical + 0.7
    )
    is_outlier_low = (
        this_tier is not None and typical is not None and this_tier < typical - 0.7
    )
    tier_digest = TierDigest(
        this_tier=this_tier,
        typical_applied_tier=round(typical, 1) if typical is not None else None,
        is_outlier_high=is_outlier_high,
        is_outlier_low=is_outlier_low,
    )

    # ── 4. Days since value-regression review ──
    cal = session.get(UserCalibration, 1)
    days_since_review: int | None = None
    if cal and cal.last_review_at:
        last = cal.last_review_at if cal.last_review_at.tzinfo else cal.last_review_at.replace(tzinfo=timezone.utc)
        days_since_review = (datetime.now(timezone.utc) - last).days

    # ── 5. Six-months-ago voice (frozen, simple) ──
    # Compose from earliest-available drift point, OR earliest assumption_regenerated event
    six_months_voice = _compose_six_months_voice(session, points, drift)

    has_anything = (
        objections.count > 0
        or (drift and drift.direction != "stable")
        or is_outlier_high
        or is_outlier_low
        or (days_since_review is not None and days_since_review >= 14)
    )

    return PreSubmitReflection(
        job_id=job_id,
        job_title=job.title,
        company=job.company or "—",
        objections=objections,
        drift=drift,
        tier=tier_digest,
        days_since_value_review=days_since_review,
        six_months_voice=six_months_voice,
        has_anything_to_say=has_anything,
    )


def _compose_six_months_voice(
    session: Session,
    drift_points: list[CalibrationDriftPoint],
    drift: DriftDigest | None,
) -> str:
    """A 1-line synthesis of "what would past-you say about this submit?"

    Frozen: uses only OLDEST data, never current calibration. The point is
    to give the user a reverse-channel reminder, not a personalized cheerleader.
    """
    if not drift_points:
        return "（还没有过去的快照——这是你的第一段轨迹。）"

    earliest = drift_points[0]
    earliest_lvl = earliest.snapshot_json.get("max_amplification_level", 0)
    earliest_locked = earliest.snapshot_json.get("sensitive_categories_locked", []) or []
    captured = earliest.captured_at
    if captured.tzinfo is None:
        captured = captured.replace(tzinfo=timezone.utc)
    days_ago = (datetime.now(timezone.utc) - captured).days

    if drift and drift.direction == "loosening":
        return (
            f"{days_ago} 天前的你设的边界是 L{earliest_lvl}（更收）。"
            f"现在松到 L{earliest_lvl + drift.delta_level}——这是你深思熟虑的吗？"
        )
    if earliest_locked:
        return (
            f"{days_ago} 天前你锁住了类目: {', '.join(earliest_locked[:3])}。"
            "今天的简历是否还守住这些线？"
        )
    return f"{days_ago} 天前的你 calibration 边界是 L{earliest_lvl}。今天投递前，停一秒——还是同一个标准？"
