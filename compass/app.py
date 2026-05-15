# Copyright (c) 2026 Jiawen <Jiawen.Cc@outlook.com>
# Licensed under the Elastic License 2.0 — see LICENSE in the project root.
# You may not provide this as a hosted/managed service to third parties.
# Commercial use (including SaaS): Jiawen.Cc@outlook.com
"""Flask app factory.

Phase 0 wiring:
    /                       redirects to /reflect/dashboard
    /reflect/dashboard      drift dashboard
    /reflect/assumptions    tool-assumptions report
    /healthz                liveness probe

Future phases plug into create_app(): tier matcher routes, interview routes,
fact/variant CRUD, etc.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import Flask, jsonify, redirect, render_template, request, url_for
from sqlalchemy import text
from markupsafe import escape

from .config import Settings
from .extensions import Base, get_engine, init_engine
# CRITICAL: import models package to trigger registration of all model classes
# with Base.metadata BEFORE create_all() runs. Without this line, metadata
# is empty at create_all-time and no tables are created.
from . import models  # noqa: F401

log = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> Flask:
    settings = settings or Settings.from_env()

    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    init_engine(settings)
    Base.metadata.create_all(get_engine())  # idempotent — creates missing tables
    log.info("Database ready at %s", settings.db_url)

    # Auto-run alembic upgrade head — fixes "no such column" errors after
    # cherry-picking schema migrations into a database created at an older
    # version (5-07: R rollback brought v3 sqlite back but code expects v4
    # columns like jobs.matched_profile_id / FactVariant.decomposition_json).
    # Failures are logged but don't block startup — `create_all` already gave
    # us a usable schema for fresh DBs.
    try:
        # Quiet alembic's own loggers — only OUR friendly "Schema migrate"
        # message + warnings should reach the user. (alembic.ini sets WARN
        # for the loggers it knows, but `runtime.plugins/migration` need this.)
        for _alembic_logger in (
            "alembic.runtime.plugins",
            "alembic.runtime.migration",
            "alembic.autogenerate",
        ):
            logging.getLogger(_alembic_logger).setLevel(logging.WARNING)

        from alembic.config import Config
        from alembic import command
        from alembic.script import ScriptDirectory
        from alembic.runtime.migration import MigrationContext

        alembic_ini = Path(__file__).parent.parent / "alembic.ini"
        if alembic_ini.exists():
            cfg = Config(str(alembic_ini))
            cfg.set_main_option("sqlalchemy.url", settings.db_url)

            # Diff: where are we vs where do we need to be?
            with get_engine().connect() as conn:
                ctx = MigrationContext.configure(conn)
                current = ctx.get_current_revision()
            head = ScriptDirectory.from_config(cfg).get_current_head()

            if current != head:
                # Suppress alembic's INFO chatter (handler set in alembic.ini)
                # but keep our own friendly message
                log.info(
                    "Schema migrate: %s → %s (auto-upgrading)",
                    current[:8] if current else "fresh",
                    head[:8] if head else "?",
                )
                command.upgrade(cfg, "head")
                log.info("Schema migrate: ✓ done")
            # else: silent — schema already at head
    except Exception as exc:  # noqa: BLE001
        log.warning("alembic upgrade skipped: %s — fresh tables OK, but old "
                    "DB may have schema drift. If you see 'no such column' "
                    "errors, run: .venv/bin/python -m alembic upgrade head",
                    exc)

    template_folder = Path(__file__).parent / "templates"
    static_folder = Path(__file__).parent / "static"
    app = Flask(__name__, template_folder=str(template_folder), static_folder=str(static_folder))
    app.secret_key = settings.secret_key
    app.config["SETTINGS"] = settings
    # Cap upload size at 10MB — guards against malicious / accidental huge uploads
    app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024

    # ── i18n setup ─────────────────────────────────────────────────────────
    import sys as _sys
    _intl_root = Path(__file__).parent.parent
    if str(_intl_root) not in _sys.path:
        _sys.path.insert(0, str(_intl_root))
    from flask import g, make_response, redirect, request
    from flask_babel import Babel
    from i18n_config import (
        COOKIE_MAX_AGE, COOKIE_NAME, DEFAULT_LOCALE,
        LANGUAGE_FLAGS, LANGUAGE_NAMES, SUPPORTED_LOCALES, select_locale,
    )

    app.config["BABEL_DEFAULT_LOCALE"] = DEFAULT_LOCALE
    app.config["BABEL_TRANSLATION_DIRECTORIES"] = str(_intl_root / "translations")
    _babel = Babel(app, locale_selector=select_locale)

    @app.before_request
    def _set_g_locale():
        g.locale = select_locale()

    @app.context_processor
    def _inject_i18n_globals():
        return {
            "current_locale": getattr(g, "locale", DEFAULT_LOCALE),
            "supported_locales": SUPPORTED_LOCALES,
            "language_names": LANGUAGE_NAMES,
            "language_flags": LANGUAGE_FLAGS,
        }

    @app.route("/set-language/<lang>")
    def set_language(lang: str):
        if lang not in SUPPORTED_LOCALES:
            lang = DEFAULT_LOCALE
        next_url = request.args.get("next") or request.referrer or "/"
        resp = make_response(redirect(next_url))
        resp.set_cookie(COOKIE_NAME, lang, max_age=COOKIE_MAX_AGE, samesite="Lax")
        return resp

    # ── First-run web wizard (P-007 后续：取代 .env 文本编辑) ────────────────
    # template 用户解压后没填 LLM_API_KEY, before_request 重定向到 /setup,
    # 表单 POST 后原子写 .env, 刷新 Settings, 直接进 dashboard.
    import os as _os
    _PLACEHOLDERS = {"", "your-api-key-here", "your_key_here",
                     "change-me", "sk-fake-for-audit"}

    def _key_is_set() -> bool:
        v = _os.environ.get("LLM_API_KEY", "").strip()
        return bool(v) and v not in _PLACEHOLDERS

    def _setup_needed() -> bool:
        return not _key_is_set()

    def _update_env_file(env_path, updates: dict) -> None:
        """原子更新 .env, 保留注释和其它行 (P-007 atomic write pattern)."""
        env_path = Path(env_path)
        if env_path.exists():
            text = env_path.read_text(encoding="utf-8")
        elif (env_path.parent / ".env.example").exists():
            text = (env_path.parent / ".env.example").read_text(encoding="utf-8")
        else:
            text = ""
        lines = text.splitlines()
        seen = set()
        out = []
        for line in lines:
            replaced = False
            for k, v in updates.items():
                if line.startswith(f"{k}="):
                    out.append(f"{k}={v}")
                    seen.add(k); replaced = True; break
            if not replaced:
                out.append(line)
        for k, v in updates.items():
            if k not in seen:
                out.append(f"{k}={v}")
        tmp = env_path.with_suffix(env_path.suffix + ".tmp")
        tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
        _os.replace(tmp, env_path)

    @app.before_request
    def _require_setup_complete():
        p = request.path
        if p.startswith("/static/") or p == "/setup" or p.startswith("/setup/"):
            return None
        if not _setup_needed():
            return None
        if p.startswith("/api/") or p == "/healthz":
            return jsonify({"error": "setup_required",
                            "redirect": "/setup"}), 503
        return redirect("/setup")

    @app.route("/setup", methods=["GET"])
    def _setup_page():
        return render_template("setup.html",
                               has_key=_key_is_set(),
                               current_provider=_os.environ.get("LLM_PROVIDER", "claude"))

    @app.route("/setup", methods=["POST"])
    def _setup_save():
        provider = request.form.get("llm_provider", "claude").strip()
        api_key = request.form.get("llm_api_key", "").strip()
        if provider not in ("claude", "openai"):
            return render_template("setup.html",
                                   error="LLM provider 必须是 claude 或 openai",
                                   has_key=False,
                                   current_provider=provider), 400
        if not api_key:
            return render_template("setup.html",
                                   error="API Key 不能为空",
                                   has_key=False,
                                   current_provider=provider), 400
        try:
            env_path = Path(__file__).parent.parent / ".env"
            _update_env_file(env_path, {
                "LLM_PROVIDER": provider,
                "LLM_API_KEY": api_key,
            })
            # 让当前进程立刻拿到新 env: dotenv override + 重建 Settings
            try:
                from dotenv import load_dotenv
                load_dotenv(env_path, override=True)
            except ImportError:
                pass
            app.config["SETTINGS"] = Settings.from_env()
        except Exception as e:
            return render_template("setup.html",
                                   error=f"保存失败: {e}",
                                   has_key=bool(api_key),
                                   current_provider=provider), 500
        return render_template("setup_saved.html")

    # Resolve user timezone once; fall back to UTC if invalid name in .env
    try:
        user_tz = ZoneInfo(settings.timezone)
    except ZoneInfoNotFoundError:
        log.warning("Unknown timezone %r in COMPASS_TZ — falling back to UTC", settings.timezone)
        user_tz = ZoneInfo("UTC")

    @app.template_filter("local_dt")
    def _local_dt(value, fmt: str = "%Y-%m-%d %H:%M") -> str:
        """Convert UTC-stored datetime to user-configured local TZ for display.
        Naive datetimes are assumed UTC (which is how we store them)."""
        if value is None:
            return "—"
        if not isinstance(value, datetime):
            return str(value)
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(user_tz).strftime(fmt)

    @app.template_filter("diff_html")
    def _diff_html(new_text: str, original_text: str):
        """Word-level diff between two strings → safe HTML with <del>/<ins> tags."""
        from .services.resume_diff import diff_html
        return diff_html(original_text or "", new_text or "")

    @app.template_global("level_meta")
    def _level_meta(level):
        from .services.resume_diff import level_meta
        return level_meta(level)

    @app.template_global("now_utc")
    def _now_utc():
        from datetime import datetime, timezone
        return datetime.now(timezone.utc)

    # Inject 'recommend_claude' into all templates — shown when user picked
    # a non-Claude provider (the skill ecosystem path requires Claude Code).
    @app.context_processor
    def _inject_provider_notice() -> dict:
        return {
            "recommend_claude": settings.llm_provider != "claude",
            "current_provider": settings.llm_provider,
        }

    # Inject global active alerts (value-regression checkpoint, drift, divergence...)
    # Computed cheaply from existing tables. Empty list on any error so banners never
    # break the page — alert system itself must not be a failure mode.
    @app.context_processor
    def _inject_active_alerts() -> dict:
        from .extensions import session_scope
        from .services.reflection_engine import compute_active_alerts
        try:
            with session_scope() as session:
                alerts = compute_active_alerts(session)
                return {"active_alerts": [
                    {
                        "severity": a.severity,
                        "kind": a.kind,
                        "title": a.title,
                        "body": a.body,
                        "cta_label": a.cta_label,
                        "cta_url": a.cta_url,
                    }
                    for a in alerts
                ]}
        except Exception:
            log.exception("active_alerts computation failed — suppressing for this request")
            return {"active_alerts": []}

    from .routes import (
        blacklist, calibration, connections, contacts, exports, feedback,
        funnel, gmail, identity, jobs, reflection, resume, themes,
    )

    app.register_blueprint(reflection.bp)
    app.register_blueprint(jobs.bp)
    app.register_blueprint(funnel.bp)
    app.register_blueprint(identity.bp)
    app.register_blueprint(resume.bp)
    app.register_blueprint(calibration.bp)
    app.register_blueprint(exports.bp)
    app.register_blueprint(gmail.bp)
    app.register_blueprint(blacklist.bp)
    app.register_blueprint(feedback.bp)
    app.register_blueprint(connections.bp)
    app.register_blueprint(contacts.bp)
    app.register_blueprint(themes.bp)
    from .routes import negotiation, rejection
    app.register_blueprint(negotiation.bp)
    app.register_blueprint(rejection.bp)

    @app.route("/")
    def index():
        """C3 (5-08): 主页 dashboard 摘要卡 — 高频信息一屏看完。
        漂移仪表盘移到 /reflect/dashboard（导航主面板第一项），主页给"求职动作"。
        """
        from .extensions import session_scope
        from .services.scrape_messages import last_scrape_diagnostic
        from .services.job_funnel import compute as compute_funnel
        from sqlalchemy import select, func
        from .models import Job

        with session_scope() as session:
            scrape_diag = last_scrape_diagnostic(session)
            funnel = compute_funnel(session)
            counts_by_status = dict(
                session.execute(
                    select(Job.status, func.count(Job.id))
                    .where(Job.deleted.is_(False))
                    .group_by(Job.status)
                ).all()
            )
            # Y4 (5-08): LLM silent-failure indicator. Counts the
            # "llm_silent_failure" ReflectionEvents from the last 7 days; also
            # counts the inline pipeline_warnings stuffed into recent
            # rewrite_attempt_completed events. Either signal means the user
            # has been getting degraded LLM output without realizing.
            from datetime import datetime, timedelta, timezone as _tz
            from .models import ReflectionEvent as _RE
            cutoff = datetime.now(_tz.utc) - timedelta(days=7)
            silent_count = session.execute(
                select(func.count(_RE.id)).where(
                    _RE.kind == "llm_silent_failure",
                    _RE.created_at >= cutoff,
                )
            ).scalar() or 0
            # Plus any pipeline_warnings on rewrite_attempt_completed
            recent_attempts = session.execute(
                select(_RE.payload_json).where(
                    _RE.kind == "rewrite_attempt_completed",
                    _RE.created_at >= cutoff,
                )
            ).scalars().all()
            for p in recent_attempts:
                if isinstance(p, dict):
                    silent_count += len(p.get("pipeline_warnings") or [])

        return render_template(
            "dashboard.html",
            scrape_diag=scrape_diag,
            funnel=funnel,
            pending_review=counts_by_status.get("new", 0),
            pending_apply=counts_by_status.get("reviewed", 0),
            pending_interview=counts_by_status.get("interview", 0),
            llm_silent_failures_7d=silent_count,
        )

    @app.route("/healthz")
    def healthz():
        """Health check — `?detail=1` for full report (DB / schema / config / LLM)."""
        if not request.args.get("detail"):
            return {"status": "ok", "version": _version()}

        report = {"status": "ok", "version": _version(), "checks": {}}
        # DB connectivity
        try:
            with get_engine().connect() as conn:
                conn.execute(text("SELECT 1"))
            report["checks"]["db"] = "ok"
        except Exception as exc:
            report["status"] = "degraded"
            report["checks"]["db"] = f"fail: {exc}"
        # Schema at head
        try:
            from alembic.config import Config as _AC
            from alembic.script import ScriptDirectory as _ASD
            from alembic.runtime.migration import MigrationContext as _AMC
            alembic_ini = Path(__file__).parent.parent / "alembic.ini"
            if alembic_ini.exists():
                cfg = _AC(str(alembic_ini))
                cfg.set_main_option("sqlalchemy.url", settings.db_url)
                with get_engine().connect() as conn:
                    current = _AMC.configure(conn).get_current_revision()
                head = _ASD.from_config(cfg).get_current_head()
                if current == head:
                    report["checks"]["schema"] = f"ok ({current[:8] if current else 'fresh'})"
                else:
                    report["status"] = "degraded"
                    report["checks"]["schema"] = (
                        f"drift: {current[:8] if current else 'fresh'} != head {head[:8]}"
                    )
        except Exception as exc:
            report["checks"]["schema"] = f"unknown: {exc}"
        # LLM key configured
        try:
            from .services.llm_settings import load as _llm_load
            _ll = _llm_load(settings.data_dir)
            if _ll and (_ll.api_key or settings.llm_api_key):
                report["checks"]["llm_key"] = f"ok ({_ll.provider}/{_ll.model})"
            elif settings.llm_api_key:
                report["checks"]["llm_key"] = "ok (.env fallback)"
            else:
                report["status"] = "degraded"
                report["checks"]["llm_key"] = "missing — go to /jobs/llm-settings/"
        except Exception as exc:
            report["checks"]["llm_key"] = f"unknown: {exc}"
        # Scrape config
        try:
            from .services.scrape_config import load as _sc_load
            cfg = _sc_load(settings.data_dir)
            n = len(cfg.profiles)
            n_enabled = sum(1 for p in cfg.profiles if p.enabled)
            report["checks"]["scrape_config"] = f"ok ({n} profiles, {n_enabled} enabled)"
        except Exception as exc:
            report["checks"]["scrape_config"] = f"fail: {exc}"
        # Vendor static files (5-10): regression-detect any swap back to CDN.
        try:
            import hashlib as _hash
            vendor_dir = Path(__file__).parent / "static" / "js" / "vendor"
            vendor_info = {}
            for name in ("alpine.min.js", "htmx.min.js"):
                p = vendor_dir / name
                if p.exists():
                    data = p.read_bytes()
                    vendor_info[name] = {
                        "size": len(data),
                        "sha256_8": _hash.sha256(data).hexdigest()[:8],
                    }
                else:
                    vendor_info[name] = "missing"
                    report["status"] = "degraded"
            report["checks"]["vendor_files"] = vendor_info
        except Exception as exc:
            report["checks"]["vendor_files"] = f"unknown: {exc}"
        # Scrape lock + progress files (5-10): visible state of the scrape pipeline.
        try:
            import time as _time
            lock = Path(settings.data_dir) / ".scrape_in_flight"
            prog = Path(settings.data_dir) / ".scrape_progress.json"
            lock_state = {"present": lock.exists()}
            if lock.exists():
                lock_state["age_sec"] = round(_time.time() - lock.stat().st_mtime, 1)
                if lock_state["age_sec"] > 600:
                    lock_state["stale"] = True
                    report["status"] = "degraded"
            report["checks"]["scrape_lock"] = lock_state
            if prog.exists():
                import json as _json
                try:
                    pdata = _json.loads(prog.read_text(encoding="utf-8"))
                    report["checks"]["scrape_progress"] = {
                        "active": pdata.get("active"),
                        "phase": pdata.get("phase"),
                        "updated_age_sec": (
                            round(_time.time() - pdata.get("updated_at", 0), 1)
                            if pdata.get("updated_at") else None
                        ),
                    }
                except Exception as exc:
                    report["checks"]["scrape_progress"] = f"unreadable: {exc}"
        except Exception as exc:
            report["checks"]["scrape_lock"] = f"unknown: {exc}"
        # Last scrape activity (5-10): pulled from ReflectionEvent stream
        try:
            from sqlalchemy import select, desc as _desc
            from .models import ReflectionEvent
            with get_engine().connect() as conn:
                row = conn.execute(
                    select(ReflectionEvent.created_at)
                    .where(ReflectionEvent.kind == "external_scrape")
                    .order_by(_desc(ReflectionEvent.created_at))
                    .limit(1)
                ).first()
                report["checks"]["last_scrape_at"] = (
                    str(row[0]) if row else "never"
                )
        except Exception as exc:
            report["checks"]["last_scrape_at"] = f"unknown: {exc}"
        return report

    # Friendly error pages — render via templates with inline HTML fallback
    # (in case template system itself is broken, e.g. SyntaxError in base.html)
    @app.errorhandler(404)
    def _not_found(_e):
        try:
            return render_template("errors/404.html"), 404
        except Exception:
            return (
                "<div style='font-family:sans-serif;max-width:600px;margin:80px auto;padding:24px'>"
                "<h1>404</h1><p>页面不存在。</p><p><a href='/'>← 返回首页</a></p></div>",
                404,
            )

    @app.errorhandler(500)
    def _server_error(e):
        import traceback as _tb
        log.exception("500 error: %s", e)

        real = getattr(e, "original_exception", None) or e
        kind = type(real).__name__
        msg = str(real)
        tb_text = "".join(_tb.format_exception(type(real), real, real.__traceback__))

        # Heuristic hints — give user actionable suggestion before they read tb
        hint = None
        if "no such column" in msg or "no such table" in msg:
            hint = (
                "Schema 不匹配。如果你刚从 zip 解压或换了机器，schema 应该自动 "
                "升级——但失败了。终端里手动跑：<br>"
                "<code style='display:block;margin:6px 0;padding:6px 8px;"
                "background:white;border-radius:3px'>"
                ".venv/bin/python -m alembic upgrade head</code>"
            )
        elif "OperationalError" in kind and "locked" in msg:
            hint = "数据库被另一个进程锁住——是不是同时跑了两个 Compass 实例？"
        elif "TemplateNotFound" in kind:
            hint = "模板文件缺失。zip 解压可能不完整，重新解压。"
        elif "ConnectionError" in kind or "Timeout" in kind:
            hint = "外部 API 超时。检查网络 + 你的 LLM API key 有效。"

        try:
            err_log = settings.data_dir / "error.log"
            err_log.parent.mkdir(parents=True, exist_ok=True)
            with err_log.open("a", encoding="utf-8") as f:
                from datetime import datetime as _dt
                f.write(f"\n\n=== {_dt.utcnow().isoformat()} 500 error ===\n{tb_text}\n")
        except Exception:
            pass

        try:
            return render_template(
                "errors/500.html",
                error_class=kind,
                error_msg=msg,
                traceback=tb_text,
                hint=hint,
            ), 500
        except Exception:
            # Fallback: inline HTML if template system itself is broken
            from markupsafe import escape as _esc
            return (
                "<div style='font-family:sans-serif;max-width:900px;margin:40px auto;padding:24px'>"
                f"<h1>出错了 (500)</h1>"
                f"<p>Compass 遇到内部错误。完整 traceback 见下方 + <code>data/error.log</code>。</p>"
                f"<pre style='background:#fae5e5;color:#a02020;padding:12px'>{_esc(kind)}: {_esc(msg)}</pre>"
                f"<details open><pre style='background:#f4f4f0;padding:12px;font-size:11px'>{_esc(tb_text)}</pre></details>"
                f"<p><a href='/'>← 返回首页</a></p>"
                "</div>",
                500,
            )

    return app


def _version() -> str:
    from . import __version__

    return __version__
