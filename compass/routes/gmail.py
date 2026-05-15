"""Gmail integration routes.

GET  /gmail              status + setup instructions
POST /gmail/open-folder  open the credentials/ folder in OS file explorer
POST /gmail/sync         run a sync now (requires LLM + Gmail OAuth)
"""
from __future__ import annotations

import logging
import platform
import subprocess

from flask import Blueprint, current_app, flash, jsonify, redirect, render_template, url_for
from flask_babel import gettext as _

from ..extensions import session_scope
from ..services.gmail_sync import (
    gmail_client_secret_path,
    gmail_credentials_path,
    run_sync,
)

log = logging.getLogger(__name__)

bp = Blueprint("gmail", __name__, url_prefix="/gmail")


@bp.route("/")
def overview():
    settings = current_app.config["SETTINGS"]
    token = gmail_credentials_path(settings.data_dir)
    secret = gmail_client_secret_path(settings.data_dir)
    creds_dir = secret.parent
    # Ensure the directory exists so "Open in Finder" works on first visit
    try:
        creds_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return render_template(
        "gmail/overview.html",
        token_exists=token.exists(),
        secret_exists=secret.exists(),
        token_path=str(token),
        secret_path=str(secret),
        creds_dir=str(creds_dir),
        secret_filename=secret.name,
        has_llm=settings.has_llm,
    )


@bp.route("/open-folder", methods=["POST"])
def open_folder():
    """Open the credentials/ folder in OS file explorer.
    Local-only convenience — works on Mac (open), Win (explorer), Linux (xdg-open).
    """
    settings = current_app.config["SETTINGS"]
    creds_dir = gmail_client_secret_path(settings.data_dir).parent
    creds_dir.mkdir(parents=True, exist_ok=True)
    sysname = platform.system()
    try:
        if sysname == "Darwin":
            subprocess.Popen(["open", str(creds_dir)])
        elif sysname == "Windows":
            subprocess.Popen(["explorer", str(creds_dir)])
        else:
            subprocess.Popen(["xdg-open", str(creds_dir)])
        return jsonify({"ok": True, "path": str(creds_dir)})
    except Exception as exc:  # noqa: BLE001
        log.warning("open-folder failed: %s", exc)
        return jsonify({"ok": False, "error": str(exc), "path": str(creds_dir)}), 500


@bp.route("/sync", methods=["POST"])
def sync():
    settings = current_app.config["SETTINGS"]
    if not settings.has_llm:
        flash(_("没有配置 LLM_API_KEY — 无法分类邮件。"), "error")
        return redirect(url_for("gmail.overview"))

    try:
        with session_scope() as session:
            stats = run_sync(
                session,
                data_dir=settings.data_dir,
                provider=settings.llm_provider,
                api_key=settings.llm_api_key,
                model=settings.llm_model,
                days=14,
            )
    except RuntimeError as exc:
        flash(_("Gmail sync 失败: %(exc)s", exc=exc), "error")
        return redirect(url_for("gmail.overview"))
    except Exception as exc:  # noqa: BLE001
        log.exception("Gmail sync failed")
        flash(_("Gmail sync 出错: %(exc)s", exc=exc), "error")
        return redirect(url_for("gmail.overview"))

    flash(
        f"扫描 {stats['fetched']} 封邮件 · 识别 {stats['classified']} 封求职邮件 · "
        f"匹配 {stats['matched']} 个 job · 自动更新 {stats['status_updated']} 个状态",
        "ok",
    )
    return render_template("gmail/result.html", stats=stats)
