"""被拒原因追踪路由（5-01 P1 第 4 条）.

GET  /rejection/                — 列表 + paste 入口
POST /rejection/analyze         — 分析一封拒信，记 ReflectionEvent + 显示结果
"""
from __future__ import annotations

import logging

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from flask_babel import gettext as _

from ..extensions import session_scope
from ..models import Job
from ..services.rejection_tracker import (
    aggregate_rejection_signals,
    analyze_rejection,
    record_rejection,
)

log = logging.getLogger(__name__)

bp = Blueprint("rejection", __name__, url_prefix="/rejection")


@bp.route("/")
def overview():
    with session_scope() as session:
        agg = aggregate_rejection_signals(session, lookback_days=90)
        # rejected jobs from Job table for paste hint
        from sqlalchemy import select
        rejected = session.execute(
            select(Job).where(Job.status == "rejected").order_by(Job.status_changed_at.desc()).limit(20)
        ).scalars().all()
        rejected_data = [(j.id, j.title, j.company) for j in rejected]
    return render_template(
        "rejection/overview.html",
        agg=agg,
        rejected_jobs=rejected_data,
    )


@bp.route("/analyze", methods=["POST"])
def analyze():
    settings = current_app.config["SETTINGS"]
    job_id = (request.form.get("job_id") or "").strip()
    text = (request.form.get("rejection_text") or "").strip()

    if not text:
        flash(_("请粘贴拒信文本"), "error")
        return redirect(url_for("rejection.overview"))

    try:
        result = analyze_rejection(text, settings)
    except Exception as exc:  # noqa: BLE001
        log.exception("rejection analyze failed")
        flash(_("LLM 调用失败：%(exc)s", exc=exc), "error")
        return redirect(url_for("rejection.overview"))

    with session_scope() as session:
        record_rejection(session, job_id or "ad-hoc", text, result)

    return render_template(
        "rejection/result.html",
        result=result,
        job_id=job_id,
        rejection_text=text,
    )
