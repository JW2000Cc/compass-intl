"""Identity routes — upload resume, view facts, edit/deactivate facts."""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from flask_babel import gettext as _
from sqlalchemy import select
from werkzeug.utils import secure_filename

from ..extensions import session_scope
from ..models import (
    FactRevision,
    InterviewQA,
    InterviewSession,
    IdentityVersion,
    ReflectionEvent,
    ResumeFact,
    RewriteAttempt,
    Job,
)

ALLOWED_RESUME_EXT = {".pdf", ".docx", ".doc", ".txt", ".md"}
from ..services.identity_engine import (
    extract_structured,
    get_current_identity,
    install_identity,
    parse_resume,
)
from ..services.interview_engine import (
    generate_questions,
    get_or_create_session,
    record_answer,
    start_session,
    synthesize_facts,
)
from ..services.llm import LLMError, LLMConfigError

log = logging.getLogger(__name__)

bp = Blueprint("identity", __name__, url_prefix="/identity")


@bp.route("/")
def overview():
    with session_scope() as session:
        identity = get_current_identity(session)
        facts = []
        if identity:
            facts = (
                session.execute(
                    select(ResumeFact)
                    .where(ResumeFact.identity_id == identity.id, ResumeFact.active.is_(True))
                    .order_by(ResumeFact.kind, ResumeFact.created_at)
                )
                .scalars()
                .all()
            )

        # Group facts by kind
        groups: dict[str, list[ResumeFact]] = {}
        for f in facts:
            groups.setdefault(f.kind, []).append(f)

        return render_template(
            "identity/overview.html",
            identity=identity,
            groups=groups,
        )


@bp.route("/upload", methods=["GET", "POST"])
def upload():
    if request.method == "GET":
        return render_template("identity/upload.html")

    settings = current_app.config["SETTINGS"]
    file = request.files.get("file")
    if not file or not file.filename:
        flash(_("请选择简历文件。"), "error")
        return redirect(url_for("identity.upload"))

    # Validate extension against whitelist (defends against accidental .exe etc.)
    raw_suffix = Path(file.filename).suffix.lower()
    if raw_suffix not in ALLOWED_RESUME_EXT:
        suffix_display = raw_suffix or _("(无扩展名)")
        flash(_("不支持的文件类型: %(suffix)s。请用 PDF / DOCX / TXT。", suffix=suffix_display), "error")
        return redirect(url_for("identity.upload"))

    upload_dir = settings.data_dir / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)

    # Use secure_filename + uuid suffix to defeat collisions and path traversal.
    safe_stem = secure_filename(Path(file.filename).stem) or "resume"
    unique_id = uuid.uuid4().hex[:8]
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    dest = upload_dir / f"{timestamp}-{unique_id}-{safe_stem}{raw_suffix}"
    file.save(str(dest))

    raw_text = parse_resume(dest)
    if not raw_text:
        flash(_("无法解析简历。请尝试 PDF 或 DOCX。"), "error")
        return redirect(url_for("identity.upload"))

    upload_errors: list[str] = []
    try:
        parsed_json = extract_structured(
            raw_text,
            provider=settings.llm_provider,
            api_key=settings.llm_api_key,
            model=settings.llm_model,
            errors=upload_errors,
        )
    except LLMError as exc:
        from ..services.llm_diagnostics import record
        record(upload_errors, "identity.extract_structured", exc,
               context="路由层捕获")
        parsed_json = {}

    with session_scope() as session:
        iv = install_identity(
            session,
            raw_text=raw_text,
            parsed_json=parsed_json,
            label=Path(file.filename).stem,
            source_filename=file.filename,
        )
        # Y2 (5-08): surface silent LLM degradation; without this user sees
        # "已建立新身份版本" but parsed_json is {} so downstream pipelines
        # have nothing to work with.
        if upload_errors:
            from ..services.llm_diagnostics import summarize_for_flash
            flash(
                f"已建立新身份版本 {iv.id} ({iv.language})。 "
                + summarize_for_flash(upload_errors),
                "warn",
            )
        else:
            flash(_("已建立新身份版本 %(iv_id)s (%(iv_language)s)。", iv_id=iv.id, iv_language=iv.language), "ok")

    return redirect(url_for("identity.overview"))


@bp.route("/fact/<fact_id>/deactivate", methods=["POST"])
def deactivate_fact(fact_id: str):
    with session_scope() as session:
        f = session.get(ResumeFact, fact_id)
        if not f:
            flash(_("Fact not found."), "error")
            return redirect(url_for("identity.overview"))
        f.active = False
        session.add(
            ReflectionEvent(
                kind="fact_deactivated",
                payload_json={"fact_id": fact_id, "text": f.text[:200]},
                related_fact_id=fact_id,
            )
        )
    flash(_("已停用该 fact（不会出现在新生成的简历里）。"), "ok")
    return redirect(url_for("identity.overview"))


# Sensitive-category whitelist — must match what claim_grounder treats as L5 hard-ban
_VALID_SENSITIVE = {
    "", "education_credential", "language_level", "employment_dates",
    "health_status", "legal_status",
}


@bp.route("/fact/<fact_id>/edit", methods=["POST"])
def edit_fact(fact_id: str):
    """Edit a fact's text + metadata. Facts are designed immutable; this
    endpoint exists for two pragmatic uses:
      - correcting LLM extraction errors (typo, mis-classification)
      - setting/clearing sensitive_category and user_verified flags

    Original text is preserved in structured_json.original_text on first
    correction, so the audit trail survives. Every edit logs a fact_corrected
    ReflectionEvent so the immutability story can be reconstructed via replay.
    """
    is_htmx = request.headers.get("HX-Request") == "true"
    new_text = (request.form.get("text") or "").strip()
    new_sensitive = (request.form.get("sensitive_category") or "").strip()
    new_user_verified = request.form.get("user_verified") == "1"
    reason = (request.form.get("reason") or "").strip()

    if not new_text:
        if is_htmx:
            return ("文本不能为空", 400)
        flash(_("文本不能为空。"), "error")
        return redirect(url_for("identity.overview"))

    if new_sensitive not in _VALID_SENSITIVE:
        if is_htmx:
            return (f"未知 sensitive_category: {new_sensitive}", 400)
        flash(_("未知 sensitive_category: %(new_sensitive)s", new_sensitive=new_sensitive), "error")
        return redirect(url_for("identity.overview"))

    with session_scope() as session:
        f = session.get(ResumeFact, fact_id)
        if not f:
            if is_htmx:
                return ("", 404)
            flash(_("Fact not found."), "error")
            return redirect(url_for("identity.overview"))

        old_text = f.text
        old_sensitive = f.sensitive_category
        old_verified = f.user_verified

        text_changed = old_text != new_text
        if text_changed:
            sj = dict(f.structured_json or {})
            # Preserve the very first original (don't overwrite on multiple edits)
            sj.setdefault("original_text", old_text)
            sj.setdefault("first_corrected_at", datetime.now(timezone.utc).isoformat())
            f.structured_json = sj
            # Append manual_edit revision (also updates f.text via the helper)
            from ..services.fact_history import write_revision
            write_revision(
                session, fact=f, new_text=new_text,
                kind="manual_edit",
                note=(reason or None),
            )

        f.sensitive_category = new_sensitive or None
        f.user_verified = new_user_verified

        session.add(
            ReflectionEvent(
                kind="fact_corrected",
                payload_json={
                    "fact_id": fact_id,
                    "text_changed": text_changed,
                    "old_text": old_text if text_changed else None,
                    "new_text": new_text if text_changed else None,
                    "old_sensitive": old_sensitive,
                    "new_sensitive": new_sensitive or None,
                    "old_verified": old_verified,
                    "new_verified": new_user_verified,
                    "reason": reason or None,
                },
                related_fact_id=fact_id,
            )
        )

        if is_htmx:
            session.flush()
            return render_template("identity/_fact_card.html", f=f)

    flash(_("已更新 fact（原文保留在审计记录）。"), "ok")
    # If user came from this fact's history page, stay there so they can see
    # the new revision in the timeline. Otherwise back to overview.
    next_url = (request.form.get("next") or "").strip()
    if next_url and next_url.startswith("/"):
        return redirect(next_url)
    return redirect(url_for("identity.overview"))


@bp.route("/fact/<fact_id>/interview", methods=["POST"])
def start_interview(fact_id: str):
    settings = current_app.config["SETTINGS"]
    if not settings.has_llm:
        flash(_("没有配置 LLM_API_KEY。"), "error")
        return redirect(url_for("identity.overview"))

    with session_scope() as session:
        f = session.get(ResumeFact, fact_id)
        if not f:
            flash(_("Fact not found."), "error")
            return redirect(url_for("identity.overview"))
        try:
            qs = generate_questions(
                f, provider=settings.llm_provider, api_key=settings.llm_api_key, model=settings.llm_model
            )
        except LLMError as exc:
            flash(_("无法生成问题: %(exc)s", exc=exc), "error")
            return redirect(url_for("identity.overview"))
        if not qs:
            flash(_("LLM 未返回有效问题，重试或更换 fact。"), "error")
            return redirect(url_for("identity.overview"))
        isess = start_session(session, fact=f, questions=qs)
        return redirect(url_for("identity.interview_session", session_id=isess.id))


@bp.route("/interview/<session_id>", methods=["GET", "POST"])
def interview_session(session_id: str):
    settings = current_app.config["SETTINGS"]
    if request.method == "POST":
        # Save all answers from the form
        with session_scope() as session:
            isess = session.get(InterviewSession, session_id)
            if not isess:
                flash(_("Session not found."), "error")
                return redirect(url_for("identity.overview"))
            qas = (
                session.execute(
                    select(InterviewQA).where(InterviewQA.session_id == session_id).order_by(InterviewQA.id)
                )
                .scalars()
                .all()
            )
            for qa in qas:
                ans = request.form.get(f"answer_{qa.id}", "").strip()
                record_answer(session, qa.id, ans or None)

            # If "synthesize" submitted, run fact synthesis
            if request.form.get("action") == "synthesize":
                fact = session.get(ResumeFact, isess.target_id)
                try:
                    new_facts = synthesize_facts(
                        session,
                        isess=isess,
                        parent_fact=fact,
                        provider=settings.llm_provider,
                        api_key=settings.llm_api_key,
                        model=settings.llm_model,
                    )
                    flash(_("采访合成完成，新增 %(n)s 条 fact。", n=len(new_facts)), "ok")
                except LLMError as exc:
                    flash(_("合成失败: %(exc)s", exc=exc), "error")
                return redirect(url_for("identity.overview"))

        flash(_("已保存当前回答。点击 '完成访谈' 触发合成。"), "ok")
        return redirect(url_for("identity.interview_session", session_id=session_id))

    with session_scope() as session:
        isess = session.get(InterviewSession, session_id)
        if not isess:
            flash(_("Session not found."), "error")
            return redirect(url_for("identity.overview"))
        qas = (
            session.execute(
                select(InterviewQA).where(InterviewQA.session_id == session_id).order_by(InterviewQA.id)
            )
            .scalars()
            .all()
        )
        parent_fact = session.get(ResumeFact, isess.target_id)
        return render_template(
            "identity/interview.html",
            session_obj=isess,
            qas=qas,
            parent_fact=parent_fact,
        )


# ─────────────────────────────────────────────────────────
# Fact-level revision history (per user thread on append-only design)
# ─────────────────────────────────────────────────────────


@bp.route("/fact/<fact_id>/history")
def fact_history(fact_id: str):
    """Per-fact revision timeline. Append-only; rollback creates a new revision."""
    from ..services.fact_history import list_revisions

    with session_scope() as session:
        fact = session.get(ResumeFact, fact_id)
        if fact is None:
            flash(_("Fact not found."), "error")
            return redirect(url_for("identity.overview"))
        revisions = list_revisions(session, fact_id)

        # Resolve source links: source_job_id → job title; source_attempt_id → exists
        job_titles: dict[str, str] = {}
        attempt_ids: set[str] = set()
        for r in revisions:
            if r.source_job_id and r.source_job_id not in job_titles:
                j = session.get(Job, r.source_job_id)
                if j:
                    job_titles[r.source_job_id] = (j.title or "?") + (
                        f" @ {j.company}" if j.company else ""
                    )
            if r.source_attempt_id:
                attempt_ids.add(r.source_attempt_id)

        return render_template(
            "identity/fact_history.html",
            fact=fact,
            revisions=revisions,
            job_titles=job_titles,
        )


@bp.route("/fact/<fact_id>/restore/<rev_id>", methods=["POST"])
def restore_revision(fact_id: str, rev_id: str):
    """↺ Set an old revision as current. Writes a NEW `rolled_back` revision —
    target row is never deleted (per append-only design — see thread)."""
    from ..services.fact_history import rollback_to

    with session_scope() as session:
        fact = session.get(ResumeFact, fact_id)
        if fact is None:
            flash(_("Fact not found."), "error")
            return redirect(url_for("identity.overview"))
        try:
            rev = rollback_to(session, fact=fact, target_revision_id=rev_id)
        except ValueError as exc:
            flash(_("回滚失败：%(exc)s", exc=exc), "error")
            return redirect(url_for("identity.fact_history", fact_id=fact_id))
        if rev is None:
            flash(_("当前文本已等于该版本，无需回滚。"), "warn")
        else:
            flash(_("↺ 已回滚到该版本（新建了一条 rolled_back 修订）。"), "ok")
    return redirect(url_for("identity.fact_history", fact_id=fact_id))


@bp.route("/sediments")
def sediments_overview():
    """All promote events grouped by attempt — the 沉淀历史 tab.

    GitHub analogy: this is the "log of merges". Each row = one click on
    📌 沉淀, expand to see which facts were affected.
    """
    with session_scope() as session:
        # All promoted_from_attempt revisions, newest first.
        revs = list(
            session.execute(
                select(FactRevision)
                .where(FactRevision.kind == "promoted_from_attempt")
                .order_by(FactRevision.created_at.desc())
            ).scalars().all()
        )

        # Pre-fetch all referenced facts and jobs in TWO queries instead of N
        # (avoids N+1 — was: session.get per item).
        fact_ids = {r.fact_id for r in revs if r.fact_id}
        job_ids = {r.source_job_id for r in revs if r.source_job_id}
        facts_by_id: dict[str, ResumeFact] = {}
        jobs_by_id: dict[str, Job] = {}
        if fact_ids:
            for f in session.execute(
                select(ResumeFact).where(ResumeFact.id.in_(list(fact_ids)))
            ).scalars().all():
                facts_by_id[f.id] = f
        if job_ids:
            for j in session.execute(
                select(Job).where(Job.id.in_(list(job_ids)))
            ).scalars().all():
                jobs_by_id[j.id] = j

        # Group by source_attempt_id (one promote click = many revisions from
        # one attempt). Preserve order via dict-insertion.
        groups: dict[str, dict] = {}
        for r in revs:
            key = r.source_attempt_id or "unknown"
            g = groups.setdefault(key, {
                "attempt_id": r.source_attempt_id,
                "job_id": r.source_job_id,
                "created_at": r.created_at,
                "items": [],
            })
            if r.created_at > g["created_at"]:
                g["created_at"] = r.created_at
            g["items"].append({"rev": r, "fact": facts_by_id.get(r.fact_id)})

        # Resolve job titles
        for g in groups.values():
            if g["job_id"] and g["job_id"] in jobs_by_id:
                j = jobs_by_id[g["job_id"]]
                g["job_label"] = (
                    f"{j.title or '?'} @ {j.company}" if j.company else (j.title or "?")
                )
            else:
                g["job_label"] = "?"

        ordered = sorted(groups.values(), key=lambda g: g["created_at"], reverse=True)
        return render_template("identity/sediments.html", groups=ordered)
