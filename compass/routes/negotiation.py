"""Salary negotiation simulator route (5-01 Stage 3).

GET  /negotiation/             — input form
POST /negotiation/simulate     — LLM 模拟 + 渲染结果
"""
from __future__ import annotations

import logging

from flask import Blueprint, current_app, render_template, request

from ..extensions import session_scope
from ..models import ReflectionEvent
from ..services.salary_negotiation import NegotiationInput, simulate_negotiation

log = logging.getLogger(__name__)

bp = Blueprint("negotiation", __name__, url_prefix="/negotiation")


@bp.route("/")
def form():
    return render_template("negotiation/form.html")


@bp.route("/simulate", methods=["POST"])
def simulate():
    settings = current_app.config["SETTINGS"]

    try:
        offer_amount = float(request.form.get("offer_amount") or 0)
    except ValueError:
        offer_amount = 0
    if offer_amount <= 0:
        return render_template(
            "negotiation/form.html",
            error="请输入有效的 offer 金额（数字 > 0）",
        )

    inp = NegotiationInput(
        offer_amount=offer_amount,
        currency=(request.form.get("currency") or "EUR").strip().upper(),
        period=(request.form.get("period") or "monthly").strip(),
        city=(request.form.get("city") or "").strip(),
        country=(request.form.get("country") or "").strip(),
        role=(request.form.get("role") or "").strip(),
        seniority=(request.form.get("seniority") or "mid").strip(),
        your_minimum=(
            float(request.form.get("your_minimum") or 0) or None
            if (request.form.get("your_minimum") or "").strip()
            else None
        ),
        notes=(request.form.get("notes") or "").strip(),
    )

    try:
        result = simulate_negotiation(inp, settings)
    except Exception as exc:  # noqa: BLE001
        log.exception("negotiation simulate failed")
        return render_template(
            "negotiation/form.html",
            error=f"LLM 调用失败：{exc}",
            input=inp,
        )

    # Log to ReflectionEvent (用户能在 dashboard 看到使用历史)
    try:
        with session_scope() as session:
            session.add(
                ReflectionEvent(
                    kind="salary_negotiation_simulated",
                    payload_json={
                        "offer": offer_amount,
                        "currency": inp.currency,
                        "city": inp.city,
                        "role": inp.role,
                        "askable_high": result.askable_range_high,
                        "offer_position": result.offer_position,
                    },
                )
            )
    except Exception:
        log.exception("failed to log salary_negotiation_simulated event")

    return render_template("negotiation/result.html", input=inp, result=result)
