"""Jobs routes — list / detail / status / thumbs / classify-now.

GET  /jobs                       list (filterable by tier/status)
GET  /jobs/<id>                  detail
POST /jobs/<id>/status           change status
POST /jobs/<id>/thumb            thumbs up/down (writes ReflectionEvent)
POST /jobs/<id>/classify         re-run tier classifier on a single job

ADR-0016 in-place: 老的 POST /jobs/scrape 已迁移到 jobs_config.py 的
/jobs/search-config/scrape_all（多 profile 一等公民）。老的 ?scrape=1
query 在 list_view() 里 redirect 兼容。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from flask import Blueprint, current_app, flash, jsonify, redirect, render_template, request, url_for
from flask_babel import gettext as _
from sqlalchemy import select

from ..extensions import session_scope
from ..models import Job, JobMatch, ReflectionEvent
from ..services.job_funnel import list_jobs_by_tier
from ..services.scraper import run_scrape
from ..services.tier_classifier import classify_job

log = logging.getLogger(__name__)

bp = Blueprint("jobs", __name__, url_prefix="/jobs")

TIER_LABELS = {
    1: "核心专业圈",
    2: "邻近迁移圈 ⚡反茧房",
    3: "可学习扩展",
    4: "远距迁移",
    5: "客观不可达",
}


# Kanban column order — left-to-right pipeline.
# Tier 5 ("不可达") and "passed" are intentionally NOT shown — they're noise on the board.
KANBAN_COLUMNS = [
    ("new", "未读"),
    ("reviewed", "已看"),
    ("applied", "已投"),
    ("interview", "面试中"),
    ("offer", "Offer"),
    ("rejected", "被拒"),
]


def _kanban_columns(rows):
    """Group (job, match) pairs into a list of (status, label, items) for Kanban.

    Each item is the (job, match) tuple. Tier-5 and 'passed' are dropped.
    """
    by_status: dict[str, list] = {s: [] for s, _ in KANBAN_COLUMNS}
    for job, match in rows:
        # Skip tier 5 (客观不可达) — they're stored for transparency but cluttery on the board
        if match and match.tier == 5:
            continue
        # Skip 'passed' — user actively chose not to apply, doesn't belong on a pipeline
        if job.status == "passed":
            continue
        if job.status in by_status:
            by_status[job.status].append((job, match))
    return [(status, label, by_status[status]) for status, label in KANBAN_COLUMNS]


@bp.route("/")
def list_view():
    # 5-07 user 原意：?scrape=1 让 jobs/list.html 顶部抓取面板 force-open
    # 模板里 details#scrape 用 request.args 判断（不再 redirect 外部 URL）
    tier_raw = request.args.get("tier", "").strip()
    tier = int(tier_raw) if tier_raw and tier_raw.lstrip("-").isdigit() else None
    status = request.args.get("status") or None
    if status == "":
        status = None
    limit = request.args.get("limit", default=40, type=int)
    view = request.args.get("view", "list")
    if view not in ("list", "kanban"):
        view = "list"
    sort = request.args.get("sort", "tier")
    if sort not in ("tier", "scraped_desc", "scraped_asc", "by_date"):
        sort = "tier"
    q_text = (request.args.get("q") or "").strip() or None
    location = (request.args.get("location") or "").strip() or None
    is_htmx = request.headers.get("HX-Request") == "true"

    with session_scope() as session:
        # Kanban view ignores tier filter (it groups by status across all tiers)
        # but respects the limit so we don't blow up on huge datasets
        if view == "kanban":
            kanban_limit = max(limit, 200)  # Kanban needs more rows to look right
            rows = list_jobs_by_tier(session, tier=None, limit=kanban_limit, status=None)
            kanban_columns = _kanban_columns(rows)
            return render_template(
                "jobs/kanban.html",
                kanban_columns=kanban_columns,
                tier_labels=TIER_LABELS,
                current_view="kanban",
                current_limit=kanban_limit,
            )

        # DB sort: scraped_desc / scraped_asc; "tier" + "by_date" default uses scraped_desc internally then groups.
        db_sort = sort if sort in ("scraped_desc", "scraped_asc") else "scraped_desc"
        rows = list_jobs_by_tier(
            session, tier=tier, limit=limit, status=status,
            sort=db_sort, q_text=q_text, location=location,
        )

        # Top locations for filter pills (computed across full DB, not filtered)
        from ..services.job_funnel import top_locations
        location_pills = top_locations(session, max_n=8)

        # C5: diagnostic for empty-state — show "why is this blank" instead
        # of the unhelpful "还没有岗位". Only computed when truly empty.
        from ..services.scrape_messages import last_scrape_diagnostic
        scrape_diag = None
        # We compute later after rows are known; pre-fetch here once is fine.
        scrape_diag = last_scrape_diagnostic(session)

        # Group rendering depends on sort mode
        groups: dict[int, list] = {1: [], 2: [], 3: [], 4: [], 5: [], 0: []}
        flat_rows: list = []
        date_groups: list = []  # list of {date_str, count, items: [(job, match)]}
        if sort == "tier":
            for job, match in rows:
                t = match.tier if match else 0
                groups[t].append((job, match))
        elif sort == "by_date":
            from collections import OrderedDict
            buckets: "OrderedDict[str, list]" = OrderedDict()
            for job, match in rows:
                if job.created_at:
                    key = job.created_at.strftime("%Y-%m-%d")
                else:
                    key = "未知"
                buckets.setdefault(key, []).append((job, match))
            # Use "rows" key name (not "items") to avoid Jinja dict.items() shadow
            date_groups = [{"date_str": d, "count": len(v), "rows": v} for d, v in buckets.items()]
        else:
            flat_rows = list(rows)

        # HTMX: return only the results region for in-place swap
        common_ctx = dict(
            groups=groups, flat_rows=flat_rows, date_groups=date_groups,
            tier_labels=TIER_LABELS,
            current_tier=tier,
            current_status=status,
            current_limit=limit,
            current_sort=sort,
            current_q=q_text or "",
            current_location=location or "",
            location_pills=location_pills,
            scrape_diag=scrape_diag,
        )
        if is_htmx:
            return render_template("jobs/_list_results.html", **common_ctx)

        # 5-07: server-side 初始 groups（last_scrape.json multi-profile → form-friendly）
        # Alpine 在 localStorage 空时用这个，让用户第一次打开就看到已有 profile
        initial_groups = []
        try:
            from ..services.scrape_config import load as _load_intent
            from flask import current_app as _ca
            intent = _load_intent(_ca.config["SETTINGS"].data_dir)
            for prof in intent.profiles:
                # 拼回 "City, Country" 字符串给 v3 form
                loc_str = ", ".join(x for x in (prof.city, prof.country) if x) or (prof.label or "")
                initial_groups.append({
                    "id": prof.id,  # 5-07 carry id through form roundtrip
                    "label": prof.label,
                    "enabled": prof.enabled,
                    "locations": loc_str,
                    "keywords": ", ".join(prof.keywords),
                    "hours_old": prof.hours_old or 168,
                    "city": prof.city,
                    "country": prof.country,
                    "user_prompt": prof.user_prompt,
                    "country_must_be": "\n".join(prof.dealbreakers.country_must_be),
                    "language_blocked": "\n".join(prof.dealbreakers.language_blocked),
                    "blacklist_companies": "\n".join(prof.blacklist.companies),
                    "blacklist_keywords": "\n".join(prof.blacklist.keywords),
                    "keyword_aliases": "\n".join(
                        f"{k}={','.join(v)}" for k, v in prof.keyword_aliases.items()
                    ),
                })
        except Exception:
            initial_groups = []

        return render_template(
            "jobs/list.html",
            current_view="list",
            initial_groups=initial_groups,
            **common_ctx,
        )


def _compute_thumbs_summary(session, job_id: str, last_action: str | None = None) -> dict:
    """Count historical thumbs for a job. Used by both detail and thumb routes."""
    rows = session.execute(
        select(ReflectionEvent.kind).where(ReflectionEvent.related_job_id == job_id)
    ).all()
    up = sum(1 for (k,) in rows if k == "job_thumbed_up")
    down = sum(1 for (k,) in rows if k == "job_thumbed_down")
    return {"up": up, "down": down, "last_action": last_action}


@bp.route("/<job_id>")
def detail(job_id: str):
    with session_scope() as session:
        job = session.get(Job, job_id)
        if not job:
            flash(_("Job not found."), "error")
            return redirect(url_for("jobs.list_view"))

        matches = (
            session.execute(
                select(JobMatch).where(JobMatch.job_id == job_id).order_by(JobMatch.matched_at.desc())
            )
            .scalars()
            .all()
        )
        thumbs_summary = _compute_thumbs_summary(session, job_id)

        # Log opening as a soft-interaction event
        session.add(
            ReflectionEvent(
                kind="job_opened",
                payload_json={"job_id": job_id},
                related_job_id=job_id,
            )
        )

        # Workflow stage — drives progressive disclosure of detail page sections.
        # See ADR-0015 (TBD) — the principle is "noise off, signal on, by status".
        #   evaluate  : new / reviewed     — JD review + warm contacts visible
        #   prepare   : reviewed (manual)  — + resume rewrite + cover letter
        #   sent      : applied            — + cold contacts + post-apply outreach
        #   active    : interview/offer    — + interview prep
        #   archive   : rejected/passed    — read-only retrospective
        STAGE_BY_STATUS = {
            "new":       "evaluate",
            "reviewed":  "prepare",
            "applied":   "sent",
            "interview": "active",
            "offer":     "active",
            "rejected":  "archive",
            "passed":    "archive",
        }
        workflow_stage = STAGE_BY_STATUS.get(job.status, "evaluate")

        # Detect JD language + grab user's preferred target language.
        # ADR-0016 single source: read user_languages from search_config.
        from ..services.translation import (
            detect_language_simple, needs_translation, LANGUAGE_NAMES,
            SUPPORTED_TARGETS, get_preferred_target_lang, get_translation_pref_summary,
        )
        from ..services.scrape_config import load as load_intent

        settings = current_app.config["SETTINGS"]
        # Pass the list as-is — empty list means "user hasn't declared", and
        # needs_translation treats that as "offer the button anyway". Don't
        # `or None` here: it collapses [] to None, but more importantly hides
        # a UX bug where the empty-list case used to fall back to a hardcoded
        # {zh, en} default and silently suppress the button on en/zh JDs.
        user_langs = load_intent(settings.data_dir).global_.user_languages
        offer, lang = needs_translation(job.description or "", user_languages=user_langs)
        prefs = get_translation_pref_summary()
        jd_lang_info = {
            "lang": lang,
            "name": LANGUAGE_NAMES.get(lang, lang),
            "offer_translation": offer,
            "preferred_target": get_preferred_target_lang(default="zh"),
            "supported_targets": [
                {"code": c, "name": LANGUAGE_NAMES.get(c, c),
                 "count": prefs.get("lang_counts", {}).get(c, 0)}
                for c in SUPPORTED_TARGETS
                if c != lang  # don't offer to translate into the source language
            ],
        }

        # Compute effort pack server-side so the stepper renders immediately
        # (no async flash). The effort pack drives the workflow stepper.
        from ..services.effort_pack import build_view as _build_effort
        effort = _build_effort(session, job)
        # Pick "active step" — first non-done step, or last step if all done.
        # outreach_sent has been merged into find_contact panel — never set it
        # as the active step (no panel exists for it now).
        active_step_id = next(
            (s.id for s in effort.steps if s.state == "todo" and s.id != "outreach_sent"),
            None,
        )
        if not active_step_id:
            visible = [s for s in effort.steps if s.id != "outreach_sent"]
            active_step_id = visible[-1].id if visible else "read_jd"
        # Hash redirect: if user lands on #step-outreach_sent, treat as find_contact
        # (this is handled client-side in the Alpine x-data init)

        # Latest rewrite attempt + variants for this job — embedded inline in step 2
        from ..models import RewriteAttempt, FactVariant
        latest_attempt = (
            session.execute(
                select(RewriteAttempt).where(RewriteAttempt.job_id == job_id)
                .order_by(RewriteAttempt.created_at.desc()).limit(1)
            ).scalar_one_or_none()
        )
        review_rows = []
        if latest_attempt:
            variants = (
                session.execute(
                    select(FactVariant).where(FactVariant.job_id == job_id)
                    .order_by(FactVariant.created_at.desc())
                ).scalars().all()
            )
            # Dedup latest-per-fact + attach ADR-0015 user-facing menu when
            # decomposition_json is set. See services/review_rows.py.
            # Locale follows resume identity so dimension labels render in the
            # user's resume language (zh fallback when no identity yet).
            from ..services.review_rows import build_review_rows
            from ..services.identity_engine import get_current_identity

            _id = get_current_identity(session)
            _locale = (_id.language if _id and _id.language else "zh")
            review_rows = build_review_rows(session, list(variants), locale=_locale)

        # Themes for the preview dropdown inside review panel
        from ..services.themes import list_all_themes
        user_themes = [t for t in list_all_themes() if t["kind"] == "user"]

        # Axis buttons for the new quick-feedback panel (derived from brief)
        from ..services.quick_feedback import get_axes_for_buttons, axis_counts, get_job_axis_state
        axis_buttons = get_axes_for_buttons()
        axis_counts_map = axis_counts(session)
        job_axis_state = get_job_axis_state(session, job_id)

        # Outreach state for step 4 (sent log, drafts, responses)
        from ..models import OutreachAttempt
        from datetime import timezone as _tz
        outreach_rows = (
            session.execute(
                select(OutreachAttempt).where(OutreachAttempt.job_id == job_id)
                .order_by(OutreachAttempt.sent_at.desc().nullsfirst())
            ).scalars().all()
        )
        # Compute days_since_sent server-side (avoids tz issues in template)
        now_aware = datetime.now(_tz.utc)
        for o in outreach_rows:
            if o.sent_at:
                # SQLite strips tzinfo; treat naive as UTC
                sent = o.sent_at if o.sent_at.tzinfo else o.sent_at.replace(tzinfo=_tz.utc)
                o.days_since_sent = (now_aware - sent).days
            else:
                o.days_since_sent = None

        outreach_drafts = [o for o in outreach_rows if o.sent_at is None]
        outreach_sent = [o for o in outreach_rows if o.sent_at is not None]
        outreach_responses = [o for o in outreach_sent if o.response_received]

        # Prev/next navigation respecting current sort+filter context
        nav_sort = (request.args.get("sort") or "tier")
        if nav_sort not in ("tier", "scraped_desc", "scraped_asc"):
            nav_sort = "tier"
        nav_tier_raw = request.args.get("tier", "").strip()
        nav_tier = int(nav_tier_raw) if nav_tier_raw and nav_tier_raw.lstrip("-").isdigit() else None
        nav_status = request.args.get("status") or None
        nav_q = (request.args.get("q") or "").strip() or None

        # DB sort: use scraped_desc as base for nav; if user is in tier mode we still walk by scraped_desc
        nav_db_sort = nav_sort if nav_sort in ("scraped_desc", "scraped_asc") else "scraped_desc"
        nav_rows = list_jobs_by_tier(
            session, tier=nav_tier, limit=500, status=nav_status,
            sort=nav_db_sort, q_text=nav_q,
        )
        nav_ids = [j.id for j, _ in nav_rows]
        prev_job_id = next_job_id = None
        if job_id in nav_ids:
            idx = nav_ids.index(job_id)
            prev_job_id = nav_ids[idx - 1] if idx > 0 else None
            next_job_id = nav_ids[idx + 1] if idx + 1 < len(nav_ids) else None

        # Build query string suffix to preserve sort/filter context across clicks
        nav_qs_parts = []
        if nav_tier is not None: nav_qs_parts.append(f"tier={nav_tier}")
        if nav_status: nav_qs_parts.append(f"status={nav_status}")
        if nav_q: nav_qs_parts.append(f"q={nav_q}")
        if nav_sort != "tier": nav_qs_parts.append(f"sort={nav_sort}")
        nav_qs = ("?" + "&".join(nav_qs_parts)) if nav_qs_parts else ""

        # JD keyword coverage (Resume-Matcher inspired: hit/miss highlight)
        # 仅在已经跑过一次 rewrite 攒下 keywords_extracted 的 job 上有数据
        from ..services.jd_coverage import compute_coverage, render_jd_with_highlights
        from ..models import ResumeFact
        jd_coverage = None
        jd_html_orig = None
        jd_html_translated = None
        if latest_attempt and (latest_attempt.keywords_extracted or []):
            facts = (
                session.execute(
                    select(ResumeFact).where(ResumeFact.active.is_(True))
                )
                .scalars()
                .all()
            )
            fact_texts = [f.text for f in facts if f.text]
            jd_coverage = compute_coverage(
                latest_attempt.keywords_extracted, fact_texts
            )
            jd_html_orig = render_jd_with_highlights(
                job.description or "",
                jd_coverage.hit_keywords,
                jd_coverage.miss_keywords,
            )
            if job.description_translated:
                jd_html_translated = render_jd_with_highlights(
                    job.description_translated,
                    jd_coverage.hit_keywords,
                    jd_coverage.miss_keywords,
                )

        return render_template(
            "jobs/detail.html",
            job=job,
            workflow_stage=workflow_stage,
            matches=matches,
            tier_labels=TIER_LABELS,
            thumbs_summary=thumbs_summary,
            jd_lang_info=jd_lang_info,
            effort=effort,
            active_step_id=active_step_id,
            latest_attempt=latest_attempt,
            review_rows=review_rows,
            user_themes=user_themes,
            axis_buttons=axis_buttons,
            axis_counts=axis_counts_map,
            job_axis_state=job_axis_state,
            outreach_drafts=outreach_drafts,
            outreach_sent=outreach_sent,
            outreach_responses=outreach_responses,
            upgrade_prompt=None,
            last_axis=None,
            last_sentiment=None,
            last_action=None,
            prev_job_id=prev_job_id,
            next_job_id=next_job_id,
            nav_qs=nav_qs,
            nav_total=len(nav_ids),
            nav_index=(nav_ids.index(job_id) + 1) if job_id in nav_ids else 0,
            jd_coverage=jd_coverage,
            jd_html_orig=jd_html_orig,
            jd_html_translated=jd_html_translated,
        )


@bp.route("/<job_id>/status", methods=["POST"])
def change_status(job_id: str):
    new_status = (request.form.get("status") or "").strip()
    valid = {"new", "reviewed", "applied", "interview", "offer", "rejected", "passed"}
    is_htmx = request.headers.get("HX-Request") == "true"

    if new_status not in valid:
        if is_htmx:
            return ("", 400)
        flash(f"Invalid status: {new_status}", "error")
        return redirect(url_for("jobs.detail", job_id=job_id))

    confirmed = request.form.get("confirmed") == "1"

    # Adversarial Voice submit-time reflection: if user is moving a job to "applied"
    # for the first time AND there's something material to surface, interrupt with
    # a one-step reflection page. For HTMX clients, we send `HX-Redirect` so the
    # browser does a full-page navigation to the reflection screen (a fragment
    # swap would bury the reflection inside the status card — wrong UX).
    if new_status == "applied" and not confirmed:
        from ..services.pre_submit_reflection import build_reflection
        with session_scope() as session:
            job = session.get(Job, job_id)
            if not job:
                if is_htmx:
                    return ("", 404)
                flash(_("Job not found."), "error")
                return redirect(url_for("jobs.list_view"))
            already_applied_once = job.status in {"applied", "interview", "offer", "rejected"}
            if not already_applied_once:
                reflection = build_reflection(session, job_id)
                if reflection and reflection.has_anything_to_say:
                    if is_htmx:
                        # HX-Redirect tells HTMX to do a real browser redirect.
                        # Empty body — the redirect target page is the reflection.
                        from flask import make_response
                        resp = make_response("", 200)
                        resp.headers["HX-Redirect"] = url_for(
                            "jobs.change_status_reflection", job_id=job_id
                        )
                        return resp
                    return render_template(
                        "jobs/pre_submit_reflection.html",
                        reflection=reflection,
                        job=job,
                    )

    with session_scope() as session:
        job = session.get(Job, job_id)
        if not job:
            if is_htmx:
                return ("", 404)
            flash(_("Job not found."), "error")
            return redirect(url_for("jobs.list_view"))

        old = job.status
        job.status = new_status
        job.status_changed_at = datetime.now(timezone.utc)
        session.add(
            ReflectionEvent(
                kind="job_status_changed",
                payload_json={"from": old, "to": new_status},
                related_job_id=job_id,
            )
        )
        if new_status == "applied" and confirmed:
            session.add(
                ReflectionEvent(
                    kind="applied_with_reflection_seen",
                    payload_json={"job_id": job_id},
                    related_job_id=job_id,
                )
            )

        if is_htmx:
            session.flush()
            return render_template("jobs/_status_card.html", job=job)

    flash(_("状态: %(new_status)s", new_status=new_status), "ok")
    return redirect(url_for("jobs.detail", job_id=job_id))


@bp.route("/<job_id>/status-reflection", methods=["GET"])
def change_status_reflection(job_id: str):
    """Dedicated GET endpoint for the pre-submit reflection screen — used as the
    HX-Redirect target so HTMX clients land on the same reflection UX as
    non-HTMX form submitters get via the original interception path."""
    from ..services.pre_submit_reflection import build_reflection
    with session_scope() as session:
        job = session.get(Job, job_id)
        if not job:
            flash(_("Job not found."), "error")
            return redirect(url_for("jobs.list_view"))
        reflection = build_reflection(session, job_id)
        if reflection is None or not reflection.has_anything_to_say:
            # Race or stale — nothing to surface. Send straight back to detail.
            return redirect(url_for("jobs.detail", job_id=job_id))
        return render_template(
            "jobs/pre_submit_reflection.html",
            reflection=reflection,
            job=job,
        )


@bp.route("/<job_id>/thumb", methods=["POST"])
def thumb(job_id: str):
    """Legacy thumbs-up/down kept for backward compat. Prefer /quick-feedback."""
    direction = request.form.get("direction", "up")
    reason = (request.form.get("reason") or "").strip()
    kind = "job_thumbed_up" if direction == "up" else "job_thumbed_down"
    with session_scope() as session:
        session.add(
            ReflectionEvent(
                kind=kind,
                payload_json={"reason": reason} if reason else {},
                related_job_id=job_id,
            )
        )
    flash("👍" if direction == "up" else "👎", "ok")
    return redirect(url_for("jobs.detail", job_id=job_id))


@bp.route("/<job_id>/quick-feedback", methods=["POST"])
def quick_feedback(job_id: str):
    """Axis-tagged quick feedback. Replaces the old thumb mechanism with
    structured signals that flow into preference brief upgrade prompts.

    Behavior is IDEMPOTENT per (job, axis):
      - First click "薪资 -": records
      - Second click "薪资 -": clears
      - Click "薪资 +" (after -): flips
    Counts for upgrade-pattern detection use DISTINCT jobs.
    """
    from ..services.quick_feedback import (
        record_axis_feedback, axis_counts, get_axes_for_buttons,
        detect_upgrade_pattern, get_job_axis_state,
    )

    axis = request.form.get("axis", "").strip()
    sentiment = request.form.get("sentiment", "").strip()
    reason = (request.form.get("reason") or "").strip()

    if sentiment not in ("+", "-"):
        return ("bad sentiment", 400)
    if not axis:
        axis = "other"

    is_htmx = request.headers.get("HX-Request") == "true"

    with session_scope() as session:
        job = session.get(Job, job_id)
        if not job:
            return ("not found", 404)
        _ev, action = record_axis_feedback(
            session, job=job, axis=axis, sentiment=sentiment, reason=reason or None
        )
        # Only check upgrade pattern when action set/flipped (not when clearing)
        upgrade_prompt = None
        if action != "cleared":
            upgrade_prompt = detect_upgrade_pattern(session, axis=axis, sentiment=sentiment)
        counts = axis_counts(session)
        job_state = get_job_axis_state(session, job_id)

        if is_htmx:
            return render_template(
                "jobs/_quick_feedback.html",
                job=job,
                axis_buttons=get_axes_for_buttons(),
                axis_counts=counts,
                job_axis_state=job_state,
                last_axis=axis,
                last_sentiment=sentiment if action != "cleared" else None,
                last_action=action,
                upgrade_prompt=upgrade_prompt,
            )

    _action_msgs = {"set": _("✓ 已记录"), "flipped": _("↻ 已切换"), "cleared": _("✗ 已取消")}
    flash(_action_msgs.get(action, _("已记录")), "ok")
    return redirect(url_for("jobs.detail", job_id=job_id))


@bp.route("/<job_id>/effort/panel", methods=["GET"])
def effort_panel(job_id: str):
    """HTMX-friendly: render the effort pack checklist for one job."""
    from ..services.effort_pack import build_view
    with session_scope() as session:
        job = session.get(Job, job_id)
        if not job:
            return ("Job not found", 404)
        view = build_view(session, job)
        return render_template("jobs/_effort_pack.html", view=view, job=job)


@bp.route("/<job_id>/effort/<step_id>/override", methods=["POST"])
def effort_override(job_id: str, step_id: str):
    """Toggle a manual override on one step."""
    from ..services.effort_pack import build_view, set_override
    override = (request.form.get("override") or "").strip() or None
    if override == "":
        override = None
    if override is not None and override not in {"done", "skipped", "n/a"}:
        return ("bad override", 400)
    with session_scope() as session:
        job = session.get(Job, job_id)
        if not job:
            return ("not found", 404)
        try:
            set_override(session, job_id, step_id, override)
        except ValueError as e:
            return (str(e), 400)
        view = build_view(session, job)
    if request.headers.get("HX-Request") == "true":
        return render_template("jobs/_effort_pack.html", view=view, job=job)
    return redirect(url_for("jobs.detail", job_id=job_id))


@bp.route("/<job_id>/classify", methods=["POST"])
def classify_now(job_id: str):
    settings = current_app.config["SETTINGS"]
    is_htmx = request.headers.get("HX-Request") == "true"
    if not settings.has_llm:
        if is_htmx:
            # HTMX: render the card with an inline error so the user sees what
            # happened in-place rather than a swallowed redirect
            with session_scope() as session:
                job = session.get(Job, job_id)
                matches = session.execute(
                    select(JobMatch).where(JobMatch.job_id == job_id).order_by(JobMatch.matched_at.desc())
                ).scalars().all()
            return render_template(
                "jobs/_match_card.html",
                job=job, matches=matches, tier_labels=TIER_LABELS,
                classify_error="没有配置 LLM_API_KEY，无法分类。",
            )
        flash(_("没有配置 LLM_API_KEY，无法分类。"), "error")
        return redirect(url_for("jobs.detail", job_id=job_id))
    with session_scope() as session:
        job = session.get(Job, job_id)
        if not job:
            if is_htmx:
                return ("", 404)
            flash(_("Job not found."), "error")
            return redirect(url_for("jobs.list_view"))
        match = classify_job(
            session,
            job,
            provider=settings.llm_provider,
            api_key=settings.llm_api_key,
            model=settings.llm_model,
        )
        if is_htmx:
            session.flush()
            matches = session.execute(
                select(JobMatch).where(JobMatch.job_id == job_id).order_by(JobMatch.matched_at.desc())
            ).scalars().all()
            return render_template(
                "jobs/_match_card.html",
                job=job, matches=matches, tier_labels=TIER_LABELS,
            )
    flash(_("分类完成: Tier %(match_tier)s (score %(match_score)s)", match_tier=match.tier, match_score=match.score), "ok")
    return redirect(url_for("jobs.detail", job_id=job_id))


@bp.route("/<job_id>/translate", methods=["POST"])
def translate_jd(job_id: str):
    """Translate JD to user-chosen target language. Records the choice
    so future translations default to the user's most-used language.
    """
    from ..services.translation import (
        translate_jd as _translate,
        detect_language_simple,
        record_translation_choice,
        SUPPORTED_TARGETS,
        LANGUAGE_NAMES,
    )
    from ..services.llm import LLMError

    settings = current_app.config["SETTINGS"]
    is_htmx = request.headers.get("HX-Request") == "true"

    target_lang = (request.form.get("lang") or "zh").strip()
    if target_lang not in SUPPORTED_TARGETS:
        target_lang = "zh"

    if not settings.has_llm:
        if is_htmx:
            return (
                '<div class="flash error" style="margin: 8px 0">'
                '没有配置 LLM_API_KEY，无法翻译。在 .env 设 LLM_API_KEY 后重启。'
                '</div>',
                200,
            )
        flash(_("没有配置 LLM_API_KEY，无法翻译。"), "error")
        return redirect(url_for("jobs.detail", job_id=job_id))

    with session_scope() as session:
        job = session.get(Job, job_id)
        if not job:
            return ("", 404) if is_htmx else redirect(url_for("jobs.list_view"))

        # Re-use cache only if it's in the same target language
        if job.description_translated and job.description_translated_lang == target_lang:
            translated = job.description_translated
        else:
            try:
                translated = _translate(
                    job.description or "",
                    target_lang=target_lang,
                    provider=settings.llm_provider,
                    api_key=settings.llm_api_key,
                    model=settings.llm_model,
                )
            except LLMError as exc:
                log.warning("Translation failed for job %s: %s", job_id, exc)
                if is_htmx:
                    return (
                        f'<div class="flash error" style="margin: 8px 0">翻译失败：{exc}</div>',
                        200,
                    )
                flash(_("翻译失败：%(exc)s", exc=exc), "error")
                return redirect(url_for("jobs.detail", job_id=job_id))

            job.description_translated = translated
            job.description_translated_lang = target_lang
            session.add(ReflectionEvent(
                kind="jd_translated",
                payload_json={
                    "job_id": job_id,
                    "from_lang": detect_language_simple(job.description or ""),
                    "to_lang": target_lang,
                },
                related_job_id=job_id,
            ))
            record_translation_choice(target_lang)

        if is_htmx:
            return render_template(
                "jobs/_jd_translated.html",
                job=job, translated=translated,
                target_lang=target_lang,
                target_lang_name=LANGUAGE_NAMES.get(target_lang, target_lang),
            )

    return redirect(url_for("jobs.detail", job_id=job_id))


@bp.route("/suggest-aliases", methods=["POST"])
def suggest_aliases():
    """LLM proposes multi-language + same-lang variants for one keyword.
    Returns JSON; UI shows them as checkboxes to opt in to."""
    settings = current_app.config["SETTINGS"]
    if not settings.has_llm:
        return jsonify({"ok": False, "error": "LLM not configured"}), 400

    payload = request.get_json(silent=True) or {}
    canonical = (payload.get("keyword") or "").strip()
    regions = payload.get("regions") or []
    if not canonical:
        return jsonify({"ok": False, "error": "missing keyword"}), 400

    from ..services.keyword_alias import predict_aliases
    aliases = predict_aliases(
        canonical,
        target_regions=regions if isinstance(regions, list) else None,
        provider=settings.llm_provider,
        api_key=settings.llm_api_key,
        model=settings.llm_model,
    )
    return jsonify({
        "ok": True,
        "canonical": canonical,
        "aliases": [
            {"alias": a.alias, "language": a.language, "kind": a.kind, "rationale": a.rationale}
            for a in aliases
        ],
    })


# In-place sub-routes registered onto this same `bp` —— see jobs_config.py
# and jobs_llm.py (both use `from .jobs import bp`). Importing them is
# enough to wire their @bp.route decorators. No new Blueprint, no new
# url_prefix, no separate endpoint namespace.


# ────────────────────────────────────────────────────────────
# Multi-profile scrape config endpoints (ADR-0016 in-place merge)
#
# 5-06 用户原意：在老 ui 那个接口那里修改 → 本 section 是原 v4 fork
# routes/jobs_config.py 的内容物理合并进 jobs.py，所有 endpoint 注册
# 到同一个 jobs.bp，单一文件单一命名空间。
# ────────────────────────────────────────────────────────────

"""ADR-0016 multi-profile search config UI routes.

Backed by `services/search_config.JobSearchIntent` (JSON-on-disk in
`data/job_search_config.json`). The data layer is already wired into
scraper.run_scrape and tier_classifier — this blueprint exposes the
edit-and-trigger surface to the user.

Routes (mounted under /jobs/search-config):
  GET  /                     — list all profiles + global config summary
  POST /global               — update GlobalIntent (user_languages, exclude...)
  GET  /<profile_id>         — edit form for one profile
  POST /<profile_id>         — save profile edits
  POST /new                  — create a blank profile (redirect to edit)
  POST /<profile_id>/toggle  — enabled flag flip
  POST /<profile_id>/delete  — delete profile (二次确认 via form)
  POST /<profile_id>/scrape  — trigger run_scrape_for_profile

The form intentionally exposes EVERY field as plain inputs / textareas — a
"power user" UI. No hidden state, no LLM-inferred fields. The user owns the
intent record (ADR-0008 tool-assumptions-overridable).
"""

import logging
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from flask import (
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)

from ..extensions import session_scope
from ..services.scrape_config import (
    Blacklist,
    Dealbreakers,
    GlobalIntent,
    JobSearchIntent,
    ProfileConfig,
    TierQuota,
    load,
    save,
)

log = logging.getLogger(__name__)



# ADR-0016 补丁 D: country flag emoji map (used by list.html)
COUNTRY_FLAGS: dict[str, str] = {
    "Germany": "🇩🇪", "Deutschland": "🇩🇪",
    "Switzerland": "🇨🇭", "Schweiz": "🇨🇭",
    "Italy": "🇮🇹", "Italia": "🇮🇹",
    "France": "🇫🇷",
    "Spain": "🇪🇸", "España": "🇪🇸",
    "Netherlands": "🇳🇱", "Nederland": "🇳🇱",
    "Austria": "🇦🇹", "Österreich": "🇦🇹",
    "Poland": "🇵🇱", "Polska": "🇵🇱",
    "Belgium": "🇧🇪",
    "Sweden": "🇸🇪",
    "Denmark": "🇩🇰",
    "Norway": "🇳🇴",
    "Finland": "🇫🇮",
    "UK": "🇬🇧", "United Kingdom": "🇬🇧", "England": "🇬🇧",
    "Ireland": "🇮🇪",
    "USA": "🇺🇸", "United States": "🇺🇸",
    "Canada": "🇨🇦",
    "China": "🇨🇳", "中国": "🇨🇳",
    "Hong Kong": "🇭🇰",
    "Singapore": "🇸🇬",
    "Japan": "🇯🇵",
    "Korea": "🇰🇷", "South Korea": "🇰🇷",
    "Australia": "🇦🇺",
    "New Zealand": "🇳🇿",
}


def _flag_for(country: str) -> str:
    return COUNTRY_FLAGS.get(country, "🌍")


def _group_profiles_by_country(profiles: list[ProfileConfig]) -> list[tuple[str, str, str, list[ProfileConfig]]]:
    """Returns list of (flag, country_label, group_key, profiles).

    group_key is "_other" for profiles with no country (sorts last).
    Otherwise sorted: more-enabled groups first, then alphabetical.
    """
    by_country: dict[str, list[ProfileConfig]] = {}
    for p in profiles:
        key = p.country or "_other"
        by_country.setdefault(key, []).append(p)

    def sort_key(item: tuple[str, list[ProfileConfig]]) -> tuple:
        country, profs = item
        if country == "_other":
            return (1, 0, country)  # always last
        return (0, -sum(1 for p in profs if p.enabled), country)

    out: list[tuple[str, str, str, list[ProfileConfig]]] = []
    for country, profs in sorted(by_country.items(), key=sort_key):
        if country == "_other":
            out.append(("🌍", "远程 / 未指定", country, profs))
        else:
            out.append((_flag_for(country), country, country, profs))
    return out


def _data_dir():
    return current_app.config["SETTINGS"].data_dir


# ─── In-flight scrape lock ────────────────────────────────
# When a user double-clicks "一键抓取", or hits scrape on profile A while
# another scrape is mid-flight, we'd otherwise spawn parallel scrapes:
#   · jobspy fans out 8 profiles × 9 keywords × 2 sites = 144 concurrent reqs
#   · LinkedIn rate-limits and blacklists
#   · merge_into_db gets concurrent writers; ReflectionEvent stream gets dupes
# A simple file lock with PID + timestamp guards this. Stale lock (>10 min)
# is reclaimed automatically — assume the previous process crashed.

class ScrapeInFlightError(Exception):
    """Another scrape is currently running."""


_LOCK_NAME = ".scrape_in_flight"
_STALE_AFTER_SEC = 10 * 60  # 10 minutes


@contextmanager
def _scrape_lock(data_dir: Path):
    """Atomic acquire-or-fail file lock for the duration of a scrape.

    Uses O_CREAT|O_EXCL to make the existence-check and creation atomic at
    the OS level. Without this, the prior `if exists() ... write_text()`
    pattern was a TOCTOU race: 5 simultaneous form POSTs could all see
    "no lock" and all enter the critical section in parallel — defeating
    the whole point of the in-flight guard. Behaviour now:

      - First caller: O_EXCL creates the file → owns the lock.
      - Concurrent callers: FileExistsError → check mtime → either raise
        ScrapeInFlightError (if fresh) or reclaim (if stale > 10 min).

    Stale-reclaim itself is racy under contention (two callers both decide
    the lock is stale, both unlink, both create) — we serialise that path
    with a process-local mutex so at most one stale-reclaim happens at a
    time. Cross-process stale-reclaim still relies on the unlink+O_EXCL
    sequence: only one process wins the create even if both unlinked.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    lock_path = data_dir / _LOCK_NAME
    payload = f"pid={os.getpid()}\nstarted={time.time()}\n".encode("utf-8")

    fd = None
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        # Lock already exists. Decide: stale (reclaim) or live (raise).
        try:
            age_sec = time.time() - lock_path.stat().st_mtime
        except FileNotFoundError:
            # Vanished between exists check and stat — try one more O_EXCL
            try:
                fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                raise ScrapeInFlightError("scrape running — try again in a moment")
        if fd is None:
            if age_sec < _STALE_AFTER_SEC:
                raise ScrapeInFlightError(
                    f"scrape running for {age_sec:.0f}s — wait or 10 min for stale auto-clear"
                )
            log.warning("stale scrape lock found (%.0fs old) — reclaiming", age_sec)
            with _stale_reclaim_lock:
                # Re-check under mutex (TOCTOU guard within this process)
                try:
                    age_sec = time.time() - lock_path.stat().st_mtime
                    if age_sec < _STALE_AFTER_SEC:
                        # Another thread already reclaimed and is running fresh
                        raise ScrapeInFlightError(
                            f"another caller just reclaimed the lock ({age_sec:.0f}s old)"
                        )
                except FileNotFoundError:
                    pass  # someone unlinked already, race onward
                try:
                    lock_path.unlink()
                except FileNotFoundError:
                    pass
                try:
                    fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
                except FileExistsError:
                    raise ScrapeInFlightError("scrape running — try again in a moment")

    try:
        os.write(fd, payload)
        os.close(fd)
        fd = None  # marker so finally doesn't double-close
        yield
    finally:
        if fd is not None:
            try: os.close(fd)
            except OSError: pass
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


_stale_reclaim_lock = threading.Lock()


def _split_lines(raw: str) -> list[str]:
    """Form textareas — split by newline AND comma, trim, drop empties."""
    out: list[str] = []
    for chunk in (raw or "").replace(",", "\n").splitlines():
        s = chunk.strip()
        if s:
            out.append(s)
    return out


def _parse_aliases(raw: str) -> dict[str, list[str]]:
    """Parse the keyword_aliases textarea into a {canonical: [alias, ...]} dict.

    Format: one mapping per line, ``canonical: alias1, alias2``. Empty
    canonical lines and empty alias lists are dropped silently.
    """
    out: dict[str, list[str]] = {}
    for line in (raw or "").splitlines():
        if ":" not in line:
            continue
        canonical, rest = line.split(":", 1)
        canonical = canonical.strip()
        if not canonical:
            continue
        aliases = [s.strip() for s in rest.split(",") if s.strip()]
        if aliases:
            out[canonical] = aliases
    return out


def format_aliases(d: dict[str, list[str]]) -> str:
    """Inverse of _parse_aliases — for the textarea pre-fill."""
    return "\n".join(f"{k}: {', '.join(v)}" for k, v in (d or {}).items())


# ─────────────────────────────────────────────────────────
# List + global
# ─────────────────────────────────────────────────────────


@bp.route("/search-config/", methods=["GET"])
def search_config_overview():
    """C1 (5-08): 求职意图重新成为独立页面。
    list.html 仍保留同一份 _panel.html partial（向后兼容书签），但 sidebar
    现在指 here，因为编辑流程不该和 jobs 列表挤在一个页面。"""
    intent = load(_data_dir())
    initial_groups = []
    for prof in intent.profiles:
        loc_str = ", ".join(x for x in (prof.city, prof.country) if x) or (prof.label or "")
        initial_groups.append({
            "id": prof.id,
            "label": prof.label,
            "enabled": prof.enabled,
            "locations": loc_str,
            "keywords": ", ".join(prof.keywords),
            "hours_old": prof.hours_old or 168,
            "city": prof.city,
            "country": prof.country,
            "user_prompt": prof.user_prompt,
            "country_must_be": "\n".join(prof.dealbreakers.country_must_be),
            "language_blocked": "\n".join(prof.dealbreakers.language_blocked),
            "blacklist_companies": "\n".join(prof.blacklist.companies),
            "blacklist_keywords": "\n".join(prof.blacklist.keywords),
            "keyword_aliases": "\n".join(
                f"{k}={','.join(v)}" for k, v in prof.keyword_aliases.items()
            ),
        })
    return render_template(
        "jobs/searches/overview.html",
        initial_groups=initial_groups,
    )


@bp.route("/search-config/global", methods=["POST"])
def search_config_update_global():
    intent = load(_data_dir())
    intent.global_ = GlobalIntent(
        user_languages=_split_lines(request.form.get("user_languages", "")),
        exclude_categories=_split_lines(request.form.get("exclude_categories", "")),
        min_salary=_parse_float(request.form.get("min_salary", "")),
        remote_preference=request.form.get("remote_preference", "any") or "any",
    )
    save(_data_dir(), intent)
    flash(_("全局配置已保存"), "ok")
    return redirect(url_for("jobs.list_view") + "?scrape=1#scrape")


def _parse_float(raw: str):
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


# ─────────────────────────────────────────────────────────
# Profile edit
# ─────────────────────────────────────────────────────────


@bp.route("/search-config/<profile_id>", methods=["GET"])
def search_config_edit(profile_id: str):
    """5-07: 求职意图编辑融在主页面，不再独立 edit 页 — redirect。"""
    return redirect(url_for("jobs.list_view") + "?scrape=1#scrape")


@bp.route("/search-config/<profile_id>", methods=["POST"])
def search_config_save_profile(profile_id: str):
    intent = load(_data_dir())
    p = intent.find(profile_id)
    if p is None:
        flash(f"Profile '{profile_id}' not found", "error")
        return redirect(url_for("jobs.search_config_overview"))

    f = request.form
    # Mutate the matched ProfileConfig in place
    p.label = (f.get("label") or "").strip() or p.label
    p.enabled = f.get("enabled") == "on"
    p.city = (f.get("city") or "").strip()
    p.country = (f.get("country") or "").strip()
    try:
        p.radius_km = int(f.get("radius_km", "30"))
    except ValueError:
        p.radius_km = 30
    p.keywords = _split_lines(f.get("keywords", ""))
    p.user_prompt = (f.get("user_prompt") or "").strip()
    p.blacklist = Blacklist(
        companies=_split_lines(f.get("blacklist_companies", "")),
        keywords=_split_lines(f.get("blacklist_keywords", "")),
    )
    p.dealbreakers = Dealbreakers(
        country_must_be=_split_lines(f.get("country_must_be", "")),
        language_required=_split_lines(f.get("language_required", "")),
        language_blocked=_split_lines(f.get("language_blocked", "")),
        visa_required=f.get("visa_required") == "on",
        allow_remote_in_region=f.get("allow_remote_in_region") == "on",
    )

    def _q(name: str, default: int) -> int:
        try:
            return max(0, int(f.get(name, str(default))))
        except ValueError:
            return default

    p.tier_quota = TierQuota(
        tier_1=_q("tier_quota_1", 5),
        tier_2=_q("tier_quota_2", 3),
        tier_3=_q("tier_quota_3", 2),
    )

    # ADR-0016 补丁: hours_old + keyword_aliases
    ho_raw = (f.get("hours_old") or "").strip()
    if not ho_raw:
        p.hours_old = None
    else:
        try:
            v = int(ho_raw)
            p.hours_old = v if v > 0 else None
        except ValueError:
            p.hours_old = 168
    p.keyword_aliases = _parse_aliases(f.get("keyword_aliases", ""))

    save(_data_dir(), intent)
    flash(_("Profile '%(p_label)s' 已保存", p_label=p.label), "ok")
    return redirect(url_for("jobs.search_config_edit", profile_id=p.id))


# ─────────────────────────────────────────────────────────
# Lifecycle: new / toggle / delete / scrape
# ─────────────────────────────────────────────────────────


@bp.route("/search-config/new", methods=["POST"])
def search_config_new():
    intent = load(_data_dir())
    label = (request.form.get("label") or "新 Profile").strip()
    pid = _unique_id(intent, label)
    intent.profiles.append(
        ProfileConfig(id=pid, label=label, enabled=False)
    )
    save(_data_dir(), intent)
    flash(_("已创建 '%(label)s'，请填写详情", label=label), "ok")
    return redirect(url_for("jobs.search_config_edit", profile_id=pid))


def _unique_id(intent: JobSearchIntent, label: str) -> str:
    """Slug + collision-suffix."""
    from ..services.scrape_config import slug

    base = slug(label)
    if not intent.find(base):
        return base
    i = 2
    while intent.find(f"{base}-{i}"):
        i += 1
    return f"{base}-{i}"


@bp.route("/search-config/<profile_id>/toggle", methods=["POST"])
def search_config_toggle(profile_id: str):
    intent = load(_data_dir())
    p = intent.find(profile_id)
    if p is None:
        flash(_("Profile 不存在"), "error")
        return redirect(url_for("jobs.search_config_overview"))
    p.enabled = not p.enabled
    save(_data_dir(), intent)
    status_word = _("启用") if p.enabled else _("禁用")
    flash(_("Profile '%(p_label)s' 已%(status)s", p_label=p.label, status=status_word), "ok")
    return redirect(url_for("jobs.search_config_overview"))


@bp.route("/search-config/<profile_id>/delete", methods=["POST"])
def search_config_delete(profile_id: str):
    """Delete with二次确认 — form must include `confirm=yes`.

    Also clears `Job.matched_profile_id` on every job that pointed to this
    profile, so dashboards / classifiers don't report orphan IDs (ADR-0017
    consequence: single source of truth means delete cascades to consumers).
    """
    if request.form.get("confirm") != "yes":
        flash(_("删除已取消（缺少二次确认）"), "warn")
        return redirect(url_for("jobs.search_config_edit", profile_id=profile_id))
    intent = load(_data_dir())
    p = intent.find(profile_id)
    if p is None:
        flash(_("Profile 不存在"), "error")
        return redirect(url_for("jobs.search_config_overview"))

    # GC: null out Job.matched_profile_id pointing at this profile
    from sqlalchemy import update
    from ..models import Job

    n_orphans = 0
    with session_scope() as session:
        result = session.execute(
            update(Job)
            .where(Job.matched_profile_id == profile_id)
            .values(matched_profile_id=None)
        )
        n_orphans = result.rowcount or 0

    intent.profiles = [x for x in intent.profiles if x.id != profile_id]
    save(_data_dir(), intent)
    msg = f"Profile '{p.label}' 已删除"
    if n_orphans:
        msg += f"（已清理 {n_orphans} 条 job 的 matched_profile_id）"
    flash(msg, "ok")
    return redirect(url_for("jobs.search_config_overview"))


@bp.route("/search-config/scrape_all", methods=["POST"])
def search_config_scrape_all():
    """ADR-0016 补丁 C: 一键抓取所有 enabled profile (replaces v3 sidebar
    `jobs.scrape_direct` action — same intent, profile-aware now).

    Guarded by `_scrape_lock` so double-clicking or rapid resubmits don't
    spawn parallel scrapes (LinkedIn rate-limit / DB write races).
    """
    intent = load(_data_dir())
    enabled = intent.enabled_profiles()
    if not enabled:
        flash(_("没有启用的 profile — 先去求职意图配置一个"), "warn")
        return redirect(url_for("jobs.search_config_overview"))

    settings = current_app.config["SETTINGS"]
    from ..services.blacklist import load as load_blacklist
    from ..services.scrape_messages import ScrapeTotals, format_scrape_result
    from ..services.scraper import write_progress, clear_progress
    import time as _time

    blacklist = load_blacklist(settings.data_dir)
    totals = ScrapeTotals(profiles_run=len(enabled))
    data_dir = _data_dir()
    grand_totals: dict = {}
    error_samples: list = []
    try:
        with _scrape_lock(data_dir):
            write_progress(
                data_dir, active=True, started_at=_time.time(),
                profile_total=len(enabled), profile_index=0,
                grand_totals={"raw": 0, "new": 0, "updated": 0,
                              "blocked": 0, "gate_blocked": 0, "errors": 0},
            )
            try:
                with session_scope() as session:
                    grand_totals, error_samples = _run_profiles_with_progress(
                        session, enabled,
                        intent=intent, blacklist=blacklist,
                        totals=totals, data_dir=data_dir,
                    )
                    # Auto-classify newly-scraped jobs. Without this step jobs land in
                    # the DB with `tier=None` (no JobMatch row), and `jobs/list.html`
                    # — which groups by tier — renders blank. User-visible symptom:
                    # "抓取后一个岗位也没有". Fix: classify every Job that doesn't yet
                    # have a JobMatch and is owned by one of the profiles we just
                    # scraped. Runs inside the same transaction so partial failures
                    # don't leave half-classified state.
                    write_progress(data_dir, phase="classifying", keyword=None)
                    totals.classified = _classify_pending_for_profiles(session, enabled, settings)
            finally:
                write_progress(data_dir, grand_totals=grand_totals,
                               error_samples=error_samples)
                clear_progress(data_dir)
    except ScrapeInFlightError as exc:
        flash(f"⏳ {exc}", "warn")
        return redirect(url_for("jobs.search_config_overview"))
    except Exception as exc:  # noqa: BLE001
        log.exception("scrape_all failed")
        flash(_("抓取失败: %(exc)s", exc=exc), "error")
        return redirect(url_for("jobs.search_config_overview"))

    msg, level = format_scrape_result(totals, title=f"⚡ {len(enabled)} profile 抓取完成")
    flash(msg, level)
    return redirect(url_for("jobs.search_config_overview"))


def _run_profiles_with_progress(
    session, profiles, *, intent, blacklist, totals, data_dir,
):
    """Iterate profiles, invoke run_scrape_for_profile with a progress callback
    that writes data/.scrape_progress.json so the frontend can poll progress.

    Mutates `totals` (ScrapeTotals) in place. Returns (grand_totals, error_samples).
    Caller is responsible for: starting status (write_progress active=True),
    finalizing (write_progress final, then clear_progress), and the in-flight lock.
    """
    from ..services.scraper import run_scrape_for_profile, write_progress

    grand_totals = {"raw": 0, "new": 0, "updated": 0, "blocked": 0, "gate_blocked": 0, "errors": 0}
    error_samples: list[str] = []

    def _make_kw_cb():
        def cb(**kw):
            kind = kw.get("kind")
            if kind == "keyword_start":
                write_progress(
                    data_dir,
                    phase="keyword_running",
                    keyword=kw.get("keyword"),
                    location=kw.get("location"),
                    sites=kw.get("sites"),
                    keyword_index=kw.get("keyword_index"),
                    keyword_total=kw.get("keyword_total"),
                )
            elif kind == "keyword_done":
                grand_totals["raw"] += kw.get("raw", 0)
                grand_totals["new"] += kw.get("new", 0)
                grand_totals["updated"] += kw.get("updated", 0)
                grand_totals["blocked"] += kw.get("blocked", 0)
                grand_totals["gate_blocked"] += kw.get("gate_blocked", 0)
                if kw.get("had_error"):
                    grand_totals["errors"] += 1
                write_progress(
                    data_dir,
                    profile_totals=kw.get("profile_totals", {}),
                    grand_totals=dict(grand_totals),
                    last_keyword_done={
                        "keyword": kw.get("keyword"),
                        "raw": kw.get("raw"), "new": kw.get("new"),
                        "had_error": kw.get("had_error"),
                    },
                )
        return cb

    for idx, prof in enumerate(profiles, 1):
        write_progress(
            data_dir,
            phase="profile_starting",
            profile_index=idx,
            profile_id=prof.id,
            profile_label=getattr(prof, "label", prof.id),
            keyword=None, keyword_index=None, keyword_total=None,
        )
        try:
            result = run_scrape_for_profile(
                session, prof,
                global_intent=intent.global_,
                blacklist=blacklist,
                progress_cb=_make_kw_cb(),
            )
            totals.absorb(result)
            for e in (result.get("errors") or []):
                if len(error_samples) < 10:
                    error_samples.append(str(e)[:200])
        except Exception:
            log.exception("scrape profile %s failed", prof.id)
            totals.failed_profiles.append(prof.label)
            grand_totals["errors"] += 1

    return grand_totals, error_samples


def _classify_pending_for_profiles(session, profiles, settings) -> int:
    """Run tier_classifier on every Job whose `matched_profile_id` is in `profiles`
    and which has no JobMatch row yet. Returns count classified.

    Lives here (not in a generic helper) because:
      · scrape paths are the ONLY entry that writes Job rows without tier
      · the classifier needs the live ProfileConfig, which the caller already has
      · a separate background worker would solve this more elegantly but adds
        ops surface (process supervisor / queue) that doesn't exist yet
    """
    from sqlalchemy import select, exists
    from ..models import Job, JobMatch
    from ..services.tier_classifier import classify_job

    profile_ids = {p.id for p in profiles}
    if not profile_ids:
        return 0
    profiles_by_id = {p.id: p for p in profiles}

    pending = session.execute(
        select(Job).where(
            Job.matched_profile_id.in_(profile_ids),
            ~exists().where(JobMatch.job_id == Job.id),
        )
    ).scalars().all()

    n = 0
    for job in pending:
        try:
            classify_job(
                session, job,
                provider=settings.llm_provider,
                api_key=settings.llm_api_key,
                model=settings.llm_model,
                profile=profiles_by_id.get(job.matched_profile_id),
            )
            n += 1
        except Exception:  # noqa: BLE001
            log.exception("classify_job failed for %s", job.id)
    return n


@bp.route("/search-config/<profile_id>/predict_aliases", methods=["POST"])
def search_config_predict_aliases_route(profile_id: str):
    """ADR-0016 补丁 B: AI 给当前 profile keywords 预测多语言同义词。
    Merges into profile.keyword_aliases — does not replace existing user-edited
    entries."""
    intent = load(_data_dir())
    p = intent.find(profile_id)
    if p is None:
        flash(_("Profile 不存在"), "error")
        return redirect(url_for("jobs.search_config_overview"))
    if not p.keywords:
        flash(_("先填关键词，再让 AI 预测同义词"), "warn")
        return redirect(url_for("jobs.search_config_edit", profile_id=profile_id))

    settings = current_app.config["SETTINGS"]
    from ..services.keyword_alias import predict_aliases

    user_langs = intent.global_.user_languages or ["en"]
    target_regions = [p.country] if p.country else None
    n_added = 0
    try:
        for kw in p.keywords:
            aliases = predict_aliases(
                kw,
                target_regions=target_regions,
                user_languages=user_langs,
                provider=settings.llm_provider,
                api_key=settings.llm_api_key,
                model=settings.llm_model,
            )
            if not aliases:
                continue
            existing = p.keyword_aliases.get(kw, [])
            new_aliases = [a.alias for a in aliases if a.alias not in existing]
            if new_aliases:
                p.keyword_aliases[kw] = existing + new_aliases
                n_added += len(new_aliases)
    except Exception as exc:  # noqa: BLE001
        log.exception("predict_aliases failed for profile %s", profile_id)
        flash(_("AI 预测失败: %(exc)s", exc=exc), "error")
        return redirect(url_for("jobs.search_config_edit", profile_id=profile_id))

    save(_data_dir(), intent)
    if n_added:
        flash(_("AI 添加 %(n_added)s 个同义词候选 — 请审阅，不需要的删掉", n_added=n_added), "ok")
    else:
        flash(_("AI 没找到新的同义词（或没配 LLM key）"), "warn")
    return redirect(url_for("jobs.search_config_edit", profile_id=profile_id))


@bp.route("/search-config/<profile_id>/scrape", methods=["POST"])
def search_config_scrape(profile_id: str):
    """Trigger run_scrape_for_profile for one profile."""
    intent = load(_data_dir())
    p = intent.find(profile_id)
    if p is None or not p.enabled:
        flash(_("Profile 不存在或已禁用"), "error")
        return redirect(url_for("jobs.search_config_overview"))

    settings = current_app.config["SETTINGS"]
    from ..services.blacklist import load as load_blacklist
    from ..services.scraper import run_scrape_for_profile
    from ..services.scrape_messages import ScrapeTotals, format_scrape_result

    blacklist = load_blacklist(settings.data_dir)
    totals = ScrapeTotals(profiles_run=1)
    try:
        with _scrape_lock(_data_dir()):
            with session_scope() as session:
                result = run_scrape_for_profile(
                    session,
                    p,
                    global_intent=intent.global_,
                    blacklist=blacklist,
                )
                totals.absorb(result)
                # Auto-classify (see scrape_all for rationale).
                totals.classified = _classify_pending_for_profiles(session, [p], settings)
    except ScrapeInFlightError as exc:
        flash(f"⏳ {exc}", "warn")
        return redirect(url_for("jobs.search_config_overview"))
    except Exception as exc:  # noqa: BLE001
        log.exception("scrape for profile %s failed", profile_id)
        flash(_("抓取失败: %(exc)s", exc=exc), "error")
        return redirect(url_for("jobs.search_config_overview"))

    msg, level = format_scrape_result(totals, title=f"⚡ {p.label}")
    flash(msg, level)
    return redirect(url_for("jobs.search_config_overview"))

# ────────────────────────────────────────────────────────────
# LLM Settings UI endpoints (ADR-0016 in-place merge)
#
# 5-06 用户原意：'别再弄一个 v4 出来了' → 本 section 是原 v4 fork
# routes/jobs_llm.py 的内容物理合并进 jobs.py，所有 endpoint 注册
# 到同一个 jobs.bp，单一文件单一命名空间。
# ────────────────────────────────────────────────────────────

"""LLM settings UI routes (mounted under /settings/llm).

ADR-0017 in-place: this is `routes/llm_config.py` (not `routes/llm.py` to
avoid clash with `services/llm.py`). Edits write to `data/llm_settings.json`,
which `Settings.from_env()` reads as overlay over .env defaults.
"""

import logging
import time

from flask import (
    current_app, flash, jsonify, redirect, render_template, request, url_for,
)

from ..extensions import session_scope
from ..models import ReflectionEvent
from ..services.llm_settings import (
    KNOWN_BASE_URL_PRESETS,
    KNOWN_MODELS,
    KNOWN_STEPS,
    LLMSettings,
    PRESET_CONFIGS,
    estimate_cost,
    load as load_llm,
    save as save_llm,
)

log = logging.getLogger(__name__)


# (_data_dir defined earlier at L886 — dedup'd 5-07 after jobs_llm merge)


# ─── List + edit ──────────────────────────────────────────


@bp.route("/llm-settings/", methods=["GET"])
def llm_overview():
    settings = current_app.config["SETTINGS"]
    overlay = load_llm(_data_dir())

    # 用量统计：count attempts in last 30 days from ReflectionEvent stream
    usage = _compute_usage_stats()

    # Provider-specific model dropdowns
    effective_provider = overlay.provider or settings.llm_provider
    available_models = KNOWN_MODELS.get(effective_provider, [])

    return render_template(
        "jobs/llm.html",
        settings=settings,
        overlay=overlay,
        usage=usage,
        known_models=KNOWN_MODELS,
        known_steps=KNOWN_STEPS,
        known_base_urls=KNOWN_BASE_URL_PRESETS,
        preset_configs=PRESET_CONFIGS,
        available_models=available_models,
        effective_provider=effective_provider,
    )


@bp.route("/llm-settings/", methods=["POST"])
def llm_update():
    """Save provider / api_key / model / step_overrides / feature_flags."""
    f = request.form
    overlay = load_llm(_data_dir())

    overlay.provider = (f.get("provider") or "").strip()
    # Treat ******* as "unchanged" — UI sends mask if user didn't edit it
    new_key = (f.get("api_key") or "").strip()
    if new_key and "•" not in new_key:
        overlay.api_key = new_key
    elif new_key == "":
        # User explicitly cleared the field — clear overlay so .env wins
        overlay.api_key = ""
    overlay.model = (f.get("model") or "").strip()
    overlay.base_url = (f.get("base_url") or "").strip()

    # Step overrides — checkbox-driven dict of {step: model_id}
    overrides = {}
    for step in KNOWN_STEPS:
        val = (f.get(f"step_{step}") or "").strip()
        if val:
            overrides[step] = val
    overlay.step_overrides = overrides

    # Feature flags — opt-in checkboxes for ADR-0015 #3-5 wiring
    flags = {}
    for flag in ("decompose_bullets", "translate_variants", "native_ats_check"):
        flags[flag] = (f.get(f"flag_{flag}") == "on")
    overlay.feature_flags = flags

    save_llm(_data_dir(), overlay)
    # Refresh app-level Settings so the change takes effect without restart
    try:
        from ..config import Settings

        current_app.config["SETTINGS"] = Settings.from_env()
    except Exception:  # noqa: BLE001
        log.exception("hot-reload of Settings failed; restart may be needed")

    flash(_("LLM 配置已保存"), "ok")
    return redirect(url_for("jobs.llm_overview"))


# ─── Apply preset config ──────────────────────────────────


@bp.route("/llm-settings/preset/<preset_id>", methods=["POST"])
def llm_apply_preset(preset_id: str):
    """One-click apply a `PRESET_CONFIGS` entry. Doesn't touch api_key (the
    user's key is precious; let them keep it across preset switches)."""
    preset = PRESET_CONFIGS.get(preset_id)
    if not preset:
        flash(_("未知预设: %(preset_id)s", preset_id=preset_id), "error")
        return redirect(url_for("jobs.llm_overview"))

    overlay = load_llm(_data_dir())
    if "provider" in preset:
        overlay.provider = preset["provider"]
    if "model" in preset:
        overlay.model = preset["model"]
    if "base_url" in preset:
        overlay.base_url = preset["base_url"]
    overlay.step_overrides = dict(preset.get("step_overrides", {}))
    save_llm(_data_dir(), overlay)

    # Hot-reload Settings
    try:
        from ..config import Settings

        current_app.config["SETTINGS"] = Settings.from_env()
    except Exception:  # noqa: BLE001
        log.exception("hot-reload after preset failed")

    flash(_("已应用预设: %(label)s", label=preset["label"]), "ok")
    return redirect(url_for("jobs.llm_overview"))


# ─── Test connection ──────────────────────────────────────


@bp.route("/llm-settings/test", methods=["POST"])
def llm_test_connection():
    """Ping the configured LLM with a tiny prompt; return success/failure JSON
    so UI can show inline status without page reload."""
    settings = current_app.config["SETTINGS"]
    if not settings.llm_api_key:
        return jsonify({"ok": False, "msg": "未配置 API key"}), 400

    from ..services.llm import LLMConfigError, LLMError, chat

    try:
        t0 = time.time()
        resp = chat(
            provider=settings.llm_provider,
            api_key=settings.llm_api_key,
            model=settings.llm_model,
            base_url=getattr(settings, "llm_base_url", "") or "",
            system="Reply with the single word: pong",
            user_content="ping",
            max_tokens=10,
        )
        elapsed_ms = int((time.time() - t0) * 1000)
        text = (getattr(resp, "text", "") or str(resp))[:50]
        return jsonify({
            "ok": True,
            "model": settings.llm_model,
            "elapsed_ms": elapsed_ms,
            "reply": text,
        })
    except LLMError as exc:
        return jsonify({"ok": False, "msg": f"配置错误: {exc}"}), 400
    except Exception as exc:  # noqa: BLE001
        log.exception("test_connection failed")
        # Distinguish auth/permission/rate-limit (user-fixable) from real
        # server errors. The previous bare `500` was misleading: "unable to
        # reach Anthropic with an invalid key" is a 401, not a server crash.
        msg = str(exc)
        low = msg.lower()
        if "401" in msg or "invalid" in low and "key" in low or "authentication" in low:
            return jsonify({"ok": False, "msg": f"密钥无效: {msg}"}), 401
        if "403" in msg or "permission" in low:
            return jsonify({"ok": False, "msg": f"权限不足: {msg}"}), 403
        if "429" in msg or "rate" in low and "limit" in low:
            return jsonify({"ok": False, "msg": f"配额限流: {msg}"}), 429
        if "timeout" in low or "timed out" in low:
            return jsonify({"ok": False, "msg": f"超时: {msg}"}), 504
        return jsonify({"ok": False, "msg": f"调用失败: {msg}"}), 502


# ─── Usage stats ──────────────────────────────────────────


def _compute_usage_stats() -> dict:
    """Estimate LLM call count + cost from ReflectionEvent stream of last 30d."""
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import select

    settings = current_app.config["SETTINGS"]
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)

    n_attempts = 0
    n_classifications = 0
    try:
        with session_scope() as session:
            # rewrite_attempt_completed | job_classified | etc — proxy for LLM use
            stmt = select(ReflectionEvent).where(ReflectionEvent.created_at >= cutoff)
            for ev in session.execute(stmt).scalars():
                if ev.kind in ("rewrite_attempt_completed", "variant_proposed"):
                    n_attempts += 1
                elif ev.kind == "job_classified":
                    n_classifications += 1
    except Exception as exc:  # noqa: BLE001
        log.debug("usage stats unavailable: %s", exc)

    # Each rewrite attempt ≈ 43 LLM calls (per estimate), each classification ≈ 1
    n_calls = n_attempts * 43 + n_classifications
    cost_now = estimate_cost(settings.llm_provider, settings.llm_model, n_calls)

    # What if user switched? Show price for each known model.
    cost_alternatives = [
        {
            "model": mid,
            "label": label,
            "cost": estimate_cost(settings.llm_provider, mid, n_calls),
        }
        for mid, label, _, _, _ in KNOWN_MODELS.get(settings.llm_provider, [])
    ]

    return {
        "n_attempts_30d": n_attempts,
        "n_classifications_30d": n_classifications,
        "estimated_calls_30d": n_calls,
        "current_cost_30d": cost_now,
        "alternatives": cost_alternatives,
    }


# ────────────────────────────────────────────────────────────
# v3 老抓取接口演化版（5-07 user 原意：在老 ui 接口上扩展 multi-profile）
# POST /jobs/scrape       — 表单提交（保存 multi-profile 配置 + 跑抓取）
# POST /jobs/scrape-direct — 一键抓取（用 last_scrape.json 跑，不需要 form）
# ────────────────────────────────────────────────────────────


def _parse_scrape_groups(form) -> list[dict]:
    """Parse 老抓取面板 form 字段（兼容 v3 + multi-profile 升级字段）。

    每组字段（1-indexed）：
      - locations_N         (legacy "City, Country" 字符串，可多个逗号分)
      - keywords_N          (逗号分隔)
      - hours_old_N         (24/72/168/720/0)
      - enabled_N           (checkbox)
      - city_N / country_N  (multi-profile 升级，可空 → 从 locations 推)
      - user_prompt_N       (per-profile 偏好描述)
      - country_must_be_N   (\\n 分隔，dealbreaker)
      - language_blocked_N  (\\n 分隔)
      - blacklist_companies_N / blacklist_keywords_N (\\n 分隔)
      - keyword_aliases_N   (free-form text，"k=v1,v2" 一行一条)
    """
    out: list[dict] = []
    for i in range(1, 21):
        locs_raw = (form.get(f"locations_{i}") or "").strip()
        kws_raw = (form.get(f"keywords_{i}") or "").strip()
        if not locs_raw or not kws_raw:
            continue
        if not (form.get(f"enabled_{i}") in ("on", "1", "true", "yes")):
            continue
        try:
            ho = int((form.get(f"hours_old_{i}") or "168").strip() or "168")
            hours_old = ho if ho > 0 else None
        except ValueError:
            hours_old = 168

        loc_first = [s.strip() for s in locs_raw.split(",") if s.strip()][0] if locs_raw else ""
        city = (form.get(f"city_{i}") or "").strip()
        country = (form.get(f"country_{i}") or "").strip()
        if not city or not country:
            parts = [p.strip() for p in loc_first.split(",")]
            if not city:
                city = parts[0] if parts else ""
            if not country:
                country = parts[-1] if len(parts) >= 2 else ""

        out.append({
            "id": (form.get(f"id_{i}") or "").strip(),  # may be empty for newly added groups
            "label": (form.get(f"label_{i}") or f"{country}-{city}").strip() or f"组{i}",
            "locations": [s.strip() for s in locs_raw.split(",") if s.strip()],
            "keywords": [s.strip() for s in kws_raw.split(",") if s.strip()],
            "hours_old": hours_old,
            "enabled": True,
            "city": city,
            "country": country,
            "user_prompt": (form.get(f"user_prompt_{i}") or "").strip(),
            "country_must_be": [
                s.strip() for s in (form.get(f"country_must_be_{i}") or "").splitlines() if s.strip()
            ] or ([country] if country else []),
            "language_blocked": [
                s.strip() for s in (form.get(f"language_blocked_{i}") or "").splitlines() if s.strip()
            ],
            "blacklist_companies": [
                s.strip() for s in (form.get(f"blacklist_companies_{i}") or "").splitlines() if s.strip()
            ],
            "blacklist_keywords": [
                s.strip() for s in (form.get(f"blacklist_keywords_{i}") or "").splitlines() if s.strip()
            ],
            "keyword_aliases_raw": (form.get(f"keyword_aliases_{i}") or "").strip(),
        })
    return out


def _form_groups_to_multi_profile(groups: list[dict]):
    """Merge form-submitted groups onto the existing on-disk intent.

    The form does NOT carry every ProfileConfig field — fields like
    `tier_quota`, `radius_km`, and the full set of `dealbreakers`
    (language_required, visa_required, allow_remote_in_region) only live
    in the per-profile edit page. A naive overwrite would silently wipe
    them every time the user clicks 📥 抓取.

    We also preserve `global_` (user_languages / min_salary / exclude_categories /
    remote_preference) which the form never carries, and we preserve each
    profile's stable `id` so URLs and `Job.matched_profile_id` references
    keep pointing somewhere real after a save.

    Behaviour:
      • Form group with `id_<i>` matching an existing profile  → merge
        form fields ON TOP of that profile (preserves tier_quota etc.)
      • Form group with empty `id_<i>` (newly added in the UI)   → create
        fresh ProfileConfig with a slugged id (collision-suffixed).
      • Profiles that are on disk but absent from the form are dropped —
        the form is the canonical list of currently configured profiles.
    """
    from ..services.scrape_config import (
        ProfileConfig, JobSearchIntent, TierQuota,
        Blacklist, Dealbreakers, slug, load,
    )

    existing = load(_data_dir())
    by_id = {p.id: p for p in existing.profiles}

    profiles: list[ProfileConfig] = []
    used_ids: set[str] = set()

    def _alloc_new_id(country: str, city: str, i: int) -> str:
        base = slug(f"{country}-{city}") if country else slug(f"profile-{i}")
        if base and base not in used_ids and base not in by_id:
            return base
        n = 2
        while True:
            candidate = f"{base}-{n}"
            if candidate not in used_ids and candidate not in by_id:
                return candidate
            n += 1

    for i, g in enumerate(groups, start=1):
        kw_aliases: dict[str, list[str]] = {}
        for line in g.get("keyword_aliases_raw", "").splitlines():
            # Accept both "k=v1,v2" (form) and "k: v1, v2" (per-profile page)
            sep = "=" if "=" in line else (":" if ":" in line else None)
            if sep is None:
                continue
            k, v = line.split(sep, 1)
            vs = [s.strip() for s in v.split(",") if s.strip()]
            if k.strip() and vs:
                kw_aliases[k.strip()] = vs

        prior = by_id.get((g.get("id") or "").strip()) if g.get("id") else None

        if prior is not None:
            pid = prior.id
        else:
            pid = _alloc_new_id(g.get("country", ""), g.get("city", ""), i)
        used_ids.add(pid)

        # Merge: form fields take precedence; fields not in form fall back
        # to the prior profile (preserves user-edited advanced settings).
        merged_dealbreakers = Dealbreakers(
            country_must_be=g["country_must_be"],
            language_required=(prior.dealbreakers.language_required if prior else []),
            language_blocked=g["language_blocked"],
            visa_required=(prior.dealbreakers.visa_required if prior else False),
            allow_remote_in_region=(
                prior.dealbreakers.allow_remote_in_region if prior else True
            ),
        )

        profiles.append(ProfileConfig(
            id=pid,
            label=g["label"],
            enabled=g["enabled"],
            city=g["city"],
            country=g["country"],
            radius_km=(prior.radius_km if prior else 30),
            keywords=g["keywords"],
            keyword_aliases=kw_aliases,
            user_prompt=g["user_prompt"],
            blacklist=Blacklist(
                companies=g["blacklist_companies"],
                keywords=g["blacklist_keywords"],
            ),
            dealbreakers=merged_dealbreakers,
            tier_quota=(prior.tier_quota if prior else TierQuota()),
            hours_old=g["hours_old"],
        ))

    return JobSearchIntent(
        profiles=profiles,
        global_=existing.global_,
    )


@bp.route("/scrape", methods=["POST"])
def scrape_now():
    """老抓取接口演化版：表单提交后保存 multi-profile 配置 + 跑抓取。

    Guarded by `_scrape_lock` (5-06 20:30 共识 A.7) so double-clicking the
    📥 抓取 button doesn't fan out parallel pipelines into LinkedIn / DB.
    Auto-classifies after scrape so jobs land with a tier (otherwise the
    list view is blank — '抓取后一个岗位也没有' bug).

    B1 fix (5-08): now counts gate_blocked + raw + errors via unified
    ScrapeTotals → format_scrape_result. Old code silently dropped
    gate_blocked, so users hit "新增 0" with no diagnostic.
    """
    from ..services.scrape_config import save as save_intent
    from ..services.scrape_messages import ScrapeTotals, format_scrape_result
    groups = _parse_scrape_groups(request.form)
    if not groups:
        flash(_("请至少填一组（启用的）关键词和地点。"), "error")
        return redirect(url_for("jobs.list_view"))

    settings = current_app.config["SETTINGS"]
    intent = _form_groups_to_multi_profile(groups)
    try:
        save_intent(settings.data_dir, intent)
    except OSError as exc:
        log.warning("failed to save last_scrape.json: %s", exc)

    from ..services.blacklist import load as load_blacklist
    from ..services.scraper import write_progress, clear_progress
    import time as _time

    blacklist = load_blacklist(settings.data_dir)
    enabled_profiles = intent.enabled_profiles()
    totals = ScrapeTotals(profiles_run=len(enabled_profiles))
    data_dir = _data_dir()
    grand_totals: dict = {}
    error_samples: list = []
    try:
        with _scrape_lock(data_dir):
            write_progress(
                data_dir, active=True, started_at=_time.time(),
                profile_total=len(enabled_profiles), profile_index=0,
                grand_totals={"raw": 0, "new": 0, "updated": 0,
                              "blocked": 0, "gate_blocked": 0, "errors": 0},
            )
            try:
                with session_scope() as session:
                    grand_totals, error_samples = _run_profiles_with_progress(
                        session, enabled_profiles,
                        intent=intent, blacklist=blacklist,
                        totals=totals, data_dir=data_dir,
                    )
                    write_progress(data_dir, phase="classifying", keyword=None)
                    totals.classified = _classify_pending_for_profiles(
                        session, enabled_profiles, settings
                    )
            finally:
                write_progress(data_dir, grand_totals=grand_totals,
                               error_samples=error_samples)
                clear_progress(data_dir)
    except ScrapeInFlightError as exc:
        flash(f"⏳ {exc}", "warn")
        return redirect(url_for("jobs.list_view"))

    msg, level = format_scrape_result(totals, title="抓取完成")
    flash(msg, level)
    return redirect(url_for("jobs.list_view"))


@bp.route("/scrape-direct", methods=["POST"])
def scrape_direct():
    """一键抓取：用 last_scrape.json 跑，不需要 form。

    Same guards as scrape_now (in-flight lock + auto-classify).
    """
    from ..services.scrape_config import load as load_intent
    from ..services.scrape_messages import ScrapeTotals, format_scrape_result
    from ..services.scraper import write_progress, clear_progress
    import time as _time
    settings = current_app.config["SETTINGS"]
    intent = load_intent(settings.data_dir)
    enabled = intent.enabled_profiles()
    if not enabled:
        flash(_("还没配置抓取意图。下面是抓取面板，填一组就能跑。"), "error")
        return redirect(url_for("jobs.list_view") + "?scrape=1#scrape")

    from ..services.blacklist import load as load_blacklist

    blacklist = load_blacklist(settings.data_dir)
    totals = ScrapeTotals(profiles_run=len(enabled))
    data_dir = _data_dir()
    grand_totals: dict = {}
    error_samples: list = []

    try:
        with _scrape_lock(data_dir):
            write_progress(
                data_dir, active=True, started_at=_time.time(),
                profile_total=len(enabled), profile_index=0,
                grand_totals={"raw": 0, "new": 0, "updated": 0,
                              "blocked": 0, "gate_blocked": 0, "errors": 0},
            )
            try:
                with session_scope() as session:
                    grand_totals, error_samples = _run_profiles_with_progress(
                        session, enabled,
                        intent=intent, blacklist=blacklist,
                        totals=totals, data_dir=data_dir,
                    )
                    write_progress(data_dir, phase="classifying", keyword=None)
                    totals.classified = _classify_pending_for_profiles(session, enabled, settings)
            finally:
                # Always finalize — even on crash. Keeps last totals visible.
                write_progress(data_dir, grand_totals=grand_totals,
                               error_samples=error_samples)
                clear_progress(data_dir)
    except ScrapeInFlightError as exc:
        flash(f"⏳ {exc}", "warn")
        return redirect(url_for("jobs.list_view"))

    msg, level = format_scrape_result(totals, title="⚡ 一键抓取完成")
    flash(msg, level)
    return redirect(url_for("jobs.list_view"))


# ─── Scrape progress polling ────────────────────────────────────────────────


@bp.route("/api/scrape/progress")
def api_scrape_progress():
    """Return current scrape progress (or {active: false} if no scrape running).

    Frontend polls this every 2s during a scrape. Werkzeug threaded=True
    means this endpoint runs in a separate thread from the scrape worker —
    they share the data_dir via .scrape_progress.json (atomic file write).
    """
    from ..services.scraper import read_progress

    cur = read_progress(_data_dir())
    if cur is None:
        return jsonify({"active": False, "exists": False})
    return jsonify(cur)
