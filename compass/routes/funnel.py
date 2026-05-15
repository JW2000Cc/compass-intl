"""Funnel routes — overall funnel + drill-down by tier."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from flask import Blueprint, render_template, request

from ..extensions import session_scope
from ..services.job_funnel import compute, compute_by_dimension, timeseries

bp = Blueprint("funnel", __name__, url_prefix="/funnel")


@bp.route("/")
def dashboard():
    days = request.args.get("days", default=30, type=int)
    since = datetime.now(timezone.utc) - timedelta(days=days)
    with session_scope() as session:
        counts = compute(session, since=since)
        ts = timeseries(session, days=days)
        by_source = compute_by_dimension(session, "source", since=since, top_n=8)
        by_location = compute_by_dimension(session, "location", since=since, top_n=8)
        by_role = compute_by_dimension(session, "role", since=since, top_n=10)
        return render_template(
            "funnel/dashboard.html",
            counts=counts,
            conversion=counts.conversion_rates(),
            timeseries=ts,
            days=days,
            by_source=by_source,
            by_location=by_location,
            by_role=by_role,
        )
