"""Calibration routes — view + manually edit boundaries, trigger learning."""
from __future__ import annotations

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_babel import gettext as _

from ..extensions import session_scope
from ..services.calibration_learner import (
    _ensure_calibration,
    detect_drift,
    learn_from_recent,
)
from ..services.reflection_engine import take_drift_snapshot

bp = Blueprint("calibration", __name__, url_prefix="/calibration")


@bp.route("/")
def overview():
    with session_scope() as session:
        cal = _ensure_calibration(session)
        drift_alert = detect_drift(session)
        return render_template(
            "calibration/overview.html",
            cal=cal,
            drift_alert=drift_alert,
        )


@bp.route("/edit", methods=["POST"])
def edit():
    """User directly edits calibration. Sticky against learner overrides until next reset."""
    with session_scope() as session:
        cal = _ensure_calibration(session)
        try:
            new_level = int(request.form.get("max_level", cal.max_amplification_level))
        except ValueError:
            new_level = cal.max_amplification_level
        cal.max_amplification_level = max(0, min(4, new_level))  # 5 is hard ban — never settable

        avoid_raw = (request.form.get("avoid", "") or "").strip()
        do_raw = (request.form.get("do", "") or "").strip()
        cal.preferred_verbs_avoid = sorted({v.strip().lower() for v in avoid_raw.split(",") if v.strip()})
        cal.preferred_verbs_do = sorted({v.strip().lower() for v in do_raw.split(",") if v.strip()} - set(cal.preferred_verbs_avoid))

        locked_raw = (request.form.get("locked", "") or "").strip()
        cal.sensitive_categories_locked = sorted({c.strip() for c in locked_raw.split(",") if c.strip()})
    flash(_("已更新 calibration。"), "ok")
    return redirect(url_for("calibration.overview"))


@bp.route("/relearn", methods=["POST"])
def relearn():
    with session_scope() as session:
        delta = learn_from_recent(session, lookback_days=30)
        take_drift_snapshot(session)
    flash(
        f"已从最近 30 天的决策重新学习。Approved={delta['approved']} Rejected={delta['rejected']} Edited={delta['edited']}.",
        "ok",
    )
    return redirect(url_for("calibration.overview"))


@bp.route("/reset", methods=["POST"])
def reset():
    """Reset to neutral defaults. Logged as a value-regression checkpoint."""
    from ..models import ReflectionEvent

    with session_scope() as session:
        cal = _ensure_calibration(session)
        prior = {
            "max_level": cal.max_amplification_level,
            "do": list(cal.preferred_verbs_do or []),
            "avoid": list(cal.preferred_verbs_avoid or []),
            "locked": list(cal.sensitive_categories_locked or []),
        }
        cal.max_amplification_level = 2
        cal.preferred_verbs_do = []
        cal.preferred_verbs_avoid = []
        cal.sensitive_categories_locked = []
        cal.style_rules = []
        from datetime import datetime, timezone

        cal.last_review_at = datetime.now(timezone.utc)
        session.add(
            ReflectionEvent(
                kind="value_regression_checkpoint",
                payload_json={"prior": prior, "reset_to": "neutral defaults"},
            )
        )
    flash(_("已重置 calibration 到中性默认值。"), "ok")
    return redirect(url_for("calibration.overview"))
