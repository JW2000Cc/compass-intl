"""Quick axis-tagged feedback service.

Replaces the thumbs-up/down system with structured axis feedback:
  user clicks "💰 薪资" + sentiment → ReflectionEvent(kind="job_axis_fb")
                                       payload={"job_id", "axis", "sentiment", "reason"}
                                       related_job_id

Patterns:
  - 5+ same axis+sentiment within 30d AND no brief rule for axis → "upgrade to rule"
  - Buttons can be derived from current brief axes (dynamic), with default 6 for cold start
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Job, ReflectionEvent
from .preference_brief import load_jobs_brief

log = logging.getLogger(__name__)


# Axis label + emoji for UI buttons
AXIS_LABELS: dict[str, tuple[str, str]] = {
    "comp":          ("💰", "薪资"),
    "role":          ("🎯", "岗位类型"),
    "company_stage": ("🏢", "公司风格/规模"),
    "remote":        ("🌐", "远程/到岗"),
    "location":      ("📍", "地点"),
    "culture":       ("🤝", "文化"),
    "scope":         ("📊", "工作范围"),
    "industry":      ("🏭", "行业"),
}

DEFAULT_AXES = ["comp", "role", "company_stage", "remote", "location", "culture"]
UPGRADE_THRESHOLD = 5  # same axis+sentiment count → suggest rule
UPGRADE_LOOKBACK_DAYS = 30


def get_axes_for_buttons() -> list[dict]:
    """Return list of {id, emoji, label} dicts for button rendering.

    Strategy:
      - If brief has axes → use those (max 6) so buttons mirror user's actual concerns
      - Else → DEFAULT_AXES
    Always pad to 6 from DEFAULT_AXES if brief axes < 4 (so first-time users still see 6).
    """
    try:
        brief = load_jobs_brief()
    except Exception:
        brief = None
    user_axes = list(brief.by_axis().keys()) if brief else []
    valid_user = [a for a in user_axes if a in AXIS_LABELS]

    if len(valid_user) >= 4:
        chosen = valid_user[:6]
    else:
        # Pad with defaults (de-duped)
        seen = set(valid_user)
        chosen = list(valid_user)
        for a in DEFAULT_AXES:
            if a not in seen and len(chosen) < 6:
                chosen.append(a)
                seen.add(a)

    out = []
    for ax in chosen:
        emoji, label = AXIS_LABELS.get(ax, ("•", ax))
        out.append({"id": ax, "emoji": emoji, "label": label})
    return out


def record_axis_feedback(
    session: Session,
    *,
    job: Job,
    axis: str,
    sentiment: str,         # "+" or "-"
    reason: Optional[str] = None,
) -> tuple[Optional[ReflectionEvent], str]:
    """Toggle (job, axis, sentiment) feedback. Returns (event_or_None, action).

    Behavior:
      - First click: writes a new event. action="set"
      - Same (job, axis, sentiment) clicked again: deletes that event. action="cleared"
      - Switching sentiment (e.g. + then -): clears the opposite, writes the new. action="flipped"

    Net result: at most ONE event per (job, axis), so spamming a button doesn't
    inflate the upgrade-pattern count.
    """
    if axis not in AXIS_LABELS and axis != "other":
        log.warning("record_axis_feedback: unknown axis %r — accepting anyway", axis)
    if sentiment not in {"+", "-"}:
        raise ValueError(f"sentiment must be + or -, got {sentiment!r}")

    # Find any existing fb for (job, axis) regardless of sentiment
    existing_rows = session.execute(
        select(ReflectionEvent).where(
            ReflectionEvent.kind == "job_axis_fb",
            ReflectionEvent.related_job_id == job.id,
        )
    ).scalars().all()
    same_axis = [
        e for e in existing_rows
        if (e.payload_json or {}).get("axis") == axis
    ]

    # If exact same axis+sentiment exists → toggle off (delete)
    same_axis_same_side = [
        e for e in same_axis
        if (e.payload_json or {}).get("sentiment") == sentiment
    ]
    if same_axis_same_side:
        for e in same_axis_same_side:
            session.delete(e)
        session.flush()
        return (None, "cleared")

    # Else: clear opposite-side fb (if any), then add new
    opposite = [
        e for e in same_axis
        if (e.payload_json or {}).get("sentiment") != sentiment
    ]
    flipped = bool(opposite)
    for e in opposite:
        session.delete(e)

    ev = ReflectionEvent(
        kind="job_axis_fb",
        payload_json={
            "job_id": job.id,
            "axis": axis,
            "sentiment": sentiment,
            "reason": (reason or "").strip()[:500] or None,
        },
        related_job_id=job.id,
    )
    session.add(ev)
    session.flush()
    return (ev, "flipped" if flipped else "set")


def get_job_axis_state(session: Session, job_id: str) -> dict[str, str]:
    """Return current axis state for a job: {axis: "+"/"-"} for axes with a feedback set."""
    rows = session.execute(
        select(ReflectionEvent).where(
            ReflectionEvent.kind == "job_axis_fb",
            ReflectionEvent.related_job_id == job_id,
        )
    ).scalars().all()
    out: dict[str, str] = {}
    for r in rows:
        p = r.payload_json or {}
        ax = p.get("axis")
        sent = p.get("sentiment")
        if ax and sent in ("+", "-"):
            out[ax] = sent
    return out


def axis_counts(session: Session, *, days: int = UPGRADE_LOOKBACK_DAYS) -> dict[str, dict[str, int]]:
    """Aggregate recent axis feedback. Counts DISTINCT jobs per axis+sentiment
    (not raw events), so multi-click on the same job doesn't inflate.
    Returns {axis: {'+': distinct_jobs_n, '-': distinct_jobs_n}}.
    """
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = session.execute(
        select(ReflectionEvent).where(
            ReflectionEvent.kind == "job_axis_fb",
            ReflectionEvent.created_at >= since,
        )
    ).scalars().all()

    # Group by (axis, sentiment) → set of job_ids
    seen: dict[tuple[str, str], set[str]] = defaultdict(set)
    for r in rows:
        p = r.payload_json or {}
        ax = p.get("axis")
        sent = p.get("sentiment")
        jid = p.get("job_id") or r.related_job_id
        if ax and sent in ("+", "-") and jid:
            seen[(ax, sent)].add(jid)

    out: dict[str, dict[str, int]] = defaultdict(lambda: {"+": 0, "-": 0})
    for (ax, sent), jids in seen.items():
        out[ax][sent] = len(jids)
    return dict(out)


def detect_upgrade_pattern(
    session: Session,
    *,
    axis: str,
    sentiment: str,
    threshold: int = UPGRADE_THRESHOLD,
    days: int = UPGRADE_LOOKBACK_DAYS,
) -> Optional[dict]:
    """If user has clicked the same axis+sentiment N+ times AND no brief rule
    covers this axis already, return upgrade-prompt info. Else None.

    Return shape (when triggered):
      {"axis": "comp", "sentiment": "-", "count": 6, "label": "💰 薪资",
       "suggested_text": "薪资低于 X 不看",
       "existing_rules": []}
    """
    counts = axis_counts(session, days=days)
    n = counts.get(axis, {}).get(sentiment, 0)
    if n < threshold:
        return None

    try:
        brief = load_jobs_brief()
    except Exception:
        brief = None
    by_axis = brief.by_axis() if brief else {}
    existing = by_axis.get(axis, [])
    # If there's already a rule for this axis on the same side, don't re-prompt
    same_side = [l for l in existing if l.side == sentiment]
    if same_side:
        return None

    emoji, label = AXIS_LABELS.get(axis, ("•", axis))
    side_word = "不喜欢" if sentiment == "-" else "喜欢"
    suggested = f"{label}方面：{side_word}的具体规则…"

    return {
        "axis": axis,
        "sentiment": sentiment,
        "count": n,
        "label": f"{emoji} {label}",
        "suggested_text": suggested,
        "existing_rules_other_side": [
            {"text": l.text, "side": l.side} for l in existing
        ],
    }
