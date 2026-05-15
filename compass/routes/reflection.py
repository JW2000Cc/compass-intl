"""Reflection layer routes — Phase 0.

GET  /reflect/dashboard         drift dashboard (read-only)
GET  /reflect/assumptions       tool-assumptions report
POST /reflect/assumptions/regen regenerate (LLM call)
POST /reflect/assumptions/override/<id>   set/clear user override
POST /reflect/snapshot          capture a calibration drift point now
"""
from __future__ import annotations

import logging

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from flask_babel import gettext as _

from ..extensions import session_scope
from ..services.llm import LLMError, LLMConfigError
from ..services.reflection_engine import (
    build_drift_report,
    get_current_assumptions,
    override_assumption,
    regenerate_assumptions,
    stated_vs_revealed,
    take_drift_snapshot,
)

log = logging.getLogger(__name__)

bp = Blueprint("reflection", __name__, url_prefix="/reflect")


@bp.route("/dashboard")
def dashboard():
    from ..services.job_funnel import epsilon_exploration_summary
    from ..services.reflection_engine import flywheel_loop_health, tier_intent_vs_action
    from ..services.rejection_tracker import aggregate_rejection_signals
    from ..services import scrape_config as _scrape_config

    with session_scope() as session:
        settings = current_app.config["SETTINGS"]
        report = build_drift_report(session, review_interval_days=settings.drift_check_interval_days)
        divergence = stated_vs_revealed(session)
        epsilon = epsilon_exploration_summary(session, days=30)
        # 5-07 Reflective Flywheel 完整闭环 4 信号 + rejection 累积
        flywheel = flywheel_loop_health(session, days=90)
        rejection_agg = aggregate_rejection_signals(session, lookback_days=90)

        # tier_quota stated vs revealed (反身性距离)
        try:
            intent = _scrape_config.load(settings.data_dir)
        except Exception:
            intent = None
        intent_vs_action = tier_intent_vs_action(session, intent, days=60)

        series = [
            {"t": p.captured_at.isoformat(), "level": p.max_amplification_level}
            for p in report.points
        ]

        return render_template(
            "reflection/dashboard.html",
            report=report,
            divergence=divergence,
            epsilon=epsilon,
            flywheel=flywheel,
            rejection_agg=rejection_agg,
            intent_vs_action=intent_vs_action,
            series=series,
            llm_configured=settings.has_llm,
        )


@bp.route("/snapshot", methods=["POST"])
def snapshot_now():
    with session_scope() as session:
        take_drift_snapshot(session)
    flash(_("已记录当前 calibration 快照。"), "ok")
    return redirect(url_for("reflection.dashboard"))


@bp.route("/assumptions")
def assumptions():
    with session_scope() as session:
        rows = get_current_assumptions(session)
        settings = current_app.config["SETTINGS"]
        return render_template(
            "reflection/assumptions.html",
            assumptions=rows,
            llm_configured=settings.has_llm,
        )


@bp.route("/assumptions/regen", methods=["POST"])
def regen_assumptions():
    settings = current_app.config["SETTINGS"]
    try:
        with session_scope() as session:
            rows = regenerate_assumptions(
                session,
                provider=settings.llm_provider,
                api_key=settings.llm_api_key,
                model=settings.llm_model,
            )
        flash(_("已重新生成 %(n)s 条系统假设。", n=len(rows)), "ok")
    except LLMError as exc:
        flash(_("无法重生成假设: %(exc)s", exc=exc), "error")
    except Exception as exc:  # noqa: BLE001
        log.exception("regen_assumptions failed")
        flash(_("出错: %(exc)s", exc=exc), "error")
    return redirect(url_for("reflection.assumptions"))


@bp.route("/assumptions/override/<assumption_id>", methods=["POST"])
def override(assumption_id: str):
    text = (request.form.get("override") or "").strip() or None
    with session_scope() as session:
        override_assumption(session, assumption_id, text)
    flash(_("已记录你的修正。"), "ok")
    return redirect(url_for("reflection.assumptions"))
