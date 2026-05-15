"""Funnel analytics — Compass-flavored.

Differs from v1's funnel.py: instead of computing from the Job table alone,
Compass joins with JobMatch (tier-aware) and reads ReflectionEvent for richer
signal (thumbs / status changes / re-pushes etc.).

Stages:
    scraped     — every non-deleted Job
    classified  — has a JobMatch row
    pushable    — match.tier in (1,2,3) AND not Tier 4/5
    interacted  — user has thumbs-up/down OR opened detail
    applied     — status in {applied, interview, offer, rejected}
    interview   — status in {interview, offer, rejected}
    offer       — status == "offer"
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Job, JobMatch, ReflectionEvent

APPLIED_STATUSES = {"applied", "interview", "offer", "rejected"}
INTERVIEW_STATUSES = {"interview", "offer", "rejected"}
PUSHABLE_TIERS = {1, 2, 3}


@dataclass
class FunnelCounts:
    scraped: int = 0
    classified: int = 0
    pushable: int = 0
    interacted: int = 0
    applied: int = 0
    interview: int = 0
    offer: int = 0
    rejected: int = 0
    by_tier: dict[int, int] = None

    def __post_init__(self):
        if self.by_tier is None:
            self.by_tier = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}

    def as_dict(self) -> dict:
        return {
            "scraped": self.scraped,
            "classified": self.classified,
            "pushable": self.pushable,
            "interacted": self.interacted,
            "applied": self.applied,
            "interview": self.interview,
            "offer": self.offer,
            "rejected": self.rejected,
            "by_tier": dict(self.by_tier),
        }

    def conversion_rates(self) -> dict[str, Optional[float]]:
        def r(num: int, den: int) -> Optional[float]:
            return round(num / den * 100, 1) if den > 0 else None
        return {
            "scraped→classified": r(self.classified, self.scraped),
            "classified→pushable": r(self.pushable, self.classified),
            "pushable→interacted": r(self.interacted, self.pushable),
            "interacted→applied": r(self.applied, self.interacted),
            "applied→interview": r(self.interview, self.applied),
            "interview→offer": r(self.offer, self.interview),
            "scraped→offer": r(self.offer, self.scraped),
        }


def compute(
    session: Session,
    *,
    since: Optional[datetime] = None,
    source: Optional[str] = None,
) -> FunnelCounts:
    q = select(Job).where(Job.deleted.is_(False))
    if since:
        q = q.where(Job.created_at >= since)
    if source:
        q = q.where(Job.source == source)
    jobs = session.execute(q).scalars().all()

    # Map job_id → highest-tier match (Compass keeps history, "current" = lowest tier == best fit)
    matches = (
        session.execute(select(JobMatch)).scalars().all()
    )
    best_match: dict[str, JobMatch] = {}
    for m in matches:
        prev = best_match.get(m.job_id)
        if prev is None or (m.tier < prev.tier) or (m.tier == prev.tier and (m.score or 0) > (prev.score or 0)):
            best_match[m.job_id] = m

    interacted_ids = _interacted_job_ids(session)

    fc = FunnelCounts()
    for j in jobs:
        fc.scraped += 1
        m = best_match.get(j.id)
        if m is not None:
            fc.classified += 1
            fc.by_tier[m.tier] = fc.by_tier.get(m.tier, 0) + 1
            if m.tier in PUSHABLE_TIERS:
                fc.pushable += 1
        if j.id in interacted_ids:
            fc.interacted += 1
        if j.status in APPLIED_STATUSES:
            fc.applied += 1
        if j.status in INTERVIEW_STATUSES:
            fc.interview += 1
        if j.status == "offer":
            fc.offer += 1
        if j.status == "rejected":
            fc.rejected += 1
    return fc


def _interacted_job_ids(session: Session) -> set[str]:
    rows = session.execute(
        select(ReflectionEvent.related_job_id).where(
            ReflectionEvent.kind.in_(["job_thumbed_up", "job_thumbed_down", "job_opened"])
        )
    ).all()
    return {r[0] for r in rows if r[0]}


def list_jobs_by_tier(
    session: Session,
    *,
    tier: int | None = None,
    limit: int = 50,
    status: str | None = None,
    sort: str = "scraped_desc",
    q_text: str | None = None,
    location: str | None = None,
) -> list[tuple[Job, JobMatch | None]]:
    """Drill-down: jobs filtered by tier, joined with their best match.

    sort: "scraped_desc" (default) | "scraped_asc"
    q_text: optional search across title/company/location (case-insensitive)
    location: substring filter on Job.location (case-insensitive)
    """
    q = select(Job).where(Job.deleted.is_(False))
    if status:
        q = q.where(Job.status == status)
    if location:
        from sqlalchemy import func
        q = q.where(func.lower(Job.location).like(f"%{location.lower()}%"))
    if q_text:
        from sqlalchemy import or_, func
        like = f"%{q_text.lower()}%"
        q = q.where(
            or_(
                func.lower(Job.title).like(like),
                func.lower(Job.company).like(like),
                func.lower(Job.location).like(like),
            )
        )
    if sort == "scraped_asc":
        q = q.order_by(Job.created_at.asc())
    else:
        q = q.order_by(Job.created_at.desc())
    q = q.limit(limit * 3)  # over-fetch; filter by tier post-hoc
    jobs = session.execute(q).scalars().all()

    matches = session.execute(select(JobMatch)).scalars().all()
    best_match: dict[str, JobMatch] = {}
    for m in matches:
        prev = best_match.get(m.job_id)
        if prev is None or m.tier < prev.tier:
            best_match[m.job_id] = m

    out: list[tuple[Job, JobMatch | None]] = []
    for j in jobs:
        m = best_match.get(j.id)
        if tier is not None and (m is None or m.tier != tier):
            continue
        out.append((j, m))
        if len(out) >= limit:
            break
    return out


def top_locations(session: Session, *, max_n: int = 10) -> list[tuple[str, int]]:
    """Return [(location, count), ...] for the most populated locations.
    Empty / None locations are excluded.
    """
    from sqlalchemy import func
    rows = session.execute(
        select(Job.location, func.count(Job.id))
        .where(Job.deleted.is_(False), Job.location.isnot(None), Job.location != "")
        .group_by(Job.location)
        .order_by(func.count(Job.id).desc())
        .limit(max_n)
    ).all()
    return [(loc, cnt) for loc, cnt in rows]


def epsilon_exploration_summary(session: Session, *, days: int = 30) -> dict:
    """How balanced is the user's recent push distribution? (anti-bubble metric)

    Returns:
        {tier_distribution: {1: pct, 2: pct, 3: pct},
         target: {1: 0.70, 2: 0.20, 3: 0.10},
         deviation: 0-1 where 0 = perfect ε-exploration,
         tier_2_starvation: bool — Tier 2 (anti-bubble sweet spot) below 10%,
         message: human-readable status}
    """
    from .tier_classifier import DEFAULT_QUOTAS

    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = session.execute(
        select(Job, JobMatch).join(JobMatch).where(Job.created_at >= since)
    ).all()

    counts = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
    for _, m in rows:
        if m.tier in counts:
            counts[m.tier] += 1
    total = sum(counts[t] for t in (1, 2, 3))
    if total == 0:
        return {
            "tier_distribution": {},
            "target": DEFAULT_QUOTAS,
            "deviation": None,
            "tier_2_starvation": False,
            "message": "还没有足够的 push 数据来评估 ε-exploration。",
        }

    distribution = {t: counts[t] / total for t in (1, 2, 3)}
    deviation = sum(abs(distribution[t] - DEFAULT_QUOTAS[t]) for t in (1, 2, 3)) / 2
    starvation = distribution[2] < 0.10

    msg = []
    if starvation:
        msg.append(
            f"⚠ Tier 2（邻近迁移 = 反茧房甜点）只占 {distribution[2]*100:.0f}%（目标 20%）— 视野可能在收窄"
        )
    if abs(distribution[1] - 0.70) > 0.20:
        msg.append(f"Tier 1 占比 {distribution[1]*100:.0f}%（目标 70%）")

    return {
        "tier_distribution": distribution,
        "target": DEFAULT_QUOTAS,
        "deviation": round(deviation, 3),
        "tier_2_starvation": starvation,
        "message": "; ".join(msg) if msg else "ε-exploration 配额健康。",
    }


# ─────────────────────────────────────────────────────────
# Multi-dimensional funnel slicing (by source / location / role)
# Ported from Job Mate v2 — verified to surface "which channel works" insights.
# ─────────────────────────────────────────────────────────


_SENIORITY_WORDS = {
    "junior", "senior", "lead", "principal", "staff", "intern", "internship",
    "graduate", "grad", "trainee", "associate", "entry", "level",
    "i", "ii", "iii", "iv", "v", "1", "2", "3",
}


def _role_bucket(title: str) -> str:
    """Coarse role bucket from a title. Strip seniority noise + parens, keep first 3 words."""
    if not title:
        return "unknown"
    t = title.lower().strip()
    # Strip parens content (e.g. "(Remote)", "(EU)")
    while "(" in t and ")" in t:
        s, e = t.find("("), t.find(")")
        if s < e:
            t = (t[:s] + " " + t[e + 1:]).strip()
        else:
            break
    # Normalize separators
    for sep in ("/", "-", "—", "–", ",", "|", "·"):
        t = t.replace(sep, " ")
    words = [w for w in t.split() if w not in _SENIORITY_WORDS]
    if not words:
        return "unknown"
    return " ".join(words[:3])


def _location_bucket(location: str | None) -> str:
    """Coarse location bucket — extract country if possible, else city."""
    if not location:
        return "unknown"
    parts = [p.strip() for p in location.split(",") if p.strip()]
    if not parts:
        return "unknown"
    # Last token is usually country (jobspy convention "City, Country")
    return parts[-1] if len(parts) >= 2 else parts[0]


def compute_by_dimension(
    session: Session,
    dimension: str,
    *,
    since: Optional[datetime] = None,
    top_n: int = 8,
) -> list[dict]:
    """Compute funnel counts grouped by `dimension` ('source' | 'location' | 'role').

    Returns a list of dicts:
      {key: str, counts: {scraped, classified, pushable, applied, interview, offer},
       conversion: {scraped→classified: %, ...},
       by_tier: {1..5}}

    Sorted by scraped count descending, trimmed to top_n.
    """
    if dimension not in ("source", "location", "role"):
        raise ValueError(f"Unknown dimension: {dimension!r}")

    q = select(Job).where(Job.deleted.is_(False))
    if since:
        q = q.where(Job.created_at >= since)
    jobs = session.execute(q).scalars().all()

    # Map job_id → best (lowest tier) match
    best_match: dict[str, JobMatch] = {}
    for m in session.execute(select(JobMatch)).scalars().all():
        prev = best_match.get(m.job_id)
        if prev is None or m.tier < prev.tier:
            best_match[m.job_id] = m

    interacted_ids = _interacted_job_ids(session)

    # Bucket jobs by dimension key
    buckets: dict[str, list[Job]] = defaultdict(list)
    for j in jobs:
        if dimension == "source":
            key = (j.source or "").strip().lower() or "unknown"
        elif dimension == "location":
            key = _location_bucket(j.location)
        elif dimension == "role":
            key = _role_bucket(j.title)
        buckets[key].append(j)

    rows: list[dict] = []
    for key, group in buckets.items():
        fc = FunnelCounts()
        for j in group:
            fc.scraped += 1
            m = best_match.get(j.id)
            if m is not None:
                fc.classified += 1
                fc.by_tier[m.tier] = fc.by_tier.get(m.tier, 0) + 1
                if m.tier in PUSHABLE_TIERS:
                    fc.pushable += 1
            if j.id in interacted_ids:
                fc.interacted += 1
            if j.status in APPLIED_STATUSES:
                fc.applied += 1
            if j.status in INTERVIEW_STATUSES:
                fc.interview += 1
            if j.status == "offer":
                fc.offer += 1
            if j.status == "rejected":
                fc.rejected += 1
        rows.append({
            "key": key,
            "counts": fc.as_dict(),
            "conversion": fc.conversion_rates(),
        })

    rows.sort(key=lambda r: r["counts"]["scraped"], reverse=True)
    return rows[:top_n]


def timeseries(session: Session, *, days: int = 30) -> list[dict]:
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    jobs = session.execute(
        select(Job).where(Job.created_at >= start, Job.deleted.is_(False))
    ).scalars().all()

    counts: dict[str, dict] = defaultdict(lambda: {"scraped": 0, "applied": 0, "offer": 0})
    for j in jobs:
        d = j.created_at.date().isoformat()
        counts[d]["scraped"] += 1
        if j.status in APPLIED_STATUSES:
            counts[d]["applied"] += 1
        if j.status == "offer":
            counts[d]["offer"] += 1
    return sorted(({"date": k, **v} for k, v in counts.items()), key=lambda r: r["date"])
