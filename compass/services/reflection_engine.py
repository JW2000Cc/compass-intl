"""Reflection engine — the soul of Compass.

Two main features in Phase 0:

    1. Drift dashboard
       Reads ReflectionEvent + CalibrationDriftPoint streams.
       Surfaces:
           - calibration boundary trajectory over time
           - thumbs-up vs thumbs-down divergence (stated vs revealed preference)
           - assumption override frequency

    2. Tool-assumptions report
       LLM reads facts + recent reflections, produces 6 statements like:
           "Compass currently assumes you most value ..."
           "Compass currently assumes you'd rather ..."
       Each is editable. The user's overrides are sticky and persist back to UserCalibration.

These both READ from existing data. Neither requires a tier matcher / claim grounder
to exist. That's why they're Phase 0 — minimum viable reflection layer.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from ..models import (
    CalibrationDriftPoint,
    Job,
    JobMatch,
    ReflectionEvent,
    ResumeFact,
    ToolAssumption,
    UserCalibration,
)
from .llm import LLMConfigError, chat

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────
# Drift dashboard
# ─────────────────────────────────────────────────────────


@dataclass
class DriftSnapshot:
    captured_at: datetime
    max_amplification_level: int
    sensitive_locked: list[str]
    style_rules_count: int
    raw: dict[str, Any]


@dataclass
class DriftReport:
    points: list[DriftSnapshot]  # chronological
    current: DriftSnapshot | None
    review_due: bool
    summary_lines: list[str]
    verdict: str  # "stable" | "drifting" | "no_data"


def build_drift_report(session: Session, *, review_interval_days: int = 14) -> DriftReport:
    """Construct the drift dashboard payload."""
    rows = (
        session.execute(
            select(CalibrationDriftPoint).order_by(CalibrationDriftPoint.captured_at.asc())
        )
        .scalars()
        .all()
    )
    points = [
        DriftSnapshot(
            captured_at=r.captured_at,
            max_amplification_level=r.snapshot_json.get("max_amplification_level", 0),
            sensitive_locked=r.snapshot_json.get("sensitive_categories_locked", []),
            style_rules_count=len(r.snapshot_json.get("style_rules", [])),
            raw=r.snapshot_json,
        )
        for r in rows
    ]
    current = points[-1] if points else None

    cal = session.get(UserCalibration, 1)
    review_due = True
    if cal and cal.last_review_at:
        # SQLite strips tzinfo on round-trip; treat naive stored as UTC.
        last = cal.last_review_at if cal.last_review_at.tzinfo else cal.last_review_at.replace(tzinfo=timezone.utc)
        review_due = (datetime.now(timezone.utc) - last).days >= review_interval_days

    summary, verdict = _summarize_drift(points)
    return DriftReport(points=points, current=current, review_due=review_due, summary_lines=summary, verdict=verdict)


def _summarize_drift(points: list[DriftSnapshot]) -> tuple[list[str], str]:
    """Produce 1-3 lines of human-readable drift summary, plus verdict."""
    if len(points) < 2:
        return (["还没有足够的快照画出漂移轨迹（需要至少 2 个时间点）。"], "no_data")

    first, last = points[0], points[-1]
    lines = []
    delta_level = last.max_amplification_level - first.max_amplification_level
    if delta_level >= 1:
        lines.append(
            f"⚠ 你的 max_amplification_level 从 L{first.max_amplification_level} 漂到 L{last.max_amplification_level}（{delta_level:+d}）— 这是边界放宽。"
        )
    elif delta_level <= -1:
        lines.append(
            f"✓ 你的 max_amplification_level 从 L{first.max_amplification_level} 收紧到 L{last.max_amplification_level}（{delta_level}）— 边界收紧。"
        )
    else:
        lines.append(f"边界稳定在 L{last.max_amplification_level}。")

    delta_locked = set(last.sensitive_locked) - set(first.sensitive_locked)
    if delta_locked:
        lines.append(f"新增了硬锁类目: {', '.join(sorted(delta_locked))}")

    verdict = "drifting" if abs(delta_level) >= 2 else "stable"
    return lines, verdict


def take_drift_snapshot(session: Session) -> CalibrationDriftPoint:
    """Capture current calibration state into a drift point. Idempotent per day."""
    cal = session.get(UserCalibration, 1)
    if cal is None:
        cal = UserCalibration(id=1)
        session.add(cal)
        session.flush()
    snapshot = {
        "max_amplification_level": cal.max_amplification_level,
        "preferred_verbs_do": list(cal.preferred_verbs_do or []),
        "preferred_verbs_avoid": list(cal.preferred_verbs_avoid or []),
        "sensitive_categories_locked": list(cal.sensitive_categories_locked or []),
        "style_rules": list(cal.style_rules or []),
    }
    point = CalibrationDriftPoint(snapshot_json=snapshot)
    session.add(point)
    session.add(ReflectionEvent(kind="drift_snapshot_taken", payload_json=snapshot))
    session.flush()
    return point


# ─────────────────────────────────────────────────────────
# Tool-assumptions report
# ─────────────────────────────────────────────────────────


ASSUMPTIONS_SYSTEM = """You are Compass — a career reflection system.

Your job: read this user's resume facts + recent reflection events, then state
6 BELIEFS the system seems to currently hold about them. Format each as a
candid first-person claim from the system to the user, e.g.:

    "I (the system) seem to assume you most value technical depth over breadth"
    "I seem to assume you'd rather work in mid-sized companies than startups"

Cover these 6 categories (one belief each):
    1. values_held       — what the user seems to most value in work
    2. role_directions   — what role types the user gravitates toward
    3. growth_areas      — what skills/domains the user wants to grow into
    4. risk_tolerance    — how risk-averse / -seeking the user seems
    5. work_environment  — preferred company stage/size/culture
    6. boundary_style    — how aggressive the user is willing to be in resume rewriting

Rules:
- Be SPECIFIC. "Likes engineering" is too vague. "Gravitates toward systems-level
  problems with measurable correctness criteria" is right.
- Express UNCERTAINTY when data is thin. "I have very little signal on..."
- DO NOT make up data. If a category has no evidence, say so.
- For each, give a confidence 0.0-1.0.
- For each, state ONE concrete observation that supports it.
- The user will see all 6 and override any they disagree with — embrace this.

Output ONLY a JSON array of 6 objects:
[
  {
    "category": "values_held",
    "text": "I seem to assume you...",
    "confidence": 0.X,
    "evidence": "single concrete observation"
  },
  ...
]
"""


@dataclass
class AssumptionRow:
    id: str | None
    category: str
    text: str
    confidence: float
    evidence: str
    user_override: str | None
    is_current: bool
    generated_at: datetime


def get_current_assumptions(session: Session) -> list[AssumptionRow]:
    rows = (
        session.execute(
            select(ToolAssumption)
            .where(ToolAssumption.is_current.is_(True))
            .order_by(ToolAssumption.category)
        )
        .scalars()
        .all()
    )
    out: list[AssumptionRow] = []
    for r in rows:
        out.append(
            AssumptionRow(
                id=r.id,
                category=r.category,
                text=r.text,
                confidence=r.confidence,
                evidence="",
                user_override=r.user_override,
                is_current=r.is_current,
                generated_at=r.generated_at,
            )
        )
    return out


def regenerate_assumptions(
    session: Session,
    *,
    provider: str,
    api_key: str,
    model: str,
) -> list[AssumptionRow]:
    """Run the LLM, deactivate previous current assumptions, write new ones.

    Always preserves user_override values for the same category (sticky).
    """
    if not api_key:
        raise LLMConfigError("Tool-assumptions regeneration requires an LLM API key.")

    facts = session.execute(select(ResumeFact).where(ResumeFact.active.is_(True))).scalars().all()
    fact_lines = [f"- [{f.kind}] {f.text}" for f in facts[:80]]

    recent_events = (
        session.execute(
            select(ReflectionEvent).order_by(desc(ReflectionEvent.created_at)).limit(40)
        )
        .scalars()
        .all()
    )
    event_lines = [
        f"- [{e.kind}] {(e.payload_json or {}).get('summary','')[:120]}"
        for e in recent_events
    ]

    user_msg = (
        "## User facts (truncated)\n"
        + "\n".join(fact_lines)
        + "\n\n## Recent reflection events (most recent first)\n"
        + "\n".join(event_lines)
    )

    response = chat(
        provider=provider,
        api_key=api_key,
        model=model,
        system=ASSUMPTIONS_SYSTEM,
        user_content=user_msg,
        max_tokens=1500,
    )
    parsed = response.parse_json(default=[])
    if not isinstance(parsed, list):
        parsed = []

    # Preserve user overrides keyed by category
    prior_overrides: dict[str, str] = {}
    for prior in (
        session.execute(select(ToolAssumption).where(ToolAssumption.is_current.is_(True)))
        .scalars()
        .all()
    ):
        if prior.user_override:
            prior_overrides[prior.category] = prior.user_override
        prior.is_current = False

    out: list[AssumptionRow] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        category = str(item.get("category", "uncategorized"))[:40]
        text = str(item.get("text", "")).strip()
        confidence = float(item.get("confidence", 0.5) or 0.5)
        evidence = str(item.get("evidence", "")).strip()
        if not text:
            continue
        new = ToolAssumption(
            category=category,
            text=text,
            confidence=max(0.0, min(1.0, confidence)),
            user_override=prior_overrides.get(category),
            is_current=True,
        )
        session.add(new)
        session.flush()
        out.append(
            AssumptionRow(
                id=new.id,
                category=category,
                text=text,
                confidence=new.confidence,
                evidence=evidence,
                user_override=new.user_override,
                is_current=True,
                generated_at=new.generated_at,
            )
        )

    session.add(
        ReflectionEvent(
            kind="assumption_regenerated",
            payload_json={"count": len(out), "model": response.model},
        )
    )
    return out


def override_assumption(session: Session, assumption_id: str, override_text: str | None) -> None:
    """User edits/clears an assumption. Logs a reflection event."""
    a = session.get(ToolAssumption, assumption_id)
    if a is None:
        return
    a.user_override = (override_text.strip() if override_text else None)
    session.add(
        ReflectionEvent(
            kind="assumption_overridden",
            payload_json={
                "category": a.category,
                "system_text": a.text,
                "user_override": a.user_override,
            },
        )
    )


# ─────────────────────────────────────────────────────────
# Stated-vs-revealed preference signal
# ─────────────────────────────────────────────────────────


@dataclass
class PreferenceDivergence:
    pushed_count: int
    thumbs_up_count: int
    thumbs_down_count: int
    notes: list[str]


# ─────────────────────────────────────────────────────────
# Active alerts — surfaced globally on every page (not just /reflect/dashboard)
# ─────────────────────────────────────────────────────────


@dataclass
class ActiveAlert:
    """A single banner-worthy alert.

    severity: 'warn' | 'info'
    kind:     stable identifier (used for dismiss-for-session keying client-side)
    title:    short headline (≤ 30 chars displayed)
    body:     1 sentence elaboration
    cta:      (label, url) for the user to act on it
    """
    severity: str
    kind: str
    title: str
    body: str
    cta_label: str | None
    cta_url: str | None


def compute_active_alerts(
    session: Session,
    *,
    review_interval_days: int = 14,
    revealed_window_days: int = 60,
) -> list[ActiveAlert]:
    """Aggregate signals that should surface as a global banner.

    Decoupled from the reflection dashboard view: any page can render these.
    Read-only; never mutates state. Cheap (~3 small SELECTs).

    Signals (in priority order, first match wins for the global banner):
      1. value_regression_due  — last_review_at > review_interval_days
      2. drift_unstable        — drift report verdict == "drifting" (level shifted ≥2)
      3. preference_divergence — thumbs_down > 2× thumbs_up (recent window)
      4. tier_2_starvation     — ε-exploration Tier 2 < 10% (anti-bubble bell)

    Returns ALL applicable alerts, ordered by priority. Caller picks how many to render.
    """
    alerts: list[ActiveAlert] = []

    # ── 1. value-regression checkpoint due ──
    cal = session.get(UserCalibration, 1)
    days_since_review: int | None = None
    if cal and cal.last_review_at:
        # SQLite strips tzinfo; treat naive stored as UTC.
        last = cal.last_review_at if cal.last_review_at.tzinfo else cal.last_review_at.replace(tzinfo=timezone.utc)
        days_since_review = (datetime.now(timezone.utc) - last).days
    if cal is None or cal.last_review_at is None or (days_since_review is not None and days_since_review >= review_interval_days):
        d = days_since_review if days_since_review is not None else None
        body = (
            f"距离上次价值观回归审视已 {d} 天（建议每 {review_interval_days} 天一次）。"
            if d is not None
            else "还没有跑过价值观回归审视——是时候确认系统假设没跑偏了。"
        )
        alerts.append(ActiveAlert(
            severity="warn",
            kind="value_regression_due",
            title="🪞 该回归审视了",
            body=body,
            cta_label="去看系统假设",
            cta_url="/reflect/assumptions",
        ))

    # ── 2. drift unstable ──
    points = (
        session.execute(
            select(CalibrationDriftPoint).order_by(CalibrationDriftPoint.captured_at.asc())
        )
        .scalars()
        .all()
    )
    if len(points) >= 2:
        first = points[0].snapshot_json.get("max_amplification_level", 0)
        last = points[-1].snapshot_json.get("max_amplification_level", 0)
        delta = last - first
        if abs(delta) >= 2:
            direction = "放宽" if delta > 0 else "收紧"
            alerts.append(ActiveAlert(
                severity="warn",
                kind="drift_unstable",
                title="📉 边界漂移异常",
                body=f"max_amplification_level 已 {direction} {abs(delta)} 级（L{first} → L{last}）— 这不是慢漂移，是激进调整。",
                cta_label="看漂移仪表盘",
                cta_url="/reflect/dashboard",
            ))

    # ── 3. revealed-vs-stated divergence ──
    since = datetime.now(timezone.utc) - timedelta(days=revealed_window_days)
    up_count = session.execute(
        select(ReflectionEvent).where(
            ReflectionEvent.kind == "job_thumbed_up", ReflectionEvent.created_at >= since
        )
    ).scalars().all()
    down_count = session.execute(
        select(ReflectionEvent).where(
            ReflectionEvent.kind == "job_thumbed_down", ReflectionEvent.created_at >= since
        )
    ).scalars().all()
    n_up, n_down = len(up_count), len(down_count)
    if n_up + n_down >= 6 and n_down > 2 * max(1, n_up):
        alerts.append(ActiveAlert(
            severity="warn",
            kind="preference_divergence",
            title="🎯 推送与你的实际偏好分歧",
            body=f"过去 {revealed_window_days} 天 👎 ({n_down}) 远多于 👍 ({n_up})。系统对你的画像可能跑偏了。",
            cta_label="重生成系统假设",
            cta_url="/reflect/assumptions",
        ))

    # ── 4. Tier-2 starvation (anti-bubble bell) ──
    # Inline minimal version of epsilon_exploration_summary to avoid circular import
    try:
        from .job_funnel import epsilon_exploration_summary
        eps = epsilon_exploration_summary(session, days=30)
        if eps.get("tier_2_starvation"):
            t2 = eps.get("tier_distribution", {}).get(2, 0) * 100
            alerts.append(ActiveAlert(
                severity="info",
                kind="tier_2_starvation",
                title="🫧 视野在收窄",
                body=f"Tier 2（邻近迁移 = 反茧房甜点）只占 {t2:.0f}% — 信息茧房风险上升。",
                cta_label="看漏斗 by Tier",
                cta_url="/funnel/",
            ))
    except Exception as e:
        log.debug("epsilon summary failed in active_alerts: %s", e)

    # ── 5. Stated vs revealed semantic conflict ──
    # Direct contradictions between user's preference brief and their stated
    # criteria (scrape keywords, salary floors, calibration locks). This is
    # the *core* reflective signal — surface it where it can't be missed.
    try:
        from .conflict_detector import all_conflicts
        conflicts = all_conflicts()
        hard = [c for c in conflicts if c.severity == "hard"]
        if hard:
            n = len(hard)
            alerts.append(ActiveAlert(
                severity="warn",
                kind="stated_vs_revealed_conflict",
                title=f"🪞 stated vs revealed 分歧 ({n})",
                body=(
                    f"你说的和你做的有 {n} 处直接矛盾——"
                    "比如你 scrape 关键词写了 'fintech' 但反馈累积出'避开 fintech'。"
                    "是时候问自己是哪个版本的你。"
                ),
                cta_label="去解决",
                cta_url="/feedback?tab=jobs#conflicts",
            ))
    except Exception as e:
        log.debug("conflict detection failed in active_alerts: %s", e)

    return alerts


def stated_vs_revealed(session: Session, *, days: int = 90) -> PreferenceDivergence:
    """Compare what user CLAIMED to want (in calibration / facts) vs what they
    actually thumbs-upped. v0 is a simple count summary; richer divergence
    analysis lands when JobMatch tier is populated."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    pushed = session.execute(
        select(JobMatch).join(Job).where(Job.created_at >= since, JobMatch.tier <= 3)
    ).scalars().all()
    up = session.execute(
        select(ReflectionEvent).where(
            ReflectionEvent.kind == "job_thumbed_up", ReflectionEvent.created_at >= since
        )
    ).scalars().all()
    down = session.execute(
        select(ReflectionEvent).where(
            ReflectionEvent.kind == "job_thumbed_down", ReflectionEvent.created_at >= since
        )
    ).scalars().all()

    notes = []
    if not pushed and not up and not down:
        notes.append("还没有足够的反馈数据；用一段时间后回来看。")
    else:
        notes.append(
            f"过去 {days} 天：推送 {len(pushed)} / 👍 {len(up)} / 👎 {len(down)}。"
        )
        if up and down and len(down) > len(up) * 2:
            notes.append("👎 远多于 👍 — 系统的推送可能偏离了你的真实偏好，建议跑一次 /reflect/assumptions 重生成假设。")
    return PreferenceDivergence(
        pushed_count=len(pushed),
        thumbs_up_count=len(up),
        thumbs_down_count=len(down),
        notes=notes,
    )


# ────────────────────────────────────────────────────────────
# tier_quota stated vs revealed（反身性距离）
# ────────────────────────────────────────────────────────────
# 与 epsilon_exploration_summary 的区别:
#   - epsilon_exploration_summary: 系统 push 分布 vs 全局 DEFAULT_QUOTAS
#   - tier_intent_vs_action: 用户**配置**的 tier_quota 总和 vs 用户**实际互动**的岗位 tier
# 这条指标暴露的是 "你说想要什么" 与 "你做了什么" 的反身性 gap。

# 视为"深度互动"的事件——这些都意味着用户为该岗位投入了远超滑过的注意力
_ENGAGEMENT_KINDS = (
    "job_opened",
    "rewrite_attempt_completed",
    "variant_approved",
    "applied_with_reflection_seen",
    "job_thumbed_up",
)


def tier_intent_vs_action(session: Session, intent: Any, *, days: int = 60) -> dict:
    """声称的 tier_quota 分布 vs 用户实际互动的岗位 tier 分布。

    Returns:
        {
          "stated":   {1: 0.55, 2: 0.27, 3: 0.18},   # 用户配的
          "revealed": {1: 0.92, 2: 0.05, 3: 0.03},   # 用户做的
          "gap":      {1: +0.37, 2: -0.22, 3: -0.15}, # revealed - stated
          "l1_distance": 0.74,            # sum(|gap|), 0..2
          "verdict": "tier1_locked" | "explorer_revealed" | "aligned" | "drifting" | "insufficient_data",
          "sample_size": 47,
          "days": 60,
          "message": "...",
        }
    """
    enabled = []
    if intent is not None:
        try:
            enabled = intent.enabled_profiles()
        except Exception:
            enabled = []

    stated_raw = {1: 0, 2: 0, 3: 0}
    for p in enabled:
        tq = getattr(p, "tier_quota", None)
        if tq is None:
            continue
        stated_raw[1] += max(0, int(getattr(tq, "tier_1", 0) or 0))
        stated_raw[2] += max(0, int(getattr(tq, "tier_2", 0) or 0))
        stated_raw[3] += max(0, int(getattr(tq, "tier_3", 0) or 0))
    stated_sum = sum(stated_raw.values())
    stated = (
        {t: round(stated_raw[t] / stated_sum, 4) for t in (1, 2, 3)}
        if stated_sum > 0
        else {}
    )

    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
    rows = session.execute(
        select(ReflectionEvent.related_job_id, JobMatch.tier)
        .join(JobMatch, JobMatch.job_id == ReflectionEvent.related_job_id)
        .where(
            ReflectionEvent.kind.in_(_ENGAGEMENT_KINDS),
            ReflectionEvent.created_at >= cutoff,
            ReflectionEvent.related_job_id.is_not(None),
        )
    ).all()

    engaged_tiers: dict[int, int] = {}
    for jid, tier in rows:
        if jid is None or tier not in (1, 2, 3):
            continue
        if jid not in engaged_tiers:
            engaged_tiers[jid] = tier

    counts = {1: 0, 2: 0, 3: 0}
    for tier in engaged_tiers.values():
        counts[tier] += 1
    revealed_sum = sum(counts.values())
    revealed = (
        {t: round(counts[t] / revealed_sum, 4) for t in (1, 2, 3)}
        if revealed_sum > 0
        else {}
    )

    if not stated and not revealed:
        msg = "还没启用 profile 也没有互动数据 — 先去 /jobs/search-config/ 配 tier_quota，再用一段时间。"
        verdict = "insufficient_data"
    elif not stated:
        msg = "你启用的 profile 没有有效 tier_quota — 在 /jobs/search-config/ 设一下。"
        verdict = "insufficient_data"
    elif not revealed:
        msg = f"过去 {days} 天没有 job 互动事件——多点开几个岗位、跑一次简历改写,回来再看。"
        verdict = "insufficient_data"
    else:
        gap = {t: round(revealed.get(t, 0) - stated.get(t, 0), 3) for t in (1, 2, 3)}
        l1 = round(sum(abs(g) for g in gap.values()), 3)
        if l1 < 0.20:
            verdict = "aligned"
            msg = "你说的和你做的一致 — 当前互动分布贴近你声称的 tier_quota。"
        elif gap[1] >= 0.20 and gap[2] <= -0.10:
            verdict = "tier1_locked"
            msg = (
                f"你声称 Tier 2 占 {stated[2]*100:.0f}%（反茧房甜点）,"
                f"实际只点开 {revealed[2]*100:.0f}% — 想要邻近迁移,行为却锁在核心圈。"
                "这是 reflexivity gap：去 /reflect/assumptions 看系统学到的"
                "是否已经偏向只推 Tier 1。"
            )
        elif gap[2] >= 0.15 or gap[3] >= 0.15:
            verdict = "explorer_revealed"
            msg = (
                f"实际行为比 quota 更探索：Tier 2 实占 {revealed[2]*100:.0f}%"
                f"（声称 {stated[2]*100:.0f}%）。"
                "如果稳定持续,可在 /jobs/search-config/ 提高 tier_2 让系统多推。"
            )
        else:
            verdict = "drifting"
            msg = f"声称 vs 行为 L1 距离 {l1:.2f}（<0.20 视为对齐,>0.40 显著偏离）— 看下方对照。"
        return {
            "stated": stated,
            "revealed": revealed,
            "gap": gap,
            "l1_distance": l1,
            "verdict": verdict,
            "sample_size": revealed_sum,
            "days": days,
            "message": msg,
        }

    return {
        "stated": stated,
        "revealed": revealed,
        "gap": {},
        "l1_distance": None,
        "verdict": verdict,
        "sample_size": revealed_sum,
        "days": days,
        "message": msg,
    }


# ────────────────────────────────────────────────────────────
# Reflective Flywheel 完整闭环 metrics（5-01 P1 第 7 条 / 5-07 落实）
# ────────────────────────────────────────────────────────────


def flywheel_loop_health(session: Session, *, days: int = 90) -> dict:
    """衡量"反思飞轮"的闭环质量 — 用户的反馈是否真的反过来改了系统行为。

    4 个核心信号：
      1. **thumbs ↔ tier reclassification 闭环率**
         你 👎 一个岗位后，下次同公司/同标题的岗位 tier 是否真 demote？
         如果一直 push 同样的 → 闭环断了。
      2. **preference_brief 渗透率**
         scrape 后被 preference_brief 影响（gate 过滤 / tier 调整）的岗位占比。
         0% = brief 是装饰；100% = brief 主导推送。
      3. **undo / 撤销率**
         用户撤销操作的频率 → 反推用户对系统决策的不满意度。
         撤销率 > 30% = 用户和系统在拉锯。
      4. **rejection nudge 应用率**
         被拒原因追踪 nudge 了 calibration 后，下次同类岗位 tier 是否上调？
    """
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import select, func
    from ..models import Job, JobMatch, ReflectionEvent

    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)

    # Signal 1: thumbs ↔ tier 闭环率
    thumbs_down_jobs = session.execute(
        select(ReflectionEvent.related_job_id).where(
            ReflectionEvent.kind == "job_thumbed_down",
            ReflectionEvent.created_at >= cutoff,
            ReflectionEvent.related_job_id.isnot(None),
        )
    ).scalars().all()
    thumbed_down_set = set(thumbs_down_jobs)

    # 之后 tier 是否 demote？看 tier_demoted 后续 event
    tier_demote_events = session.execute(
        select(ReflectionEvent).where(
            ReflectionEvent.kind.like("tier_%"),
            ReflectionEvent.created_at >= cutoff,
        )
    ).scalars().all()
    demote_followups = sum(
        1 for e in tier_demote_events
        if e.related_job_id in thumbed_down_set
    )
    closure_rate_thumbs = (
        demote_followups / len(thumbed_down_set) if thumbed_down_set else 0.0
    )

    # Signal 2: preference_brief 渗透率
    gate_events = session.execute(
        select(func.count(ReflectionEvent.id)).where(
            ReflectionEvent.kind.in_((
                "hard_gate_blocked",
                "preference_brief_extracted",
                "preference_brief_applied",
            )),
            ReflectionEvent.created_at >= cutoff,
        )
    ).scalar() or 0
    total_classify_events = session.execute(
        select(func.count(ReflectionEvent.id)).where(
            ReflectionEvent.kind.in_(("job_classified", "tier_demoted_by_salary_floor")),
            ReflectionEvent.created_at >= cutoff,
        )
    ).scalar() or 0
    brief_penetration = (
        gate_events / total_classify_events if total_classify_events else 0.0
    )

    # Signal 3: undo / 撤销率
    write_events = session.execute(
        select(func.count(ReflectionEvent.id)).where(
            ReflectionEvent.kind.in_((
                "job_status_changed", "rewrite_attempt_completed",
                "fact_corrected", "preference_brief_extracted",
            )),
            ReflectionEvent.created_at >= cutoff,
        )
    ).scalar() or 0
    undo_events = session.execute(
        select(func.count(ReflectionEvent.id)).where(
            ReflectionEvent.kind.like("%_undone"),
            ReflectionEvent.created_at >= cutoff,
        )
    ).scalar() or 0
    undo_rate = undo_events / write_events if write_events else 0.0

    # Signal 4: rejection nudge 应用率
    nudge_events = session.execute(
        select(ReflectionEvent).where(
            ReflectionEvent.kind == "calibration_nudge_from_rejection",
            ReflectionEvent.created_at >= cutoff,
        )
    ).scalars().all()
    # 简化：只看 nudge 是否记下来了；真正"应用"需要 tier_classifier 后续看到这些 nudge
    # （deferred 到 calibration_learner 集成）
    nudge_count = len(nudge_events)

    # Health score 综合（0-100）
    score_thumbs = min(closure_rate_thumbs * 100, 100)        # 闭环率高 = 好
    score_brief = min(brief_penetration * 100, 100)          # 渗透率高 = 好
    score_undo = max(0, 100 - undo_rate * 200)               # undo 率低 = 好（30%以下健康）
    score_nudge = min(nudge_count * 10, 100)                  # 有 nudge = 好（每条 nudge +10）
    health_score = round((score_thumbs + score_brief + score_undo + score_nudge) / 4)

    # Diagnostic notes
    notes = []
    if not thumbed_down_set:
        notes.append(f"过去 {days} 天没有 👎 — 推送可能太宽容（用户从不否定）或没用 Compass 推送")
    elif closure_rate_thumbs < 0.2:
        notes.append(
            f"thumbs ↔ tier 闭环率 {closure_rate_thumbs:.0%} 偏低 — "
            f"你 👎 了 {len(thumbed_down_set)} 个岗位，但只有 {demote_followups} 个被 demote。"
            f"系统在重复推送你已拒绝的类型。"
        )
    if brief_penetration < 0.05 and total_classify_events > 10:
        notes.append(
            f"preference_brief 渗透率 {brief_penetration:.0%} 太低 — brief 数据存在但 "
            f"tier_classifier / dealbreaker_gate 没真用上。检查 hard_gate_blocked 事件流。"
        )
    if undo_rate > 0.3:
        notes.append(
            f"undo 率 {undo_rate:.0%} 偏高 — 你和系统在拉锯。"
            f"是否需要让系统保守一档（提高决策门槛）？"
        )
    if nudge_count > 0 and undo_rate < 0.3:
        notes.append(f"✓ rejection 反馈 {nudge_count} 条已 nudge calibration（下轮 tier 应上调）")

    return {
        "days": days,
        "health_score": health_score,  # 0-100
        "thumbs_closure": {
            "rate": closure_rate_thumbs,
            "thumbed_down": len(thumbed_down_set),
            "demoted": demote_followups,
        },
        "brief_penetration": {
            "rate": brief_penetration,
            "gate_events": gate_events,
            "total_classify": total_classify_events,
        },
        "undo_rate": {
            "rate": undo_rate,
            "writes": write_events,
            "undos": undo_events,
        },
        "rejection_nudges": {
            "count": nudge_count,
        },
        "notes": notes,
    }
