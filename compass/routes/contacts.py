"""Contact panel routes — find people at a job's company + draft outreach.

  GET  /jobs/<job_id>/contacts            HTMX: lazy-load both warm + cold panels
  POST /jobs/<job_id>/contacts/refresh    Force re-search (clears cache for company)
  POST /jobs/<job_id>/contacts/draft      Draft outreach for a specific contact
  POST /jobs/<job_id>/contacts/sent       User confirms they sent it → write OutreachAttempt
  POST /outreach/<attempt_id>/response    User logs a response received
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from flask_babel import gettext as _
from sqlalchemy import select

from ..extensions import session_scope
from ..models import (
    Connection,
    IdentityVersion,
    Job,
    OutreachAttempt,
    ReflectionEvent,
    ResumeFact,
)
from ..services.contact_finder import clear_cache, find_contacts
from ..services.contact_outreach import (
    OutreachInput,
    build_resume_summary_from_facts,
    draft_outreach,
)
from ..services.connections import find_connections_at

bp = Blueprint("contacts", __name__)

log = logging.getLogger(__name__)


@bp.route("/jobs/<job_id>/contacts", methods=["GET"])
def panel(job_id: str):
    """Render warm + cold contacts panel for a job. HTMX-friendly fragment.

    The `stage` query arg drives progressive disclosure of cold search:
        evaluate / prepare → cold search HIDDEN (decision time, warm only)
        sent / active      → cold search SHOWN (post-apply follow-up)
        archive            → both shown read-only
    """
    stage = request.args.get("stage", "evaluate")
    cold_unlocked = stage in ("sent", "active", "archive")
    with session_scope() as session:
        job = session.get(Job, job_id)
        if not job:
            return ("Job not found", 404)

        # Warm: people user already knows at this company
        warm = find_connections_at(session, job.company or "", limit=10)
        warm_data = [
            {
                "id": c.id,
                "name": c.name,
                "title": c.current_title or "",
                "company": c.current_company or "",
                "linkedin_url": c.linkedin_url or "",
                "connected_at": c.connected_at,
                "source": c.source,
            } for c in warm
        ]

        # Past outreach for this job (so user sees what they already sent)
        past = session.execute(
            select(OutreachAttempt)
            .where(OutreachAttempt.job_id == job_id)
            .order_by(OutreachAttempt.sent_at.desc())
        ).scalars().all()
        past_data = [
            {
                "id": p.id,
                "contact_name": p.contact_name,
                "path": p.path,
                "purpose": p.purpose,
                "sent_at": p.sent_at,
                "response_received": p.response_received,
                "response_kind": p.response_kind,
            } for p in past
        ]

        return render_template(
            "contacts/_panel.html",
            job=job,
            warm=warm_data,
            past=past_data,
            cold_loaded=False,   # cold path lazy-loads on user click
            cold_unlocked=cold_unlocked,
            stage=stage,
        )


@bp.route("/jobs/<job_id>/contacts/cold", methods=["GET"])
def cold_search(job_id: str):
    """Run the search-engine-based cold contact discovery. May take 10-30s."""
    settings = current_app.config["SETTINGS"]
    refresh = request.args.get("refresh") == "1"

    with session_scope() as session:
        job = session.get(Job, job_id)
        if not job:
            return ("Job not found", 404)

        if refresh:
            clear_cache(job.company or "")

        try:
            cold = find_contacts(
                company=job.company or "",
                job_title=job.title or "",
                max_results=5,
                use_cache=not refresh,
            )
        except Exception as e:
            log.exception("cold contact search failed")
            return render_template(
                "contacts/_cold_results.html",
                job=job, cold=[], error=str(e),
            )

        return render_template(
            "contacts/_cold_results.html",
            job=job, cold=cold, error=None,
        )


@bp.route("/jobs/<job_id>/contacts/draft", methods=["POST"])
def draft(job_id: str):
    """Generate an outreach draft for a chosen contact (warm or cold)."""
    settings = current_app.config["SETTINGS"]

    if not settings.has_llm:
        return ("没有配置 LLM_API_KEY，无法起草信。", 400)

    path = (request.form.get("path") or "cold").strip()
    if path not in ("warm", "cold"):
        return ("Bad path", 400)

    purpose = (request.form.get("purpose") or "referral_request").strip()
    medium = (request.form.get("medium") or "linkedin_dm").strip()

    contact_name = (request.form.get("contact_name") or "").strip()
    contact_title = (request.form.get("contact_title") or "").strip()
    contact_url = (request.form.get("contact_url") or "").strip()
    contact_snippet = (request.form.get("contact_snippet") or "").strip()
    connection_id = (request.form.get("connection_id") or "").strip() or None

    with session_scope() as session:
        job = session.get(Job, job_id)
        if not job:
            return ("Job not found", 404)

        # Pull user's resume summary from active facts
        identity = session.execute(
            select(IdentityVersion).where(IdentityVersion.is_current.is_(True))
        ).scalars().first()
        facts = []
        if identity:
            facts = session.execute(
                select(ResumeFact).where(
                    ResumeFact.identity_id == identity.id,
                    ResumeFact.active.is_(True),
                )
            ).scalars().all()
        resume_summary = build_resume_summary_from_facts(facts)

        # Build relationship context for warm path
        relationship_context = None
        if path == "warm" and connection_id:
            c = session.get(Connection, connection_id)
            if c:
                contact_name = contact_name or c.name
                contact_title = contact_title or (c.current_title or "")
                contact_url = contact_url or (c.linkedin_url or "")
                bits = []
                if c.connected_at:
                    bits.append(f"connected on LinkedIn {c.connected_at:%Y-%m}")
                if c.source == "linkedin_csv":
                    bits.append("from your LinkedIn export")
                if c.source == "alumni":
                    bits.append("alumni connection")
                if c.notes:
                    bits.append(c.notes[:120])
                relationship_context = "; ".join(bits) if bits else "existing LinkedIn connection"

        inp = OutreachInput(
            contact_name=contact_name,
            contact_title=contact_title,
            contact_company=job.company or "",
            contact_url=contact_url,
            contact_snippet=contact_snippet,
            job_title=job.title or "",
            job_company=job.company or "",
            job_location=job.location or "",
            job_description=(job.description or "")[:1000],
            job_link=job.link or "",
            user_resume_summary=resume_summary,
            purpose=purpose,
            relationship_context=relationship_context,
            medium=medium,
        )

        result = draft_outreach(
            inp,
            provider=settings.llm_provider,
            api_key=settings.llm_api_key,
            model=settings.llm_model,
        )

        if not result.success:
            # User-fixable failures (LLM key, network, auth) — render friendly
            # error inline so user sees what went wrong without a raw 500.
            return render_template(
                "contacts/_draft_result.html",
                job=job, draft="", error=result.error or "起草失败",
                contact_name=contact_name, contact_title=contact_title,
                contact_url=contact_url, contact_snippet=contact_snippet,
                connection_id=connection_id,
                path=path, purpose=purpose, medium=medium,
            ), 200

        return render_template(
            "contacts/_draft_result.html",
            job=job, draft=result.message,
            contact_name=contact_name,
            contact_title=contact_title,
            contact_url=contact_url,
            contact_snippet=contact_snippet,
            connection_id=connection_id,
            path=path, purpose=purpose, medium=medium,
        )


@bp.route("/jobs/<job_id>/contacts/sent", methods=["POST"])
def mark_sent(job_id: str):
    """User confirms they sent the outreach → write OutreachAttempt row."""
    path = (request.form.get("path") or "cold").strip()
    contact_name = (request.form.get("contact_name") or "").strip()
    contact_title = (request.form.get("contact_title") or "").strip()
    contact_url = (request.form.get("contact_url") or "").strip()
    purpose = (request.form.get("purpose") or "referral_request").strip()
    draft_text = (request.form.get("draft_text") or "").strip()
    connection_id = (request.form.get("connection_id") or "").strip() or None

    if not contact_name:
        return ("contact_name required", 400)

    with session_scope() as session:
        job = session.get(Job, job_id)
        if not job:
            return ("Job not found", 404)

        attempt = OutreachAttempt(
            job_id=job_id,
            connection_id=connection_id,
            contact_name=contact_name,
            contact_title=contact_title or None,
            contact_company=job.company or None,
            contact_url=contact_url or None,
            path=path,
            purpose=purpose,
            draft_text=draft_text or None,
            sent_at=datetime.now(timezone.utc),
        )
        session.add(attempt)

        # Bump the connection's last_contacted_at if warm
        if connection_id:
            c = session.get(Connection, connection_id)
            if c:
                c.last_contacted_at = datetime.now(timezone.utc)

        session.add(ReflectionEvent(
            kind="outreach_sent",
            payload_json={
                "job_id": job_id,
                "path": path,
                "purpose": purpose,
                "contact_name": contact_name,
            },
            related_job_id=job_id,
        ))
        session.flush()
        attempt_id = attempt.id

    flash(_("已记录 outreach 给 %(contact_name)s。回应时回来标记结果。", contact_name=contact_name), "ok")
    if request.headers.get("HX-Request") == "true":
        return render_template("contacts/_sent_confirmation.html",
                               attempt_id=attempt_id, contact_name=contact_name)
    return redirect(url_for("jobs.detail", job_id=job_id))


@bp.route("/outreach/<attempt_id>/mark-sent", methods=["POST"])
def mark_attempt_sent(attempt_id: str):
    """Convert an existing draft (sent_at=None) to sent (sets sent_at=now)."""
    with session_scope() as session:
        a = session.get(OutreachAttempt, attempt_id)
        if not a:
            return ("Not found", 404)
        if a.sent_at is None:
            a.sent_at = datetime.now(timezone.utc)
            # Bump the connection's last_contacted_at if linked
            if a.connection_id:
                c = session.get(Connection, a.connection_id)
                if c:
                    c.last_contacted_at = a.sent_at
            session.add(ReflectionEvent(
                kind="outreach_sent",
                payload_json={
                    "job_id": a.job_id, "attempt_id": attempt_id,
                    "contact_name": a.contact_name, "path": a.path,
                },
                related_job_id=a.job_id,
            ))
        job_id = a.job_id

    flash(_("已标记发出。回应时回来 ✓ 记结果。"), "ok")
    return redirect(request.referrer or url_for("jobs.detail", job_id=job_id))


@bp.route("/outreach/<attempt_id>/response", methods=["POST"])
def log_response(attempt_id: str):
    """User reports back: did they get a response? what kind?"""
    with session_scope() as session:
        a = session.get(OutreachAttempt, attempt_id)
        if not a:
            return ("Not found", 404)

        response_received_raw = request.form.get("response_received", "").lower()
        if response_received_raw in ("true", "1", "yes"):
            a.response_received = True
            a.response_at = datetime.now(timezone.utc)
            kind = (request.form.get("response_kind") or "").strip()
            if kind in {"accepted", "ignored", "declined", "referred", "intro_made"}:
                a.response_kind = kind
            led_to_interview = request.form.get("led_to_interview", "").lower()
            if led_to_interview in ("true", "1"):
                a.led_to_interview = True
            led_to_offer = request.form.get("led_to_offer", "").lower()
            if led_to_offer in ("true", "1"):
                a.led_to_offer = True
        elif response_received_raw in ("false", "0", "no"):
            a.response_received = False

        notes = (request.form.get("notes") or "").strip()
        if notes:
            a.notes = notes[:500]

        session.add(ReflectionEvent(
            kind="outreach_response_logged",
            payload_json={
                "attempt_id": attempt_id,
                "response_kind": a.response_kind,
                "led_to_interview": a.led_to_interview,
            },
            related_job_id=a.job_id,
        ))

    flash(_("已记录回应。"), "ok")
    return redirect(url_for("jobs.detail", job_id=a.job_id))
