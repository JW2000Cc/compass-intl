"""Unified scrape result formatter — single source of truth for the
post-scrape flash banner. All three scrape entry points (jobs.scrape_now,
jobs.scrape_direct, search_config.scrape_all, per-profile scrape) MUST go
through `format_scrape_result` so users see consistent diagnostics.

Rationale (B1/B2): the historical jobs.scrape_now path silently dropped
gate_blocked count and mis-labeled blacklist as "dealbreaker", while
search_config.scrape_all formatted it correctly. Two divergent flash
strings = two debugging surfaces. One formatter = one place to fix.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ScrapeTotals:
    """Aggregated counters across one or more profile scrape runs."""

    raw: int = 0           # rows jobspy actually returned (pre-filter)
    new: int = 0           # inserted into DB
    updated: int = 0       # existed, refreshed fields
    blocked: int = 0       # legacy soft-blacklist filter
    gate_blocked: int = 0  # ADR-0016 dealbreaker hard gate
    classified: int = 0    # post-scrape tier_classifier ran
    profiles_run: int = 0  # how many enabled profiles were attempted
    failed_profiles: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)  # jobspy / per-step errors

    def absorb(self, result: dict) -> None:
        """Merge a per-profile result dict (from scraper.run_scrape_for_profile)."""
        self.raw += int(result.get("raw", 0) or 0)
        self.new += int(result.get("new", 0) or 0)
        self.updated += int(result.get("updated", 0) or 0)
        self.blocked += int(result.get("blocked", 0) or 0)
        self.gate_blocked += int(result.get("gate_blocked", 0) or 0)
        for e in result.get("errors", []) or []:
            if e:
                self.errors.append(str(e))


def format_scrape_result(
    totals: ScrapeTotals,
    *,
    title: str = "抓取完成",
) -> tuple[str, str]:
    """Render (msg, level) for flash().

    Level rules:
      · "ok"    — new>0 (something landed)
      · "warn"  — raw>0 but new==0 (jobspy worked, but everything filtered)
      · "warn"  — errors not empty even if some new succeeded
      · "error" — failed_profiles == profiles_run > 0 (everything broke)

    Diagnostic principle: when new==0 the user MUST be told why
    (raw count + gate_blocked count + first error head). Silence is the
    bug we're fixing.
    """
    parts: list[str] = [title]

    pipeline_bits = []
    pipeline_bits.append(f"jobspy 拿回 {totals.raw}")
    pipeline_bits.append(f"新增 {totals.new}")
    if totals.updated:
        pipeline_bits.append(f"更新 {totals.updated}")
    if totals.blocked:
        pipeline_bits.append(f"黑名单滤 {totals.blocked}")
    if totals.gate_blocked:
        pipeline_bits.append(f"dealbreaker 滤 {totals.gate_blocked}")
    if totals.classified:
        pipeline_bits.append(f"分级 {totals.classified}")

    parts.append(" · ".join(pipeline_bits))
    if totals.profiles_run:
        parts.append(f"({totals.profiles_run} 组运行)")

    if totals.failed_profiles:
        parts.append(f"· 失败: {', '.join(totals.failed_profiles)}")

    # Diagnostic when new==0 but jobspy returned data
    diagnostic = ""
    if totals.new == 0 and totals.raw > 0:
        reasons = []
        if totals.gate_blocked:
            reasons.append(
                f"全部被 dealbreaker 拦下 ({totals.gate_blocked} 条) — "
                "可能是 country_must_be / language_blocked / 关键词不命中"
            )
        if totals.blocked:
            reasons.append(f"黑名单滤掉 {totals.blocked} 条")
        if reasons:
            diagnostic = "⚠ " + "；".join(reasons) + "。点开抓取面板调整 dealbreaker / 关键词，或先去『求职意图』松开过滤"

    # Diagnostic when raw==0 (jobspy itself failed or no profiles ran)
    if totals.raw == 0 and totals.profiles_run > 0:
        if totals.errors:
            diagnostic = "⚠ jobspy 没拿到任何结果。错误: " + " · ".join(totals.errors[:2])
        else:
            diagnostic = "⚠ jobspy 没拿到任何结果（可能限流/网络问题）。稍后重试或换地区"

    msg = " ".join(parts)
    if diagnostic:
        msg = msg + " · " + diagnostic

    # Level
    if totals.profiles_run > 0 and len(totals.failed_profiles) == totals.profiles_run:
        level = "error"
    elif totals.new == 0 and totals.profiles_run > 0:
        level = "warn"
    elif totals.errors:
        level = "warn"
    else:
        level = "ok"
    return msg, level


def last_scrape_diagnostic(session) -> Optional[dict]:
    """Read the most recent `external_scrape` ReflectionEvent and return a
    UI-friendly diagnostic dict (or None if no scrape ever ran).

    Used by jobs/list.html empty-state to answer "why is this blank?"
    instead of the unhelpful "还没有岗位".

    Returns:
        {
          "when": datetime,
          "raw": int,
          "new": int,
          "blocked": int,
          "gate_blocked": int,
          "errors": [str],
          "headline": str,   # one-line summary for the empty state
          "fix_hint": str | None,
        }
    """
    from sqlalchemy import select
    from ..models import ReflectionEvent

    row = session.execute(
        select(ReflectionEvent)
        .where(ReflectionEvent.kind == "external_scrape")
        .order_by(ReflectionEvent.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if row is None:
        return None
    p = row.payload_json or {}
    raw = int(p.get("raw", 0) or 0)
    new = int(p.get("new", 0) or 0)
    blocked = int(p.get("blocked", 0) or 0)
    gate_blocked = int(p.get("gate_blocked", 0) or 0)
    errors = list(p.get("errors") or [])

    # Legacy events (pre-A2) have no `raw` key; payload_json sets it to 0 by
    # default. Don't say "还没跑过" when we actually have a row — that contradicts
    # the timestamp the UI also shows. Detect legacy by checking if payload had
    # no `raw` key at all.
    has_raw_key = "raw" in p
    if new == 0 and raw == 0 and errors:
        headline = "上次抓取 jobspy 没拿到任何结果（可能限流/网络）"
        fix = "稍后重试，或换关键词/地区"
    elif new == 0 and raw > 0 and gate_blocked >= raw:
        headline = f"上次抓取拿回 {raw} 条但被 dealbreaker 全过滤"
        fix = "去『求职意图』松一下 country_must_be / language_blocked / 关键词"
    elif new == 0 and raw > 0:
        headline = f"上次抓取拿回 {raw} 条但 {blocked} 黑名单 + {gate_blocked} dealbreaker 全过滤"
        fix = "调整黑名单或 dealbreaker"
    elif new > 0:
        headline = f"上次抓取入库 {new} 条（{raw} → 黑名单 {blocked} → dealbreaker {gate_blocked} → 入库 {new}）"
        fix = None
    elif not has_raw_key:
        # Legacy event — no `raw` field, can't reconstruct the funnel
        headline = "上次抓取无诊断信息（旧版数据）"
        fix = "再跑一次 ⚡ 一键抓取，新数据会有完整 raw → blocked → 入库 链路"
    else:
        headline = "上次抓取入库 0 条"
        fix = "可能没匹配到岗位 / dealbreaker 太严"

    return {
        "when": row.created_at,
        "raw": raw, "new": new,
        "blocked": blocked, "gate_blocked": gate_blocked,
        "errors": errors,
        "headline": headline,
        "fix_hint": fix,
    }
