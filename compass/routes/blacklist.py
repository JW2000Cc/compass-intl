"""Blacklist management — companies + keywords to filter out at scrape time."""
from __future__ import annotations

from flask import (
    Blueprint, current_app, flash, jsonify, redirect, render_template, request, url_for,
)
from flask_babel import gettext as _

from ..extensions import session_scope
from ..services.blacklist import (
    effectiveness_audit,
    from_natural_language,
    load,
    save,
)

bp = Blueprint("blacklist", __name__, url_prefix="/blacklist")


@bp.route("/", methods=["GET"])
def overview():
    settings = current_app.config["SETTINGS"]
    bl = load(settings.data_dir)
    with session_scope() as session:
        audit = effectiveness_audit(session, days=30)
    return render_template("blacklist/overview.html", bl=bl, audit=audit)


@bp.route("/", methods=["POST"])
def update():
    settings = current_app.config["SETTINGS"]
    companies_raw = (request.form.get("companies") or "").strip()
    keywords_raw = (request.form.get("keywords") or "").strip()
    sep = lambda s: [t.strip() for line in s.splitlines() for t in line.split(",") if t.strip()]
    save(
        settings.data_dir,
        companies=sep(companies_raw),
        keywords=sep(keywords_raw),
    )
    flash(_("黑名单已保存。下次抓取时生效。"), "ok")
    return redirect(url_for("blacklist.overview"))


@bp.route("/from-description", methods=["POST"])
def from_description():
    """LLM converts natural-language preferences into structured suggestions.
    Returns JSON; UI shows them as checkboxes for user to opt in to."""
    settings = current_app.config["SETTINGS"]
    if not settings.has_llm:
        return jsonify({"ok": False, "error": "LLM not configured"}), 400

    payload = request.get_json(silent=True) or {}
    description = (payload.get("description") or "").strip()
    if not description:
        return jsonify({"ok": False, "error": "missing description"}), 400

    suggestions = from_natural_language(
        description,
        provider=settings.llm_provider,
        api_key=settings.llm_api_key,
        model=settings.llm_model,
    )
    return jsonify({"ok": True, **suggestions})
