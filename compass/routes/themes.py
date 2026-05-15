"""Theme management routes.

GET  /templates/                   list / upload page
POST /templates/upload             upload a user theme (HTML file or pasted text)
POST /templates/<uid>/delete       remove a user theme
GET  /templates/<id>/preview       render theme with sample resume data (no real data)
GET  /templates/inspire            inspire UI page (4-route format-preserving)
POST /templates/inspire            run inspire on uploaded file
POST /templates/inspire/save       save the result as a user theme
"""
from __future__ import annotations

import logging

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from flask_babel import gettext as _

from ..services.themes import (
    BUILTIN_THEMES,
    delete_user_theme,
    list_all_themes,
    render_theme,
    save_user_theme,
)

log = logging.getLogger(__name__)

bp = Blueprint("themes", __name__, url_prefix="/templates")


# ────────────────────────────────────────────────────────────
# Sample resume — used for theme preview without touching real data
# ────────────────────────────────────────────────────────────

SAMPLE_RESUME = {
    "basics": {
        "name": "Alex Chen",
        "email": "alex.chen@example.com",
        "phone": "+39 333 1234 5678",
        "location": {"address": "Milan, Italy"},
        "summary": "Data analyst with 4 years building reporting pipelines and "
                   "shipping LLM-augmented internal tools. Looking for a fintech "
                   "or climate-tech role with Italian/English exposure.",
    },
    "work": [
        {
            "name": "Acme Analytics",
            "position": "Senior Data Analyst",
            "startDate": "2024-01",
            "endDate": "Present",
            "location": "Milan, Italy",
            "highlights": [
                "Built a self-serve dashboard cutting analyst time-on-ad-hoc by 40% — adopted by 12 stakeholders.",
                "Led migration of legacy SQL pipelines to dbt + Airflow with zero downtime over 3 weekends.",
                "Mentored 2 junior analysts; both promoted within their first year.",
            ],
        },
        {
            "name": "Data Lab Srl",
            "position": "Data Analyst",
            "startDate": "2021-08",
            "endDate": "2023-12",
            "location": "Bologna, Italy",
            "highlights": [
                "Designed A/B tests for the e-commerce platform; lifted conversion 3.2% (significant at p<0.01).",
                "Owned the company-wide weekly KPI report read by C-level.",
            ],
        },
    ],
    "education": [
        {
            "institution": "Politecnico di Milano",
            "studyType": "M.Sc.",
            "area": "Computer Engineering",
            "startDate": "2019",
            "endDate": "2021",
            "gpa": "3.8/4.0",
        },
        {
            "institution": "University of XYZ",
            "studyType": "B.Sc.",
            "area": "Mathematics",
            "startDate": "2015",
            "endDate": "2019",
        },
    ],
    "skills": [
        {"name": "Languages", "keywords": ["Python", "SQL", "TypeScript"]},
        {"name": "Frameworks", "keywords": ["dbt", "Airflow", "FastAPI", "React"]},
        {"name": "Tools", "keywords": ["Git", "Docker", "GCP BigQuery", "Tableau"]},
    ],
    "languages": [
        {"language": "English (C1)"},
        {"language": "Italian (B2)"},
        {"language": "Mandarin (Native)"},
    ],
    "projects": [
        {
            "name": "Compass — career reflection system",
            "description": "Self-built tool that learns user calibration boundaries from accept/reject decisions, prevents 'sycophancy drift'.",
            "keywords": ["Python", "Flask", "SQLAlchemy", "Claude API"],
            "url": "github.com/example/compass",
        },
    ],
    "certificates": [
        {"name": "Google Data Analytics Certificate", "issuer": "Coursera", "date": "2022"},
    ],
}


# ────────────────────────────────────────────────────────────
# Routes
# ────────────────────────────────────────────────────────────


@bp.route("/")
def overview():
    themes = list_all_themes()
    return render_template("themes/manage.html", themes=themes)


@bp.route("/upload", methods=["POST"])
def upload():
    name = (request.form.get("name") or "").strip()
    description = (request.form.get("description") or "").strip()
    pasted = (request.form.get("html") or "").strip()
    file = request.files.get("file")

    if not name:
        flash(_("请填模板名称。"), "error")
        return redirect(url_for("themes.overview"))

    html = ""
    if file and file.filename:
        raw = file.read()
        # Reject binary uploads (e.g. accidentally drag-and-dropped DOCX/PDF) —
        # this endpoint is HTML-only; the DOCX path is /templates/inspire.
        # ZIP magic = 'PK\x03\x04' covers DOCX, XLSX, ODT, etc.; %PDF marks PDFs.
        head = raw[:8]
        if head.startswith(b"PK\x03\x04") or head.startswith(b"%PDF"):
            flash(
                _("这是二进制文件（DOCX/PDF）。HTML 上传只接受 .html。"
                  "如果想从 DOCX 学样式，去 → 模板灵感 (/templates/inspire)。"),
                "error",
            )
            return redirect(url_for("themes.overview"))
        try:
            html = raw.decode("utf-8", errors="replace")
        except Exception as e:  # noqa: BLE001
            flash(_("读不出文件: %(err)s", err=e), "error")
            return redirect(url_for("themes.overview"))
    elif pasted:
        html = pasted

    if not html:
        flash(_("请上传 .html 文件或粘贴 HTML 内容。"), "error")
        return redirect(url_for("themes.overview"))

    if len(html) > 1_000_000:  # 1 MB cap
        flash(_("模板太大（>1 MB）。请精简后再上传。"), "error")
        return redirect(url_for("themes.overview"))

    # Pre-flight: try compiling as Jinja so the broken template doesn't crash
    # /preview later. Cheap defense for HTML containing `{{` ambiguous text
    # (e.g. accidental binary characters slipped past magic-byte check).
    try:
        from jinja2 import Environment
        Environment().parse(html)
    except Exception as e:  # noqa: BLE001
        flash(_("HTML 模板语法错误，无法保存: %(err)s", err=e), "error")
        return redirect(url_for("themes.overview"))

    try:
        theme = save_user_theme(name=name, description=description, html=html)
    except ValueError as e:
        flash(_("保存失败: %(err)s", err=e), "error")
        return redirect(url_for("themes.overview"))

    flash(_("已上传模板 '%(name)s' (id=%(id)s)。", name=theme.name, id=theme.id), "ok")
    return redirect(url_for("themes.overview"))


@bp.route("/<uid>/delete", methods=["POST"])
def delete(uid: str):
    if delete_user_theme(uid):
        flash(_("已删除模板 %(id)s。", id=uid), "ok")
    else:
        flash(_("未找到模板 %(id)s。", id=uid), "error")
    return redirect(url_for("themes.overview"))


@bp.route("/<theme_id>/preview")
def preview(theme_id: str):
    """Render a theme with SAMPLE data — no DB lookup, no PII leak.

    For built-in themes use plain id (e.g. /templates/classic/preview).
    For user themes use prefixed (e.g. /templates/user:my-theme/preview).
    """
    try:
        return render_theme(theme_id, SAMPLE_RESUME, job_id=None)
    except ValueError as exc:
        flash(_("模板渲染失败: %(err)s", err=exc), "error")
        return redirect(url_for("themes.overview"))
    except Exception as exc:  # noqa: BLE001
        # Jinja TemplateSyntaxError / generic render error — friendly fallback.
        # Without this, a corrupt user theme (e.g. broken {{ }} block) 500s the
        # whole preview page; better to surface the cause and let the user fix.
        from jinja2 import TemplateError
        if isinstance(exc, TemplateError):
            flash(_("模板语法错误（行 %(line)s）: %(err)s", line=getattr(exc, 'lineno', '?'), err=exc), "error")
        else:
            flash(_("模板渲染异常: %(err)s", err=exc), "error")
        return redirect(url_for("themes.overview"))


# ────────────────────────────────────────────────────────────
# Template inspire — borrow from any uploaded file
# ────────────────────────────────────────────────────────────

# Stash inspire results in-memory so the UI can preview/save without re-running
# (LLM calls are expensive). Keyed by short token. Cleared on server restart.
#
# Memory hygiene: each entry holds ~600KB-2MB (modified .docx + sample render).
# Without bound, 100 uploads-without-save would grow to ~200MB. Cap to 12 entries
# (FIFO oldest-evicted), and prune entries older than 30 min on every insert.
import time
from collections import OrderedDict
_inspire_cache: OrderedDict[str, dict] = OrderedDict()
_INSPIRE_CACHE_MAX = 12
_INSPIRE_CACHE_TTL_SECS = 30 * 60


def _inspire_cache_prune() -> None:
    """Drop entries older than TTL or beyond cap. Called on every insert."""
    now = time.time()
    expired = [tok for tok, e in _inspire_cache.items()
               if now - e.get("_inserted_at", 0) > _INSPIRE_CACHE_TTL_SECS]
    for tok in expired:
        _inspire_cache.pop(tok, None)
    while len(_inspire_cache) > _INSPIRE_CACHE_MAX:
        _inspire_cache.popitem(last=False)  # FIFO oldest


@bp.route("/inspire", methods=["GET"])
def inspire_page():
    """The inspire UI — explains 4 routes + dropzone."""
    from flask import current_app
    settings = current_app.config["SETTINGS"]
    return render_template(
        "themes/inspire.html",
        has_llm=settings.has_llm,
        provider=settings.llm_provider,
        model=settings.llm_model,
    )


@bp.route("/inspire", methods=["POST"])
def inspire_run():
    """Process the uploaded template file and produce a preview."""
    from flask import current_app, jsonify
    from ..services.template_inspire import inspire_from_file
    import secrets

    settings = current_app.config["SETTINGS"]
    if not settings.has_llm:
        return jsonify({"ok": False, "error": _("没有配置 LLM_API_KEY，借鉴功能不可用")}), 400

    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"ok": False, "error": _("请选择文件")}), 400

    file_bytes = file.read()
    if len(file_bytes) > 15_000_000:
        return jsonify({"ok": False, "error": _("文件过大（>15MB）— 请压缩")}), 400

    force_route = request.form.get("force_route") or None

    result = inspire_from_file(
        file_bytes, file.filename,
        provider=settings.llm_provider,
        api_key=settings.llm_api_key,
        model=settings.llm_model,
        force_route=force_route,
    )

    if result.error:
        return jsonify({"ok": False, "error": result.error, "route": result.route_used}), 200

    # Stash for preview/save (with memory bookkeeping — see _inspire_cache_prune)
    _inspire_cache_prune()
    token = secrets.token_urlsafe(16)
    _inspire_cache[token] = {
        "result": result,
        "filename": file.filename,
        "_inserted_at": time.time(),
    }

    # For DOCX route, also pre-render a sample-data .docx so the user can
    # download it and verify in real Word/Pages. iframe preview is mammoth-
    # converted (approximate); the sample .docx is the actual binary that
    # ships through the export path. ADR-0018 P0: preview样品.
    sample_url = None
    if result.theme_kind == "docx" and result.theme_bytes:
        try:
            from ..services.template_inspire.docx_route import _render_sample
            _inspire_cache[token]["sample_bytes"] = _render_sample(result.theme_bytes)
            sample_url = url_for("themes.inspire_sample", token=token)
        except Exception as exc:  # noqa: BLE001
            log.warning("sample render failed for token %s: %s", token, exc)

    return jsonify({
        "ok": True,
        "token": token,
        "route": result.route_used,
        "kind": result.theme_kind,
        "fidelity": result.fidelity_estimate,
        "warnings": result.warnings,
        "notes_count": len(result.notes),
        "preview_url": url_for("themes.inspire_preview", token=token),
        "download_url": url_for("themes.inspire_download", token=token),
        "sample_url": sample_url,
    })


@bp.route("/inspire/<token>/sample")
def inspire_sample(token: str):
    """Download a sample-data-rendered .docx so the user can verify the
    inspired template renders correctly in Word/Pages before saving it."""
    from flask import Response
    entry = _inspire_cache.get(token)
    if not entry or "sample_bytes" not in entry:
        return "Sample expired or unavailable — re-upload to retry", 404
    fname = entry.get("filename") or "sample"
    base = fname.rsplit(".", 1)[0] if "." in fname else fname
    return Response(
        entry["sample_bytes"],
        mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition":
                 f'attachment; filename="{base}_sample_render.docx"'},
    )


@bp.route("/inspire/<token>/preview")
def inspire_preview(token: str):
    """Show the preview HTML in an iframe."""
    entry = _inspire_cache.get(token)
    if not entry:
        return "Preview expired — re-upload to retry", 404
    return entry["result"].preview_html


@bp.route("/inspire/<token>/download")
def inspire_download(token: str):
    """Download the inspired template (raw .docx or .html)."""
    from flask import Response
    entry = _inspire_cache.get(token)
    if not entry:
        return "Expired", 404
    result = entry["result"]
    fname = entry["filename"]
    base = fname.rsplit(".", 1)[0] if "." in fname else fname
    out_name = f"{base}_compass_template.{'docx' if result.theme_kind == 'docx' else 'html'}"
    mime = ("application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            if result.theme_kind == "docx" else "text/html; charset=utf-8")
    return Response(result.theme_bytes, mimetype=mime,
                    headers={"Content-Disposition": f'attachment; filename="{out_name}"'})


@bp.route("/inspire/<token>/save", methods=["POST"])
def inspire_save(token: str):
    """Save the inspired template as a user theme."""
    from ..services.themes import save_user_theme
    entry = _inspire_cache.get(token)
    if not entry:
        flash(_("Inspire 结果已过期 — 请重新上传"), "error")
        return redirect(url_for("themes.inspire_page"))

    result = entry["result"]
    name = (request.form.get("name") or "").strip()
    description = (request.form.get("description") or "").strip()
    if not name:
        flash(_("请填模板名称"), "error")
        return redirect(url_for("themes.inspire_page"))

    # ADR-0018 alignment layer: both HTML and DOCX themes route through the
    # same save_user_theme entry. DOCX bytes go to data/templates/docx/<id>.docx
    # and are rendered by themes.render_docx_theme via docxtpl at export time.
    desc = description or (
        f"从「{entry['filename']}」借鉴 ({result.route_used} 路, "
        f"保真 {int(result.fidelity_estimate*100)}%)"
    )
    try:
        if result.theme_kind == "docx":
            theme = save_user_theme(
                name=name, description=desc,
                data=result.theme_bytes, kind="docx",
            )
        else:
            # html / fragment — keep legacy str path
            theme = save_user_theme(
                name=name, description=desc,
                html=result.theme_bytes.decode("utf-8"),
            )
    except ValueError as e:
        flash(_("保存失败: %(err)s", err=e), "error")
        return redirect(url_for("themes.inspire_page"))

    # Clean up cache after save
    _inspire_cache.pop(token, None)

    flash(_("✓ 已保存为模板「%(name)s」(id=%(id)s)", name=theme.name, id=theme.id), "ok")
    return redirect(url_for("themes.overview"))
