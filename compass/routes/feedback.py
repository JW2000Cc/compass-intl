"""Feedback routes — preference brief management.

Endpoints:
    GET  /feedback                   dashboard (default tab=jobs)
    POST /feedback/extract           run NL extraction → preview (returns HTML fragment)
    POST /feedback/save              save approved lines from preview
    POST /feedback/line/<id>/edit    edit an existing line
    POST /feedback/line/<id>/delete  delete a line (moves to archive)
    POST /feedback/line/<id>/extend  re-extend expiry +30d
    POST /feedback/archive/<idx>/reactivate  bring an archived line back
    POST /feedback/salary/upsert     create/update a region floor
    POST /feedback/salary/delete     remove a region floor
    POST /feedback/resolve           handle a stated-vs-revealed conflict
"""
from __future__ import annotations

import logging

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from flask_babel import gettext as _

from ..extensions import session_scope
from ..models import IdentityVersion, ReflectionEvent
from ..services.conflict_detector import all_conflicts
from ..services.llm import LLMError, LLMConfigError
from ..services.preference_brief import (
    Brief,
    HARD_CAP,
    SOFT_WARN,
    PER_AXIS_WARN,
    load_jobs_brief,
    load_resume_brief,
    save_jobs_brief,
    save_resume_brief,
)
from ..services.preference_extractor import (
    ExtractedLine,
    commit_extraction,
    extract,
)
from ..services.salary_minimums import load as load_salary
from ..services.salary_minimums import save as save_salary

bp = Blueprint("feedback", __name__, url_prefix="/feedback")

log = logging.getLogger(__name__)


def _resolve_lang(session) -> str:
    """User's resume language, fallback en."""
    iv = session.execute(
        __import__("sqlalchemy").select(IdentityVersion).where(IdentityVersion.is_current.is_(True))
    ).scalars().first()
    return (iv.language if iv and iv.language else "en")


def _save(brief: Brief) -> None:
    if brief.kind == "jobs":
        save_jobs_brief(brief)
    elif brief.kind == "resume":
        save_resume_brief(brief)


def _load(kind: str) -> Brief:
    return load_jobs_brief() if kind == "jobs" else load_resume_brief()


# ─────────────────────────────────────────────────────────
# Dashboard
# ─────────────────────────────────────────────────────────


@bp.route("/")
def dashboard():
    tab = request.args.get("tab", "jobs")
    if tab not in ("jobs", "resume"):
        tab = "jobs"
    is_htmx = request.headers.get("HX-Request") == "true"

    jobs_brief = load_jobs_brief()
    resume_brief = load_resume_brief()
    salary = load_salary()
    jobs_brief.prune_expired()
    resume_brief.prune_expired()
    save_jobs_brief(jobs_brief)
    save_resume_brief(resume_brief)
    conflicts = all_conflicts()

    ctx = {
        "tab": tab,
        "jobs_brief": jobs_brief,
        "resume_brief": resume_brief,
        "salary": salary,
        "conflicts": conflicts,
        "HARD_CAP": HARD_CAP,
        "SOFT_WARN": SOFT_WARN,
        "PER_AXIS_WARN": PER_AXIS_WARN,
    }

    if is_htmx and request.args.get("partial") == "tab":
        # Tab swap — return only the active tab partial
        if tab == "jobs":
            return render_template("feedback/_jobs_tab.html", **ctx)
        return render_template("feedback/_resume_tab.html", **ctx)

    return render_template("feedback/dashboard.html", **ctx)


# ─────────────────────────────────────────────────────────
# Extract + save flow
# ─────────────────────────────────────────────────────────


@bp.route("/extract", methods=["POST"])
def extract_preview():
    """Run LLM extraction on user's NL feedback → render preview HTML fragment.

    Form fields:
      context     — "jobs" or "resume"
      item_id     — job_id or variant_id
      item_label  — human label (e.g. "Sr PM @ Foobar")
      user_text   — raw NL feedback
      bullet_index (resume optional) — int
    """
    settings = current_app.config["SETTINGS"]
    is_htmx = request.headers.get("HX-Request") == "true"

    context = (request.form.get("context") or "").strip()
    item_id = (request.form.get("item_id") or "").strip()
    item_label = (request.form.get("item_label") or "").strip()
    user_text = (request.form.get("user_text") or "").strip()
    bullet_idx_raw = (request.form.get("bullet_index") or "").strip()
    bullet_index = int(bullet_idx_raw) if bullet_idx_raw.isdigit() else None

    if context not in ("jobs", "resume") or not user_text or not item_id:
        return ("缺少必要字段", 400) if is_htmx else (
            "Bad request", 400)

    if not settings.has_llm:
        return ("没有配置 LLM_API_KEY，无法做反馈提取", 400)

    brief = _load(context)
    with session_scope() as session:
        canonical_lang = _resolve_lang(session)

    try:
        result = extract(
            user_text=user_text,
            context=context,
            canonical_lang=canonical_lang,
            existing_lines=brief.active_lines(),
            provider=settings.llm_provider,
            api_key=settings.llm_api_key,
            model=settings.llm_model,
        )
    except LLMError as exc:
        return (f"LLM 配置错误: {exc}", 500)

    return render_template(
        "feedback/_extract_preview.html",
        result=result,
        context=context,
        item_id=item_id,
        item_label=item_label,
        user_text=user_text,
        bullet_index=bullet_index,
        existing_lines={l.id: l for l in brief.active_lines()},
    )


@bp.route("/save", methods=["POST"])
def save_lines():
    """Apply (possibly user-edited) extracted lines to brief.

    Form fields (one set per line, suffixed by index):
      context, item_id, item_label, user_text, bullet_index
      lines_count
      line_<i>_text, line_<i>_side, line_<i>_axis,
      line_<i>_scope, line_<i>_confidence, line_<i>_merge_into,
      line_<i>_keep ("1" if user kept this line)
    """
    settings = current_app.config["SETTINGS"]
    is_htmx = request.headers.get("HX-Request") == "true"

    context = (request.form.get("context") or "").strip()
    item_id = (request.form.get("item_id") or "").strip()
    item_label = (request.form.get("item_label") or "").strip()
    user_text = (request.form.get("user_text") or "").strip()
    bullet_idx_raw = (request.form.get("bullet_index") or "").strip()
    bullet_index = int(bullet_idx_raw) if bullet_idx_raw.isdigit() else None

    if context not in ("jobs", "resume"):
        return ("Bad context", 400)

    try:
        n = int(request.form.get("lines_count", "0"))
    except ValueError:
        n = 0

    kept_lines: list[ExtractedLine] = []
    for i in range(n):
        if request.form.get(f"line_{i}_keep") != "1":
            continue
        text = (request.form.get(f"line_{i}_text") or "").strip()[:60]
        side = (request.form.get(f"line_{i}_side") or "").strip()
        axis = (request.form.get(f"line_{i}_axis") or "").strip()
        scope = (request.form.get(f"line_{i}_scope") or "").strip() or None
        try:
            confidence = float(request.form.get(f"line_{i}_confidence", "0.7"))
        except ValueError:
            confidence = 0.7
        merge_into = (request.form.get(f"line_{i}_merge_into") or "").strip() or None
        if not text or side not in ("+", "-") or not axis:
            continue
        kept_lines.append(ExtractedLine(
            text=text, side=side, axis=axis, scope=scope,
            confidence=confidence, merge_into=merge_into,
        ))

    brief = _load(context)

    # Wrap into ExtractResult-shape for commit_extraction
    from ..services.preference_extractor import ExtractResult
    result = ExtractResult(lines=kept_lines, ambiguity_note=None, raw_response="")
    applied = commit_extraction(
        brief, result,
        item_id=item_id, item_label=item_label,
        user_original_text=user_text, bullet_index=bullet_index,
    )
    _save(brief)

    # Log a reflection event so the audit trail records what was extracted
    with session_scope() as s:
        s.add(ReflectionEvent(
            kind="preference_extracted",
            payload_json={
                "context": context,
                "item_id": item_id,
                "user_text": user_text[:300],
                "applied": [
                    {"action": a, "line_id": l.id, "text": l.text}
                    for l, a in applied
                ],
            },
            related_job_id=item_id if context == "jobs" else None,
        ))

    if is_htmx:
        # Return a small confirmation snippet for the originating widget to swap
        return render_template(
            "feedback/_save_confirmation.html",
            applied=applied,
        )
    flash(_("已保存 %(n)s 条偏好。", n=len(applied)), "ok")
    return redirect(url_for("feedback.dashboard", tab=context))


# ─────────────────────────────────────────────────────────
# Line CRUD
# ─────────────────────────────────────────────────────────


def _resolve_brief_for_line(line_id: str) -> tuple[Brief, str] | None:
    """Find which brief owns this line. Returns (brief, kind) or None."""
    jb = load_jobs_brief()
    if jb.find(line_id):
        return jb, "jobs"
    rb = load_resume_brief()
    if rb.find(line_id):
        return rb, "resume"
    return None


@bp.route("/line/<line_id>/edit", methods=["POST"])
def edit_line(line_id: str):
    found = _resolve_brief_for_line(line_id)
    if not found:
        return ("Not found", 404)
    brief, kind = found

    text = (request.form.get("text") or "").strip()[:60]
    scope = (request.form.get("scope") or "").strip() or None
    side = (request.form.get("side") or "").strip()
    if side and side not in ("+", "-"):
        return ("Bad side", 400)

    line = brief.edit_line(line_id, text=text or None, scope=scope, side=side or None)
    if not line:
        return ("Not found", 404)
    _save(brief)

    if request.headers.get("HX-Request") == "true":
        return render_template("feedback/_line_card.html", line=line, kind=kind)
    flash(_("已更新偏好。"), "ok")
    return redirect(url_for("feedback.dashboard", tab=kind))


@bp.route("/line/<line_id>/delete", methods=["POST"])
def delete_line(line_id: str):
    found = _resolve_brief_for_line(line_id)
    if not found:
        return ("Not found", 404)
    brief, kind = found

    if not brief.delete_line(line_id):
        return ("Not found", 404)
    _save(brief)

    if request.headers.get("HX-Request") == "true":
        # Empty body removes the card from the DOM
        return ("", 200)
    flash(_("已删除偏好。"), "ok")
    return redirect(url_for("feedback.dashboard", tab=kind))


@bp.route("/line/<line_id>/extend", methods=["POST"])
def extend_line(line_id: str):
    found = _resolve_brief_for_line(line_id)
    if not found:
        return ("Not found", 404)
    brief, kind = found

    line = brief.extend_line(line_id, days=30)
    if not line:
        return ("Not found", 404)
    _save(brief)

    if request.headers.get("HX-Request") == "true":
        return render_template("feedback/_line_card.html", line=line, kind=kind)
    flash(_("已续期 30 天。"), "ok")
    return redirect(url_for("feedback.dashboard", tab=kind))


@bp.route("/archive/<kind>/<int:idx>/reactivate", methods=["POST"])
def reactivate_archived(kind: str, idx: int):
    if kind not in ("jobs", "resume"):
        return ("Bad kind", 400)
    brief = _load(kind)
    line = brief.reactivate_archived(idx)
    if not line:
        return ("Not found", 404)
    _save(brief)
    flash(_("已重新激活该偏好。"), "ok")
    return redirect(url_for("feedback.dashboard", tab=kind))


# ─────────────────────────────────────────────────────────
# Salary minimums CRUD (jobs only)
# ─────────────────────────────────────────────────────────


@bp.route("/salary/upsert", methods=["POST"])
def salary_upsert():
    region = (request.form.get("region") or "").strip()
    if not region:
        return ("region required", 400)
    currency = (request.form.get("currency") or "EUR").strip().upper() or "EUR"
    monthly_raw = (request.form.get("monthly_min") or "").strip()
    yearly_raw = (request.form.get("yearly_min") or "").strip()
    monthly_min = float(monthly_raw) if monthly_raw else None
    yearly_min = float(yearly_raw) if yearly_raw else None
    note = (request.form.get("note") or "").strip()[:200]

    table = load_salary()
    table.upsert(region, currency=currency, monthly_min=monthly_min,
                 yearly_min=yearly_min, note=note)
    save_salary(table)
    flash(_("已设置 %(region)s 工资底线。", region=region), "ok")
    return redirect(url_for("feedback.dashboard", tab="jobs"))


@bp.route("/salary/delete", methods=["POST"])
def salary_delete():
    region = (request.form.get("region") or "").strip()
    if not region:
        return ("region required", 400)
    table = load_salary()
    if table.remove(region):
        save_salary(table)
        flash(_("已移除 %(region)s 工资底线。", region=region), "ok")
    return redirect(url_for("feedback.dashboard", tab="jobs"))


# ─────────────────────────────────────────────────────────
# Conflict resolution
# ─────────────────────────────────────────────────────────


@bp.route("/resolve", methods=["POST"])
def resolve_conflict():
    """User picked a resolution for a stated-vs-revealed conflict.

    Form fields:
      line_id      — the brief line that conflicts
      resolution   — "update_stated" | "narrow_brief" | "acknowledge"
      narrow_text  — if narrow_brief, the new scoped text
    """
    line_id = (request.form.get("line_id") or "").strip()
    resolution = (request.form.get("resolution") or "").strip()
    if resolution not in ("update_stated", "narrow_brief", "acknowledge"):
        return ("bad resolution", 400)

    found = _resolve_brief_for_line(line_id)
    if not found:
        return ("Line not found", 404)
    brief, kind = found
    line = brief.find(line_id)

    if resolution == "narrow_brief":
        narrow_text = (request.form.get("narrow_text") or "").strip()[:60]
        if narrow_text:
            line.text = narrow_text
        narrow_scope = (request.form.get("narrow_scope") or "").strip()[:80]
        if narrow_scope:
            line.scope = narrow_scope
        _save(brief)

    if resolution == "acknowledge":
        # Just log the acknowledgment; no data change
        pass

    with session_scope() as s:
        s.add(ReflectionEvent(
            kind="stated_vs_revealed_resolved",
            payload_json={
                "line_id": line_id,
                "line_text": line.text,
                "resolution": resolution,
            },
            related_fact_id=None,
        ))

    flash(_("已记录解决方式: %(resolution)s", resolution=resolution), "ok")
    if resolution == "update_stated":
        # Send user back to scrape config (they need to manually edit keywords)
        return redirect(url_for("jobs.list_view"))
    return redirect(url_for("feedback.dashboard", tab=kind))
