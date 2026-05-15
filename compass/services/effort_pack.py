"""Per-job effort checklist — derives step completion from existing data.

Why this matters (from the architecture discussion):
    Most workflow steps are scattered across surfaces — improving the resume,
    finding a contact, drafting outreach, marking applied. Without a checklist,
    users routinely SKIP "find a referral" — the highest-ROI action — because
    they forget. The checklist makes it impossible to forget.

    Equally important: the **completion data** feeds back into improvement
    analysis ("Tier-1 jobs where you completed all 6 steps got 28% interview
    rate; jobs with no contact-finding got 5%"). Without an EffortPack, that
    data has no home.

Derivation strategy:
    Most step states are computed FROM EXISTING TABLES, not stored. The
    EffortPack table only stores explicit user overrides (e.g. "skip this step
    because it doesn't apply to this kind of job"). This keeps the data model
    clean and lets retroactive analysis work even on old jobs.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    EffortPack,
    FactVariant,
    Job,
    JobMatch,
    OutreachAttempt,
    ReflectionEvent,
    RewriteAttempt,
)
from .connections import find_connections_at


# ─────────────────────────────────────────────────────────
# Step definitions — each step is auto-derived from real data unless overridden
# ─────────────────────────────────────────────────────────


@dataclass
class Step:
    id: str
    label: str
    why: str                  # one-line user-facing rationale
    estimated_minutes: int
    auto_done: bool           # derived from existing data
    user_override: Optional[str]  # "done" / "skipped" / "n/a" if manually set
    relevance: str            # "core" | "tier_dependent" | "warm_only"
    href: Optional[str] = None  # CTA link

    @property
    def state(self) -> str:
        """Resolved state: user override wins, else auto."""
        if self.user_override:
            return self.user_override
        return "done" if self.auto_done else "todo"


@dataclass
class EffortPackView:
    job_id: str
    steps: list[Step]
    estimated_total_minutes: int
    completed_count: int
    todo_count: int
    skipped_count: int
    notes: Optional[str]


# ─────────────────────────────────────────────────────────
# Derivation
# ─────────────────────────────────────────────────────────


def _has_resume_rewrite(session: Session, job_id: str) -> bool:
    """Did the user run a tailored rewrite for this job?"""
    rt = session.execute(
        select(RewriteAttempt).where(RewriteAttempt.job_id == job_id).limit(1)
    ).scalars().first()
    if rt:
        return True
    # Also count if any FactVariant exists for this job (older variants pre-RewriteAttempt)
    fv = session.execute(
        select(FactVariant).where(FactVariant.job_id == job_id).limit(1)
    ).scalars().first()
    return fv is not None


def _has_contacts_searched(session: Session, job_id: str, company: Optional[str]) -> bool:
    """Did the user actually look at the contacts panel for this job?
    We log a ReflectionEvent only when outreach is sent. So 'searched' is
    approximated by: warm match exists OR cold cache hit OR outreach exists."""
    # Outreach attempt exists → definitely searched
    oa = session.execute(
        select(OutreachAttempt).where(OutreachAttempt.job_id == job_id).limit(1)
    ).scalars().first()
    if oa:
        return True
    # Warm match in connections → likely seen
    if company:
        warm = find_connections_at(session, company, limit=1)
        if warm:
            return True
    return False


def _has_warm_match(session: Session, company: Optional[str]) -> bool:
    if not company:
        return False
    return bool(find_connections_at(session, company, limit=1))


def _has_outreach_sent(session: Session, job_id: str) -> bool:
    return bool(
        session.execute(
            select(OutreachAttempt).where(OutreachAttempt.job_id == job_id).limit(1)
        ).scalars().first()
    )


def _is_applied(job: Job) -> bool:
    return job.status in {"applied", "interview", "offer", "rejected"}


def _reached_interview(job: Job) -> bool:
    return job.status in {"interview", "offer", "rejected"}


# ─────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────


def build_view(session: Session, job: Job) -> EffortPackView:
    """Compute the full checklist view for a job from current data."""
    pack = session.get(EffortPack, job.id)
    overrides = (pack.manual_overrides_json or {}) if pack else {}
    notes = pack.notes if pack else None

    # Tier from match (best/lowest tier)
    matches = session.execute(
        select(JobMatch).where(JobMatch.job_id == job.id).order_by(JobMatch.tier.asc())
    ).scalars().all()
    tier = matches[0].tier if matches else None

    has_warm = _has_warm_match(session, job.company)

    steps: list[Step] = []

    # Step 1 — read JD carefully
    steps.append(Step(
        id="read_jd",
        label="读 JD（认真）",
        why="2 分钟读完决定后续 30 分钟值不值得花",
        estimated_minutes=2,
        auto_done=bool(
            session.execute(
                select(ReflectionEvent).where(
                    ReflectionEvent.related_job_id == job.id,
                    ReflectionEvent.kind == "job_opened",
                ).limit(1)
            ).scalars().first()
        ),
        user_override=overrides.get("read_jd"),
        relevance="core",
    ))

    # Step 2 — tailor resume
    steps.append(Step(
        id="resume_rewrite",
        label="针对此 job 改写简历",
        why="ATS + HR 6 秒扫——通用简历过不去",
        estimated_minutes=8,
        auto_done=_has_resume_rewrite(session, job.id),
        user_override=overrides.get("resume_rewrite"),
        relevance="tier_dependent" if (tier and tier >= 3) else "core",
        href=f"/resume/?job_id={job.id}",
    ))

    # Step 3 — find contacts (warm preferred)
    steps.append(Step(
        id="find_contact",
        label="找内推 / 联系人" + (" ⭐ warm 可达" if has_warm else ""),
        why="内推回应率 30-50% vs 冷投 1-3%——15 倍差，最高 ROI 步骤",
        estimated_minutes=3 if has_warm else 8,
        auto_done=_has_contacts_searched(session, job.id, job.company),
        user_override=overrides.get("find_contact"),
        relevance="warm_only" if has_warm else "core",
        href=f"/jobs/{job.id}#contacts",
    ))

    # Step 4 — outreach
    steps.append(Step(
        id="outreach_sent",
        label="给联系人发 outreach",
        why="找到了不发 = 没找。发了至少 25% 概率有进展" if has_warm else "冷 outreach 也比纯冷投强",
        estimated_minutes=6,
        auto_done=_has_outreach_sent(session, job.id),
        user_override=overrides.get("outreach_sent"),
        relevance="core",
        href=f"/jobs/{job.id}#contacts",
    ))

    # Step 5 — actually apply
    steps.append(Step(
        id="apply",
        label="投递 + 标记 applied",
        why="完整准备但没投出去 = 0",
        estimated_minutes=3,
        auto_done=_is_applied(job),
        user_override=overrides.get("apply"),
        relevance="core",
    ))

    # Step 6 — interview prep (only shows once interview reached)
    if _reached_interview(job):
        steps.append(Step(
            id="interview_prep",
            label="面试准备（公司研究 + 答案准备）",
            why="到了面试这步——别裸面",
            estimated_minutes=45,
            auto_done=False,  # No good auto-signal; user marks manually
            user_override=overrides.get("interview_prep"),
            relevance="core",
        ))

    estimated_total = sum(s.estimated_minutes for s in steps if s.state == "todo")
    completed = sum(1 for s in steps if s.state == "done")
    todo = sum(1 for s in steps if s.state == "todo")
    skipped = sum(1 for s in steps if s.state == "skipped")

    return EffortPackView(
        job_id=job.id,
        steps=steps,
        estimated_total_minutes=estimated_total,
        completed_count=completed,
        todo_count=todo,
        skipped_count=skipped,
        notes=notes,
    )


def set_override(session: Session, job_id: str, step_id: str, override: Optional[str]) -> None:
    """Set or clear a manual override for one step."""
    if override and override not in {"done", "skipped", "n/a"}:
        raise ValueError(f"invalid override {override!r}")
    pack = session.get(EffortPack, job_id)
    if pack is None:
        pack = EffortPack(job_id=job_id, manual_overrides_json={})
        session.add(pack)
    overrides = dict(pack.manual_overrides_json or {})
    if override is None:
        overrides.pop(step_id, None)
    else:
        overrides[step_id] = override
    pack.manual_overrides_json = overrides

    # If everything is done now, mark completed_at
    job = session.get(Job, job_id)
    if job:
        view = build_view(session, job)
        if view.todo_count == 0 and pack.completed_at is None:
            pack.completed_at = datetime.now(timezone.utc)
