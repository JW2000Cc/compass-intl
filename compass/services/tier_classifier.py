"""Tier classifier — replaces v1's binary push/skip with a 5-tier model.

──────────────────────────────────────────────────────────
Why this exists  (see docs/decisions/0002-five-tier-not-binary.md)
──────────────────────────────────────────────────────────
v1 输出 push/skip 二元判断——抛弃了 90% 的信息。一个边缘可能岗位和
一个完美匹配在 push 这一边没有任何区别，前端只能按 created_at 排序。

Compass 用 5-tier + tier 内 score 替代：
  Tier 是类型判断（LLM 能稳定区分），score 是同 tier 内细分。
  Tier 5 是 "客观不可达" 的 hard 标签，正面对应"工程师 ≠ 调酒师"教训。
  Tier 2 是 "邻近迁移" 的反茧房甜点区——通过 70/20/10 配额显式保护。

──────────────────────────────────────────────────────────
Tier 定义
──────────────────────────────────────────────────────────
Tier 1: core professional (matches background directly)
Tier 2: adjacent migration (transferable; the anti-bubble sweet spot)
Tier 3: learnable extension (need to learn 1-2 things; learning_gap filled)
Tier 4: distant migration (only push if user signals interest)
Tier 5: objectively unreachable (NEVER pushed; stored for transparency)

──────────────────────────────────────────────────────────
设计要点
──────────────────────────────────────────────────────────
- 没 LLM key → 默认 Tier 5（refuse-to-push 姿态，宁可不推也不瞎推）
- learning_gap 只在 tier 3 时保留（其它强制 null）
- _coerce_tier / _coerce_score 防 LLM 返回奇怪值
- 70/20/10 配额是 ε-exploration 的具象化（见 daily_push_distribution）
- system prompt 里的 "工程师 ≠ 调酒师" 规则是非协商的——v1 血泪教训

输入：user identity (current IdentityVersion + active facts) + UserCalibration
       + global target_doc + JD
输出：JobMatch row + ReflectionEvent("job_classified")
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    IdentityVersion,
    Job,
    JobMatch,
    ReflectionEvent,
    ResumeFact,
    UserCalibration,
)
from .llm import LLMConfigError, LLMError, chat

log = logging.getLogger(__name__)


SYSTEM_PROMPT = """You are Compass — a tier-classification engine for jobs.

Your task: read the user's identity (resume facts + stated direction) and a
single job posting, then output a TIER (1–5) plus a 0–10 score, a one-sentence
reason, and (only for tier 3) a learning_gap.

TIER DEFINITIONS — apply STRICTLY:

  Tier 1 — CORE PROFESSIONAL FIT
    The job's CORE day-to-day work directly matches the user's background.
    Ex: user is a financial analyst → analyst at investment bank.

  Tier 2 — ADJACENT MIGRATION (anti-bubble sweet spot)
    Different surface role, but the user's existing skills transfer naturally.
    Same big domain, different angle.
    Ex: financial analyst → fintech product manager / quant research / strategy
    consulting in finance practice.
    MOST users underestimate Tier 2; surface generously.

  Tier 3 — LEARNABLE EXTENSION
    User would need to learn 1–2 specific things to be a real fit.
    Always fill `learning_gap` with what's missing AND a rough months estimate.
    Ex: user is financial analyst, JD wants data scientist with ML production
    experience → learning_gap="ML production stack (PyTorch + MLOps), ~6mo"

  Tier 4 — DISTANT MIGRATION
    Different domain entirely, but a path exists.
    Surface only if other signals suggest user is exploring this direction
    (recent thumbs-ups in this area, etc.). Otherwise default to Tier 5.

  Tier 5 — OBJECTIVELY UNREACHABLE — NEVER PUSH
    User cannot be a real fit:
    - hard credential mismatch (no PhD/MD/JD/license)
    - visa/citizenship hard requirement
    - native-language hard requirement
    - completely unrelated industry (engineer → bartender, finance → nurse)
    Be DECISIVE: if the user is an engineer, NEVER tier the bartender job
    above 5. The "工程师 ≠ 调酒师" rule is non-negotiable.

SCORE (0–10): within the assigned tier, how strong is the fit?
  Tier 1: typically 6–10
  Tier 2: typically 4–8
  Tier 3: typically 3–6
  Tier 4: typically 2–4
  Tier 5: 0

REASON: ONE sentence. State which tier rule was hit.

Output ONLY JSON, no commentary:
{
  "tier": 1|2|3|4|5,
  "score": 0-10,
  "reason": "one sentence",
  "learning_gap": "(only for tier 3) what to learn + months estimate, else null"
}
"""


def _build_user_payload(
    job: Job,
    identity: Optional[IdentityVersion],
    facts: list[ResumeFact],
    calibration: Optional[UserCalibration],
    target_doc: str,
    profile=None,  # ADR-0016: ProfileConfig
) -> str:
    parts: list[str] = []

    # ADR-0016: profile context comes FIRST — it sets the frame for everything
    # downstream. Without this the LLM rates jobs against the user's full resume
    # diversity (electrical + finance + data) and gives Tier 1 to anything in
    # any领域. With this it judges fit to THIS profile's intent.
    if profile is not None:
        block = [f"## Currently active job-search profile: {profile.label}"]
        if profile.user_prompt:
            block.append(
                "User's stated direction for THIS profile:\n"
                + profile.user_prompt[:600]
            )
        if profile.keywords:
            block.append(
                "Active keywords (the user is specifically searching for): "
                + ", ".join(profile.keywords[:20])
            )
        if profile.dealbreakers.country_must_be:
            block.append(
                "Country constraint (already pre-filtered, here for context): "
                + ", ".join(profile.dealbreakers.country_must_be)
            )
        block.append(
            "**Frame**: judge tier by fit to THIS profile's intent. "
            "A job that matches the user's background but is OUTSIDE this "
            "profile's keywords/role should be Tier 4 or 5, even if the user "
            "has experience that could technically apply."
        )
        parts.append("\n\n".join(block))

    if target_doc:
        parts.append(f"## User's stated direction (global)\n{target_doc.strip()[:1500]}")

    if facts:
        fact_lines = []
        for f in facts[:60]:
            fact_lines.append(f"- [{f.kind}] {f.text[:200]}")
        parts.append("## User facts (current identity)\n" + "\n".join(fact_lines))

    if calibration and calibration.sensitive_categories_locked:
        parts.append(
            "## Sensitive categories (the user has hard-locked these — never overstate):\n"
            + ", ".join(calibration.sensitive_categories_locked)
        )

    parts.append(
        "## Job posting\n"
        f"Title: {job.title}\n"
        f"Company: {job.company}\n"
        f"Location: {job.location or 'unspecified'}\n"
        f"Source: {job.source or 'unknown'}\n"
        f"Remote: {'yes' if job.is_remote else 'no'}\n"
        f"Description:\n{(job.description or '')[:2500]}"
    )
    return "\n\n".join(parts)


def _autoload_profile_from_job(job: Job):
    """Best-effort: if job.matched_profile_id is set, try loading the profile
    from job_search_config.json. Returns None on any failure (no Flask context,
    file missing, etc.) — callers fall back to legacy behavior."""
    if not getattr(job, "matched_profile_id", None):
        return None
    try:
        from pathlib import Path

        from flask import current_app

        from .scrape_config import load as load_intent

        data_dir = current_app.config["SETTINGS"].data_dir
        intent = load_intent(data_dir)
        return intent.find(job.matched_profile_id)
    except Exception as exc:  # noqa: BLE001
        log.debug("autoload profile %s failed: %s", job.matched_profile_id, exc)
        return None


def classify_job(
    session: Session,
    job: Job,
    *,
    provider: str,
    api_key: str,
    model: str,
    target_doc: str = "",
    profile=None,  # ADR-0016: ProfileConfig — overrides auto-load
) -> JobMatch:
    """Run the tier classifier and persist a JobMatch row.

    Falls back to Tier 5 (and an explanatory reason) if no LLM is available —
    Compass refuses to push without genuine analysis.

    profile: ADR-0016 ProfileConfig. If None, attempts to auto-load from
        job.matched_profile_id via job_search_config.json (Flask context required).
        When loaded, frames the LLM judgment around THIS profile's intent rather
        than the user's full resume — solves "啥岗位都有" by giving the
        classifier a narrower lens than "match anything that touches user facts".
    """
    if profile is None:
        profile = _autoload_profile_from_job(job)
    identity = (
        session.execute(select(IdentityVersion).where(IdentityVersion.is_current.is_(True)))
        .scalars()
        .first()
    )
    facts: list[ResumeFact] = []
    if identity:
        facts = (
            session.execute(
                select(ResumeFact).where(
                    ResumeFact.identity_id == identity.id, ResumeFact.active.is_(True)
                )
            )
            .scalars()
            .all()
        )
    calibration = session.get(UserCalibration, 1)

    if not api_key:
        log.warning("no LLM key — defaulting to Tier 5 (refuse-to-push posture)")
        match = JobMatch(
            job_id=job.id,
            tier=5,
            score=0,
            reason="No LLM configured; Compass refuses to push without analysis.",
            model="no_llm",
        )
        session.add(match)
        session.add(
            ReflectionEvent(
                kind="job_classified_skipped_no_llm",
                payload_json={"job_id": job.id, "title": job.title},
                related_job_id=job.id,
            )
        )
        session.flush()  # autoflush is off; flush so callers can query the new match
        return match

    user_payload = _build_user_payload(
        job, identity, facts, calibration, target_doc, profile=profile
    )

    # Inject preference_brief (jobs) + salary minimums into the system prompt.
    # Both are user-editable, transparent, and bridge multilingual semantics
    # naturally through the LLM. Empty if user hasn't given any feedback yet.
    from .preference_brief import load_jobs_brief
    from .salary_minimums import load as load_salary_minimums
    jobs_brief = load_jobs_brief()
    salary_table = load_salary_minimums()
    brief_block = jobs_brief.format_for_prompt()
    salary_block = salary_table.format_for_prompt()

    system_prompt = SYSTEM_PROMPT
    if brief_block:
        system_prompt = SYSTEM_PROMPT + "\n\n" + brief_block
    if salary_block:
        system_prompt += "\n\n" + salary_block

    try:
        response = chat(
            provider=provider,
            api_key=api_key,
            model=model,
            system=system_prompt,
            user_content=user_payload,
            max_tokens=400,
        )
    except LLMError as exc:
        log.warning("classifier LLM config error: %s — falling back to Tier 5", exc)
        match = JobMatch(
            job_id=job.id, tier=5, score=0, reason=f"LLM error: {exc}", model="error"
        )
        session.add(match)
        session.flush()  # autoflush off; flush so caller can query
        return match

    parsed = response.parse_json(default={})
    tier = _coerce_tier(parsed.get("tier"))
    score = _coerce_score(parsed.get("score"))
    reason = str(parsed.get("reason", ""))[:500]
    learning_gap = parsed.get("learning_gap")
    if learning_gap is not None and not isinstance(learning_gap, str):
        learning_gap = None
    if tier != 3:
        learning_gap = None  # spec: only tier 3 keeps gap

    # Salary floor deterministic demotion: if user has set a per-region floor
    # AND the job's salary is below it, demote one tier (don't go past 5).
    # This is a hard rule on top of LLM judgment — symbolic floors don't
    # require the LLM to remember/apply them correctly every time.
    floor_eval = salary_table.evaluate(
        job_location=job.location,
        job_salary_min=job.salary_min,
        job_salary_currency=job.salary_currency,
    )
    if floor_eval["below_floor"]:
        old_tier = tier
        tier = min(5, tier + floor_eval["demote_recommendation"])
        if tier != old_tier:
            reason = f"[salary-floor: {floor_eval['note']}] " + reason
            session.add(
                ReflectionEvent(
                    kind="tier_demoted_by_salary_floor",
                    payload_json={
                        "job_id": job.id, "old_tier": old_tier, "new_tier": tier,
                        "floor_note": floor_eval["note"],
                    },
                    related_job_id=job.id,
                )
            )

    # ─── ε-exploration（5-01 P1 第 8 条 / 反茧房二阶）───
    # 以 ε 概率把 tier 3/4 的岗位"提升"到 tier 2（邻近迁移甜点），让用户偶尔
    # 看到不在舒适区的岗位。配置：settings.epsilon_exploration（0.0-1.0，默认 0）。
    # 真实 tier 保留在 reason 里给用户看，可解释、可推翻。
    try:
        import random as _r
        eps = float(getattr(settings, "epsilon_exploration", 0.0) or 0.0)
        if eps > 0 and tier in (3, 4) and _r.random() < eps:
            old_tier_eps = tier
            tier = 2
            reason = f"[ε-explore: 反茧房随机提升 tier {old_tier_eps} → 2] " + reason
            session.add(
                ReflectionEvent(
                    kind="epsilon_exploration_triggered",
                    payload_json={
                        "job_id": job.id, "real_tier": old_tier_eps, "shown_tier": 2,
                        "epsilon": eps,
                    },
                    related_job_id=job.id,
                )
            )
    except Exception:
        pass

    match = JobMatch(
        job_id=job.id,
        tier=tier,
        score=score,
        reason=reason,
        learning_gap=learning_gap,
        model=response.model,
    )
    session.add(match)
    session.add(
        ReflectionEvent(
            kind="job_classified",
            payload_json={
                "job_id": job.id,
                "title": job.title,
                "tier": tier,
                "score": score,
                "reason": reason,
            },
            related_job_id=job.id,
        )
    )
    session.flush()  # autoflush off; flush so caller can query
    return match


def _coerce_tier(v) -> int:
    try:
        t = int(v)
    except (ValueError, TypeError):
        return 5
    return max(1, min(5, t))


def _coerce_score(v) -> int:
    try:
        s = int(v)
    except (ValueError, TypeError):
        return 0
    return max(0, min(10, s))


# ─────────────────────────────────────────────────────────
# Distribution policy — tier-aware push quotas
# ─────────────────────────────────────────────────────────


DEFAULT_QUOTAS = {1: 0.70, 2: 0.20, 3: 0.10, 4: 0.0, 5: 0.0}


def daily_push_distribution(total_slots: int = 30) -> dict[int, int]:
    """How many slots each tier gets in today's push.

    Defaults reflect "70% core / 20% adjacent (anti-bubble) / 10% learnable".
    Tier 4 only fires when the calibration learner detects user signals;
    Tier 5 never pushes.
    """
    return {tier: max(0, round(total_slots * frac)) for tier, frac in DEFAULT_QUOTAS.items()}
