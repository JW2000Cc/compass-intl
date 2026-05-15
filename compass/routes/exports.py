"""Export routes — multi-format resume rendering (ADR-0018 alignment layers).

GET /export/json/<job_id>           Download as JSON Resume
GET /export/json                    Download master resume as JSON Resume
GET /export/preview/<job_id>        Render HTML preview (browser → Cmd+P → PDF)
GET /export/preview                 Render master resume HTML preview
GET /export/docx/<job_id>           Stream tailored resume as .docx (DOCX user theme)
GET /export/docx                    Stream master resume as .docx
GET /export/markdown[/<job_id>]     Plain Markdown (LinkedIn / Notion / Bear)
GET /export/txt[/<job_id>]          Plain text (ATS-safe)
GET /export/pdf/<job_id>            DOCX→LibreOffice→PDF (requires LibreOffice)
    Query param ?theme=<id>         Choose theme (built-in/user:<id>)
"""
from __future__ import annotations

import json
import logging

from flask import Blueprint, Response, current_app, flash, redirect, request, url_for
from flask_babel import gettext as _

from ..extensions import session_scope
from ..services.json_resume_exporter import build_json_resume
from ..services.themes import (
    list_all_themes, render_docx_theme, render_theme, _find_user_theme,
)

log = logging.getLogger(__name__)

bp = Blueprint("exports", __name__, url_prefix="/export")


def _version_suffix(*, fmt: str, theme_id: str | None, job_id: str | None) -> str:
    """Look up how many times the user already exported this (job, theme, fmt)
    combination. Returns "" for first export, "_v2" / "_v3" / ... for repeats.

    Anti-overwrite measure: simply changing the filename means the new file
    coexists with the old in Downloads/ — no destructive overwrite, no UI
    confirmation needed. ADR-0018 P3.
    """
    from ..models import ReflectionEvent
    from sqlalchemy import select, func
    try:
        with session_scope() as session:
            n = session.execute(
                select(func.count(ReflectionEvent.id)).where(
                    ReflectionEvent.kind == "export_done",
                    ReflectionEvent.payload_json["format"].as_string() == fmt,
                    ReflectionEvent.related_job_id == job_id,
                )
            ).scalar() or 0
        if n <= 0:
            return ""
        return f"_v{n + 1}"
    except Exception:  # noqa: BLE001
        return ""


def _suffix_for_lang() -> str:
    """When user picks ?lang=X (cross-language export), append `_X` to filename
    stem so the master file name and translated file name don't collide on disk.
    Empty string when no lang query (or it equals identity.language)."""
    lang = (request.args.get("lang") or "").strip()
    if not lang:
        return ""
    # Don't add suffix if lang == identity.language (it's a no-op)
    try:
        from ..models import IdentityVersion
        from sqlalchemy import select
        with session_scope() as session:
            idv = session.execute(
                select(IdentityVersion).where(IdentityVersion.is_current.is_(True))
            ).scalars().first()
            if idv and idv.language and idv.language.lower() == lang.lower():
                return ""
    except Exception:  # noqa: BLE001
        pass
    return f"_{_safe_filename(lang)}"


def _log_export(*, fmt: str, theme_id: str | None, job_id: str | None, filename: str, byte_count: int) -> None:
    """Write a ReflectionEvent when an export finishes successfully.

    Lets the user query "show me everything I've exported for this job" via the
    reflection stream, without us building a separate audit table.
    """
    from ..models import ReflectionEvent
    try:
        with session_scope() as session:
            session.add(ReflectionEvent(
                kind="export_done",
                payload_json={
                    "format": fmt,
                    "theme_id": theme_id,
                    "job_id": job_id,
                    "filename": filename,
                    "bytes": byte_count,
                },
                related_job_id=job_id,
            ))
    except Exception:  # noqa: BLE001
        log.exception("export_done event write failed (export still succeeded)")


def _safe_filename(s: str) -> str:
    """Strip path separators and oddballs so a filename is HTTP-safe.
    Allow CJK characters since they render fine in HTTP headers + on disk."""
    out = []
    for c in s:
        if c.isalnum() or c in "-_" or "一" <= c <= "鿿":
            out.append(c)
    return "".join(out) or "resume"


def _resolve_filename_stem(
    *,
    job_id: str | None = None,
    theme_name: str | None = None,
    template: str | None = None,
) -> str:
    """Resolve the export filename stem (no extension) from a `template` string.

    Default template (when caller passes None):
        "{name}_{company}_{role}"  (master resume → "{name}_master")

    Supported variables (any not bound resolves to "" or "master" for job_id):
        {name}     candidate name from current IdentityVersion.parsed_json.basics.name
        {company}  job.company
        {role}     job.title
        {job_id}   raw job id
        {date}     today YYYY-MM-DD
        {theme}    theme display name
        {lang}     identity.language fallback "en"

    Empty values are dropped (no double underscores) and the whole string is
    sanitized via `_safe_filename`.
    """
    from datetime import date as _date

    if template is None:
        template = current_app.config["SETTINGS"].export_filename_template

    # Resolve variables — pull DB context lazily so callers don't have to thread session
    name = ""
    company = ""
    role = ""
    lang = ""
    with session_scope() as session:
        from ..models import IdentityVersion, Job
        from sqlalchemy import select
        identity = session.execute(
            select(IdentityVersion).where(IdentityVersion.is_current.is_(True))
        ).scalars().first()
        if identity:
            lang = identity.language or ""
            try:
                basics = (identity.parsed_json or {}).get("basics") or {}
                name = (basics.get("name") or "").strip()
            except (AttributeError, TypeError):
                pass
        if job_id:
            job = session.get(Job, job_id)
            if job:
                company = (job.company or "").strip()
                role = (job.title or "").strip()

    bindings = {
        "name": name or "resume",
        "company": company,
        "role": role,
        "job_id": (job_id or ""),
        "date": _date.today().isoformat(),
        "theme": (theme_name or ""),
        "lang": lang,
    }

    # Render template, then collapse empty segments. Use a chain of {var} tokens
    # joined by separators; if any var resolves to "", the join layer drops it.
    import re as _re

    def _sub(m):
        return bindings.get(m.group(1), "")

    out = _re.sub(r"\{(\w+)\}", _sub, template)
    # Collapse runs of separator chars caused by empty bindings (e.g. "_" "_-")
    out = _re.sub(r"[_\-]{2,}", "_", out).strip("_-")

    if not out:
        out = "resume" + (f"_{job_id}" if job_id else "_master")

    return _safe_filename(out)


_EXT_BY_FMT = {
    "docx": "docx",
    "pdf": "pdf",
    "markdown": "md",
    "txt": "txt",
    "json": "json",
    "bundle": "zip",
}


def _build_export_filename(
    *,
    fmt: str,
    theme_name: str | None = None,
    theme_id: str | None = None,
    job_id: str | None = None,
) -> str:
    """One-stop filename builder. Combines stem + lang suffix + version suffix
    + extension. Eliminates the boilerplate that was repeated per endpoint.
    """
    stem = _resolve_filename_stem(theme_name=theme_name, job_id=job_id)
    return (
        stem
        + _suffix_for_lang()
        + _version_suffix(fmt=fmt, theme_id=theme_id, job_id=job_id)
        + "."
        + _EXT_BY_FMT.get(fmt, fmt)
    )


@bp.route("/json")
@bp.route("/json/<job_id>")
def json_export(job_id: str | None = None):
    with session_scope() as session:
        payload = build_json_resume(session, job_id=job_id)
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    fname = f"resume_{job_id or 'master'}.json"
    body = text.encode("utf-8")
    _log_export(fmt="json", theme_id=None, job_id=job_id, filename=fname, byte_count=len(body))
    return Response(
        body,
        mimetype="application/json",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@bp.route("/preview")
@bp.route("/preview/<job_id>")
def html_preview(job_id: str | None = None):
    """Render resume HTML using selected theme. User does Cmd+P → Save as PDF.

    Themes are pure presentation — same JSON Resume payload renders into any theme.
    Choose via ?theme=classic|modern|compact|user:<id>.
    DOCX user themes are NOT renderable here — they redirect to /export/docx.
    """
    theme_id = request.args.get("theme") or "classic"

    # Auto-redirect DOCX themes to the docx endpoint (single export button works
    # for both kinds — see ADR-0018 alignment layer dispatch).
    if theme_id.startswith("user:"):
        ut = _find_user_theme(theme_id[len("user:"):])
        if ut and ut.kind == "docx":
            url = url_for("exports.docx_export", job_id=job_id, theme=theme_id)
            return redirect(url)

    with session_scope() as session:
        resume = build_json_resume(session, job_id=job_id, target_language=(request.args.get("lang") or None))
    try:
        return render_theme(theme_id, resume, job_id=job_id)
    except ValueError as exc:
        log.warning("Theme %s render failed: %s — falling back to classic", theme_id, exc)
        return render_theme("classic", resume, job_id=job_id)


@bp.route("/docx")
@bp.route("/docx/<job_id>")
def docx_export(job_id: str | None = None):
    """ADR-0018 DOCX alignment layer endpoint.

    Streams a .docx where the user's uploaded DOCX template (kind="docx")
    is rendered with the JSON Resume payload via docxtpl. Format/style
    are preserved from the user's template; only text content is replaced.

    Theme must be a `user:<id>` kind="docx" theme — built-in HTML themes
    cannot render to .docx (use /preview + Cmd+P instead).
    """
    theme_id = request.args.get("theme") or ""
    if not theme_id.startswith("user:"):
        flash(_("DOCX 导出需要选用户上传的 DOCX 模板。先到 /templates/inspire 上传一份。"), "error")
        return redirect(url_for("themes.overview"))

    ut = _find_user_theme(theme_id[len("user:"):])
    if ut is None:
        flash(_("找不到模板: %(theme_id)s", theme_id=theme_id), "error")
        return redirect(url_for("themes.overview"))
    if ut.kind != "docx":
        # Allow falling back to HTML preview for HTML user themes
        return redirect(url_for("exports.html_preview", job_id=job_id, theme=theme_id))

    with session_scope() as session:
        resume = build_json_resume(session, job_id=job_id, target_language=(request.args.get("lang") or None))

    try:
        docx_bytes = render_docx_theme(ut, resume, job_id=job_id)
    except Exception as exc:  # noqa: BLE001
        log.exception("docx_export failed for theme=%s job_id=%s", theme_id, job_id)
        flash(_("DOCX 渲染失败: %(exc)s", exc=exc), "error")
        return redirect(url_for("themes.overview"))

    fname = _build_export_filename(fmt="docx", theme_name=ut.name, theme_id=theme_id, job_id=job_id)
    _log_export(fmt="docx", theme_id=theme_id, job_id=job_id, filename=fname, byte_count=len(docx_bytes))
    return Response(
        docx_bytes,
        mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@bp.route("/markdown")
@bp.route("/markdown/<job_id>")
def markdown_export(job_id: str | None = None):
    """Render JSON Resume → standard Markdown. Pure function, no LLM."""
    from ..services.exporters import render_markdown
    with session_scope() as session:
        resume = build_json_resume(session, job_id=job_id, target_language=(request.args.get("lang") or None))
    text = render_markdown(resume)
    fname = _build_export_filename(fmt="markdown", job_id=job_id)
    body = text.encode("utf-8")
    _log_export(fmt="markdown", theme_id=None, job_id=job_id, filename=fname, byte_count=len(body))
    # mimetype only — Flask appends charset itself; setting it here would dupe.
    return Response(
        body,
        mimetype="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@bp.route("/txt")
@bp.route("/txt/<job_id>")
def txt_export(job_id: str | None = None):
    """Render JSON Resume → plain text. ATS-safe."""
    from ..services.exporters import render_txt
    with session_scope() as session:
        resume = build_json_resume(session, job_id=job_id, target_language=(request.args.get("lang") or None))
    text = render_txt(resume)
    fname = _build_export_filename(fmt="txt", job_id=job_id)
    body = text.encode("utf-8")
    _log_export(fmt="txt", theme_id=None, job_id=job_id, filename=fname, byte_count=len(body))
    return Response(
        body,
        mimetype="text/plain",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@bp.route("/pdf")
@bp.route("/pdf/<job_id>")
def pdf_export(job_id: str | None = None):
    """Render DOCX → PDF via LibreOffice headless.

    Requires:
      · A user-uploaded DOCX theme (kind="docx") via ?theme=user:<id>
      · LibreOffice installed on this machine

    If LibreOffice is missing, returns a friendly error page with install hint
    (we don't silently install system-level software).
    """
    theme_id = request.args.get("theme") or ""
    if not theme_id.startswith("user:"):
        flash(_("PDF 直出需要选用户上传的 DOCX 模板。先到 /templates/inspire 上传一份。"), "error")
        return redirect(url_for("themes.overview"))

    ut = _find_user_theme(theme_id[len("user:"):])
    if ut is None or ut.kind != "docx":
        flash(_("PDF 直出只支持 DOCX 用户模板。HTML 模板请用 /preview + Cmd+P。"), "error")
        return redirect(url_for("themes.overview"))

    # Check LibreOffice early — better to bail with a helpful message than to
    # spend time rendering DOCX and then fail at the conversion step.
    from ..services.exporters import (
        LibreOfficeNotFoundError, docx_to_pdf, find_libreoffice,
    )
    if not find_libreoffice():
        flash(
            "PDF 直出需要 LibreOffice。"
            "推荐：双击 Compass 文件夹里的 「Install LibreOffice_Mac.command」"
            "（Win 版：「Install LibreOffice_Windows.bat」）一键安装。"
            "或者手动 brew install libreoffice / 下载 https://www.libreoffice.org/。"
            "也可以下载 .docx 用 Word/Pages 自己导 PDF（不依赖 LibreOffice）。",
            "error",
        )
        return redirect(url_for("exports.docx_export", job_id=job_id, theme=theme_id))

    with session_scope() as session:
        resume = build_json_resume(session, job_id=job_id, target_language=(request.args.get("lang") or None))

    try:
        docx_bytes = render_docx_theme(ut, resume, job_id=job_id)
        # Font fallback warning — heuristic check before PDF conversion.
        # Doesn't block, just adds a flash so user knows why the PDF might
        # look different from the .docx.
        from ..services.exporters import detect_dominant_fonts, fonts_likely_to_fall_back
        target_lang = (request.args.get("lang") or "").strip() or None
        fonts = detect_dominant_fonts(docx_bytes)
        risky = fonts_likely_to_fall_back(fonts, target_lang)
        if risky:
            flash(
                f"⚠ 字体提示：模板用了 {' / '.join(risky)}（CJK 字体），"
                f"导出 {target_lang} 的拉丁字符可能 fallback 到字体族西文变体。"
                f"如对视觉一致有要求，建议另存一份西式字体（Calibri / Garamond）的同结构 DOCX",
                "warn",
            )
        pdf_bytes = docx_to_pdf(docx_bytes)
    except LibreOfficeNotFoundError as exc:
        log.warning("LibreOffice missing during PDF export: %s", exc)
        flash(str(exc), "error")
        return redirect(url_for("exports.docx_export", job_id=job_id, theme=theme_id))
    except Exception as exc:  # noqa: BLE001
        log.exception("pdf_export failed for theme=%s job_id=%s", theme_id, job_id)
        flash(_("PDF 渲染失败: %(exc)s", exc=exc), "error")
        return redirect(url_for("themes.overview"))

    fname = _build_export_filename(fmt="pdf", theme_name=ut.name, theme_id=theme_id, job_id=job_id)
    _log_export(fmt="pdf", theme_id=theme_id, job_id=job_id, filename=fname, byte_count=len(pdf_bytes))
    return Response(
        pdf_bytes,
        mimetype="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@bp.route("/bundle")
@bp.route("/bundle/<job_id>")
def bundle_export(job_id: str | None = None):
    """One-click "everything" export: zip with .docx + .pdf (if LibreOffice
    available) + .md + .txt + .json. ADR-0018 P3 — convenience for users
    who batch out variants for many JDs.
    """
    import io
    import zipfile

    target_lang = (request.args.get("lang") or "").strip() or None
    theme_id = request.args.get("theme") or ""
    ut = None
    if theme_id.startswith("user:"):
        ut = _find_user_theme(theme_id[len("user:"):])
        if ut is not None and ut.kind != "docx":
            ut = None  # bundle only includes DOCX-rendered files when theme is docx

    with session_scope() as session:
        resume = build_json_resume(session, job_id=job_id, target_language=target_lang)

    from ..services.exporters import (
        LibreOfficeNotFoundError, docx_to_pdf, find_libreoffice,
        render_markdown, render_txt,
    )

    # Bundle stem reuses the same shared builder. The .zip extension is added
    # below at writing time; we strip it here to derive individual filenames.
    bundle_filename = _build_export_filename(
        fmt="bundle", theme_name=ut.name if ut else None,
        theme_id=theme_id or None, job_id=job_id,
    )
    stem = bundle_filename.rsplit(".", 1)[0]  # drop ".zip"

    buf = io.BytesIO()
    included: list[str] = []
    skipped: list[str] = []
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # Always include text formats — pure functions, never fail
        zf.writestr(f"{stem}.md", render_markdown(resume))
        included.append("md")
        zf.writestr(f"{stem}.txt", render_txt(resume))
        included.append("txt")
        zf.writestr(f"{stem}.json", json.dumps(resume, ensure_ascii=False, indent=2))
        included.append("json")

        # DOCX + PDF require a user DOCX template
        if ut is not None:
            try:
                docx_bytes = render_docx_theme(ut, resume, job_id=job_id)
                zf.writestr(f"{stem}.docx", docx_bytes)
                included.append("docx")
                if find_libreoffice():
                    try:
                        pdf_bytes = docx_to_pdf(docx_bytes)
                        zf.writestr(f"{stem}.pdf", pdf_bytes)
                        included.append("pdf")
                    except (LibreOfficeNotFoundError, Exception) as exc:  # noqa: BLE001
                        log.warning("bundle: pdf step failed: %s", exc)
                        skipped.append(f"pdf ({exc})")
                else:
                    skipped.append("pdf (LibreOffice not installed)")
            except Exception as exc:  # noqa: BLE001
                log.exception("bundle: docx step failed")
                skipped.append(f"docx ({exc})")
        else:
            skipped.append("docx/pdf (no DOCX user theme selected via ?theme=user:...)")

        # Manifest so the user knows what got included / skipped + why
        manifest = (
            f"Compass export bundle — {stem}\n"
            f"Included: {', '.join(included)}\n"
            f"Skipped:  {', '.join(skipped) or '(none)'}\n"
        )
        zf.writestr("MANIFEST.txt", manifest)

    body = buf.getvalue()
    _log_export(fmt="bundle", theme_id=theme_id or None, job_id=job_id,
                filename=bundle_filename, byte_count=len(body))
    return Response(
        body,
        mimetype="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{bundle_filename}"'},
    )
