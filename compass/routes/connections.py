"""/connections — LinkedIn CSV import + browse known people."""
from __future__ import annotations

import logging

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from flask_babel import gettext as _
from werkzeug.utils import secure_filename

from ..extensions import session_scope
from ..models import Connection, ReflectionEvent
from ..services.connections import (
    companies_with_connections,
    import_linkedin_csv,
    total_active_connections,
)

bp = Blueprint("connections", __name__, url_prefix="/connections")

log = logging.getLogger(__name__)


_ALLOWED_EXT = {".csv"}
_MAX_BYTES = 10 * 1024 * 1024  # 10 MB — way more than any real export


@bp.route("/")
def overview():
    with session_scope() as session:
        total = total_active_connections(session)
        top_companies = companies_with_connections(session, limit=20)
        # Recent imports — sample of the latest 30 connections by import_at
        from sqlalchemy import select, desc as _desc
        recent = session.execute(
            select(Connection)
            .where(Connection.active.is_(True))
            .order_by(_desc(Connection.imported_at))
            .limit(30)
        ).scalars().all()
        return render_template(
            "connections/overview.html",
            total=total,
            top_companies=top_companies,
            recent=recent,
        )


@bp.route("/import", methods=["POST"])
def upload_csv():
    f = request.files.get("csv_file")
    if not f or not f.filename:
        flash(_("没选文件。"), "error")
        return redirect(url_for("connections.overview"))

    safe = secure_filename(f.filename)
    ext = "." + safe.rsplit(".", 1)[-1].lower() if "." in safe else ""
    if ext not in _ALLOWED_EXT:
        flash(_("只接受 .csv 文件（LinkedIn 导出）。"), "error")
        return redirect(url_for("connections.overview"))

    raw = f.read(_MAX_BYTES + 1)
    if len(raw) > _MAX_BYTES:
        flash(_("文件过大（>10MB）— 这不像是 LinkedIn Connections 导出。"), "error")
        return redirect(url_for("connections.overview"))

    # LinkedIn export is UTF-8; tolerate BOM and edge encodings
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        flash(_("无法识别 CSV 编码。"), "error")
        return redirect(url_for("connections.overview"))

    with session_scope() as session:
        result = import_linkedin_csv(session, text)
        if result.error:
            flash(_("导入失败: %(result_error)s", result_error=result.error), "error")
            return redirect(url_for("connections.overview"))

        session.add(ReflectionEvent(
            kind="connections_imported",
            payload_json={
                "source": "linkedin_csv",
                "filename": safe,
                "parsed": result.parsed_rows,
                "inserted": result.inserted,
                "updated": result.skipped_duplicates,
            },
        ))

    flash(
        f"已导入 {result.inserted} 个新连接 + 更新 {result.skipped_duplicates} 个 "
        f"(跳过 {result.skipped_invalid} 行无效)。",
        "ok",
    )
    return redirect(url_for("connections.overview"))


@bp.route("/<conn_id>/deactivate", methods=["POST"])
def deactivate(conn_id: str):
    with session_scope() as session:
        c = session.get(Connection, conn_id)
        if not c:
            flash(_("找不到该联系人。"), "error")
            return redirect(url_for("connections.overview"))
        c.active = False
    flash(_("已停用。"), "ok")
    return redirect(url_for("connections.overview"))


@bp.route("/sync-markdown", methods=["POST"])
def sync_markdown():
    """Run a full PKM markdown sync of contacts / companies / jobs.

    No-op (with friendly message) if pkm_export_enabled is off.
    """
    from ..services.pkm_export import sync_all
    settings = current_app.config["SETTINGS"]
    with session_scope() as session:
        result = sync_all(session)
    if not result.get("enabled"):
        flash(
            "PKM markdown 导出已关闭。打开方法：在 .env 加 "
            "<code>COMPASS_PKM_EXPORT=1</code> 然后重启。",
            "error",
        )
        return redirect(url_for("connections.overview"))
    flash(
        f"已写入: {result['connections']} 个联系人 + "
        f"{result['companies']} 家公司 + "
        f"{result['jobs']} 个 job 的 markdown "
        f"(到 {settings.data_dir}/contacts|companies|jobs/)",
        "ok",
    )
    return redirect(url_for("connections.overview"))
