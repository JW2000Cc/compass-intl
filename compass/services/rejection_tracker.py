"""被拒原因追踪 → 反喂 calibration（5-01 P1 第 4 条 / "funnel 金矿"）.

Two paths to feed:

  1. **Manual paste path**（5-07 MVP）— user pastes rejection email text into UI;
     LLM extracts reason; we log to ReflectionEvent + nudge calibration_learner.
  2. **Gmail auto-fetch**（future, requires OAuth）— scan inbox for rejection
     keywords, extract structured signal, feed to calibration. Deferred.

Reason categories (LLM tags into one of these):
  - skills_gap         技能差距（缺某项硬技能）
  - experience_gap     经验差距（年限 / 行业经验不够）
  - location_visa      地点 / 签证 / relocation
  - language           语言要求
  - culture_seniority  culture / seniority mismatch
  - filled_internally  内部填了 / 已不招
  - no_reason_given    没说原因（formal "we decided to go with another candidate"）
  - other              其他

calibration_learner 接收：
  - skills_gap × N  →  "这类岗位你 fact 缺 X 技能" → bullet 改写时优先扩 X
  - experience_gap × N → 把 tier_classifier 的 experience floor 提一档
  - 其他类别 → log only（不动 calibration）
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy.orm import Session

from ..models import Job, ReflectionEvent
from .llm import chat as _llm_chat

log = logging.getLogger(__name__)

REASON_CATEGORIES = (
    "skills_gap",
    "experience_gap",
    "location_visa",
    "language",
    "culture_seniority",
    "filled_internally",
    "no_reason_given",
    "other",
)


@dataclass
class RejectionAnalysis:
    category: str            # one of REASON_CATEGORIES
    confidence: float         # 0.0-1.0
    summary: str              # 1-2 句中文摘要
    skills_named: list[str] = field(default_factory=list)   # 提到缺哪些 skill
    quote: str = ""           # 拒信里的关键引文（trim, ≤200 char）


_PROMPT = """从拒信文本中提取拒绝原因，输出 JSON。

Categories（必须用其中一个）:
  - skills_gap: 缺硬技能（例如"need 5y Spark experience"）
  - experience_gap: 经验/年限不够（"need senior with 8+ years"）
  - location_visa: 地点 / 签证 / relocate
  - language: 语言要求
  - culture_seniority: culture fit / seniority 不对
  - filled_internally: 内部填了 / 已撤销
  - no_reason_given: 没给原因（"we decided to go with another candidate"）
  - other: 其他

输出 JSON ONLY:
{
  "category": "<one of above>",
  "confidence": <0.0-1.0>,
  "summary": "<1-2 句中文摘要>",
  "skills_named": ["skill1", "skill2"],
  "quote": "<拒信里关键引文 ≤200 char>"
}

拒信原文:
---
{text}
---
"""


def analyze_rejection(text: str, settings) -> RejectionAnalysis:
    """LLM 分析单封拒信，返回结构化结果。"""
    if not text or len(text.strip()) < 10:
        return RejectionAnalysis(
            category="no_reason_given",
            confidence=0.0,
            summary="文本太短，无法分析",
        )

    response = _llm_chat(
        system="You are a precise rejection-letter analyzer. Output JSON only.",
        user=_PROMPT.replace("{text}", text[:4000]),
        provider=settings.llm_provider,
        api_key=settings.llm_api_key,
        model=settings.llm_model,
        base_url=settings.llm_base_url,
        timeout=settings.llm_timeout,
    )
    raw = (response.text or "").strip()
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        if raw.startswith("json"):
            raw = raw[4:].lstrip()
        raw = raw.rsplit("```", 1)[0].strip()

    try:
        data = json.loads(raw)
    except (ValueError, IndexError) as exc:
        log.warning("rejection_tracker: LLM JSON parse failed: %s", exc)
        return RejectionAnalysis(
            category="other",
            confidence=0.0,
            summary=f"LLM 解析失败：{raw[:100]}",
        )

    cat = str(data.get("category", "other"))
    if cat not in REASON_CATEGORIES:
        cat = "other"

    return RejectionAnalysis(
        category=cat,
        confidence=float(data.get("confidence", 0.5)),
        summary=str(data.get("summary", "")),
        skills_named=[s for s in (data.get("skills_named") or []) if isinstance(s, str)],
        quote=str(data.get("quote", ""))[:200],
    )


def record_rejection(
    session: Session,
    job_id: str,
    rejection_text: str,
    analysis: RejectionAnalysis,
) -> ReflectionEvent:
    """写到 ReflectionEvent + 视情况 nudge calibration."""
    event = ReflectionEvent(
        kind="rejection_analyzed",
        related_job_id=job_id,
        payload_json={
            "job_id": job_id,
            "rejection_text_preview": rejection_text[:300],
            "category": analysis.category,
            "confidence": analysis.confidence,
            "summary": analysis.summary,
            "skills_named": analysis.skills_named,
            "quote": analysis.quote,
        },
    )
    session.add(event)

    # Nudge calibration_learner only for actionable categories
    if analysis.category in ("skills_gap", "experience_gap") and analysis.confidence >= 0.6:
        session.add(
            ReflectionEvent(
                kind="calibration_nudge_from_rejection",
                related_job_id=job_id,
                payload_json={
                    "job_id": job_id,
                    "category": analysis.category,
                    "skills_named": analysis.skills_named,
                    "rationale": (
                        f"被拒原因 = {analysis.category}（confidence={analysis.confidence}）"
                        f" → 后续 calibration 把这类岗位的 expectation 上调一档"
                    ),
                },
            )
        )

    return event


def aggregate_rejection_signals(session: Session, lookback_days: int = 90) -> dict:
    """Aggregate rejection categories over recent N days for dashboard."""
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import select

    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=lookback_days)
    rows = session.execute(
        select(ReflectionEvent).where(
            ReflectionEvent.kind == "rejection_analyzed",
            ReflectionEvent.created_at >= cutoff,
        )
    ).scalars().all()

    by_cat: dict[str, int] = {c: 0 for c in REASON_CATEGORIES}
    skills_counts: dict[str, int] = {}
    for r in rows:
        p = r.payload_json or {}
        cat = p.get("category", "other")
        by_cat[cat] = by_cat.get(cat, 0) + 1
        for s in (p.get("skills_named") or []):
            skills_counts[s] = skills_counts.get(s, 0) + 1

    top_skills = sorted(skills_counts.items(), key=lambda kv: -kv[1])[:10]
    return {
        "total": len(rows),
        "by_category": by_cat,
        "top_skills_named": top_skills,
        "lookback_days": lookback_days,
    }
