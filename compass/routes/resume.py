"""Resume workbench routes — pick a job, run pipeline, review variants."""
from __future__ import annotations

import logging

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from flask_babel import gettext as _
from sqlalchemy import select

from ..extensions import session_scope
from ..models import (
    FactVariant,
    Job,
    ReflectionEvent,
    ResumeFact,
    RewriteAttempt,
)
from ..services.identity_engine import get_current_identity
from ..services.llm import LLMError, LLMConfigError
from ..services.resume_writer import run_pipeline, regenerate_single_variant

log = logging.getLogger(__name__)

bp = Blueprint("resume", __name__, url_prefix="/resume")


@bp.route("/")
def workbench():
    with session_scope() as session:
        identity = get_current_identity(session)
        # All attempts, ordered by creation desc — we group by job below
        attempts = (
            session.execute(
                select(RewriteAttempt).order_by(RewriteAttempt.created_at.desc())
            )
            .scalars()
            .all()
        )

        # Group attempts by job, preserving "most-recent-job-first" order:
        # the order of jobs is determined by latest attempt per job (desc).
        from collections import OrderedDict
        groups: "OrderedDict[str, dict]" = OrderedDict()
        for a in attempts:
            job_id = a.job_id
            if job_id not in groups:
                job = session.get(Job, job_id)
                groups[job_id] = {
                    "job": job,
                    "attempts": [],
                    "latest_at": a.created_at,
                    "best_score": 0,
                }
            groups[job_id]["attempts"].append(a)
            if a.judge_score and a.judge_score > groups[job_id]["best_score"]:
                groups[job_id]["best_score"] = a.judge_score

        # Available jobs to write a resume for (Tier 1-3 ish)
        jobs = (
            session.execute(
                select(Job)
                .where(Job.deleted.is_(False))
                .order_by(Job.created_at.desc())
                .limit(20)
            )
            .scalars()
            .all()
        )
        return render_template(
            "resume/workbench.html",
            identity=identity,
            attempt_groups=list(groups.values()),
            total_attempts=len(attempts),
            jobs=jobs,
        )


@bp.route("/run/<job_id>", methods=["POST"])
def run(job_id: str):
    settings = current_app.config["SETTINGS"]
    if not settings.has_llm:
        flash(_("没有配置 LLM_API_KEY，无法运行简历改写。"), "error")
        return redirect(url_for("resume.workbench"))

    try:
        with session_scope() as session:
            job = session.get(Job, job_id)
            if not job:
                flash(_("Job not found."), "error")
                return redirect(url_for("resume.workbench"))
            # Count prior decisions before pipeline (so we can tell the user
            # what was preserved vs newly generated).
            prior = session.execute(
                select(FactVariant.user_decision)
                .where(FactVariant.job_id == job_id)
            ).scalars().all()
            n_prior_kept = sum(1 for d in prior if d in ("approved", "edited_by_user"))

            # Wire step_overrides from the saved LLMSettings overlay so per-step
            # model choices in the UI actually take effect at LLM call time.
            _overlay = getattr(settings, "_llm_overlay", None)
            _step_overrides = getattr(_overlay, "step_overrides", None) if _overlay else None
            attempt = run_pipeline(
                session,
                job=job,
                provider=settings.llm_provider,
                api_key=settings.llm_api_key,
                model=settings.llm_model,
                adversarial_enabled=settings.adversarial_voice_enabled,
                step_overrides=_step_overrides,
            )
            attempt_id = attempt.id
        # Y4 (5-08): if any pipeline step silently fell back (LLM 401/timeout),
        # attempt.judge_report["pipeline_warnings"] has the entries. Surface
        # them as a warn-level flash instead of pretending "成功 · composite 0/10".
        from ..services.llm_diagnostics import summarize_for_flash
        warnings = (attempt.judge_report or {}).get("pipeline_warnings", []) or []
        base_msg = f"流水线完成 · composite {attempt.judge_score}/10"
        if n_prior_kept:
            base_msg += f" · 保留了 {n_prior_kept} 条 ✓/✎ 已决定的，其余重新生成了建议。"
        else:
            base_msg += " · 在下方逐 bullet 审核。"
        if warnings:
            flash(base_msg + " " + summarize_for_flash(warnings), "warn")
        else:
            flash(base_msg, "ok")
        # Redirect to job detail page step 2 — review embedded inline
        return redirect(url_for("jobs.detail", job_id=job_id) + "#step-resume_rewrite")
    except RuntimeError as exc:
        flash(str(exc), "error")
        return redirect(url_for("jobs.detail", job_id=job_id))
    except LLMError as exc:
        flash(_("LLM 错误: %(exc)s", exc=exc), "error")
        return redirect(url_for("jobs.detail", job_id=job_id))


@bp.route("/review/<attempt_id>")
def review(attempt_id: str):
    with session_scope() as session:
        attempt = session.get(RewriteAttempt, attempt_id)
        if not attempt:
            flash(_("Attempt not found."), "error")
            return redirect(url_for("resume.workbench"))
        job = session.get(Job, attempt.job_id)
        variants = (
            session.execute(
                select(FactVariant)
                .where(FactVariant.job_id == attempt.job_id)
                .order_by(FactVariant.created_at.desc())
            )
            .scalars()
            .all()
        )
        # Dedup: keep latest variant per fact (older versions live in DB as
        # audit trail; UI shows current state only).
        # ADR-0015: each row also gets a UserFacingMenu when decomposition_json
        # is set, rendered by templates/resume/_decomposition_panel.html.
        # Locale follows the user's resume language so dimension labels and
        # action prompts render in the resume's native tongue rather than
        # always-zh. Fallback "zh" preserves existing behavior pre-identity.
        from ..services.review_rows import build_review_rows

        identity = get_current_identity(session)
        locale = (identity.language if identity and identity.language else "zh")
        rows = build_review_rows(session, list(variants), locale=locale)

        # User themes for the preview dropdown — only user-uploaded; builtins are hardcoded in template
        from ..services.themes import list_all_themes
        all_themes = list_all_themes()
        user_themes = [t for t in all_themes if t["kind"] == "user"]

        return render_template(
            "resume/review.html",
            attempt=attempt,
            job=job,
            rows=rows,
            user_themes=user_themes,
        )


@bp.route("/variant/<variant_id>/decide", methods=["POST"])
def decide_variant(variant_id: str):
    decision = request.form.get("decision", "")
    valid = {"approved", "rejected", "edited_by_user", "pending"}
    if decision not in valid:
        flash(_("无效决定。"), "error")
        return redirect(url_for("resume.workbench"))

    edited_text = (request.form.get("edited_text") or "").strip()
    rejection_reason = (request.form.get("rejection_reason") or "").strip()

    with session_scope() as session:
        v = session.get(FactVariant, variant_id)
        if not v:
            flash(_("Variant not found."), "error")
            return redirect(url_for("resume.workbench"))
        v.user_decision = decision
        if decision == "edited_by_user" and edited_text:
            v.text = edited_text[:1500]
        if decision == "rejected":
            v.rejection_reason = rejection_reason or None

        session.add(
            ReflectionEvent(
                kind=f"variant_{decision}",
                payload_json={
                    "variant_id": variant_id,
                    "fact_id": v.fact_id,
                    "level": v.amplification_level,
                    "rejection_reason": v.rejection_reason,
                },
                related_fact_id=v.fact_id,
                related_job_id=v.job_id,
            )
        )
    # Specific flash per decision so the user actually understands what happened.
    msg = {
        "approved": "✓ 已接受这条改写",
        "rejected": "✗ 已拒绝（再跑一次时会作为反向提示）",
        "edited_by_user": "✎ 编辑已保存到本次改写",
        "pending": "↺ 已撤销决定",
    }.get(decision, f"已记录: {decision}")
    flash(msg, "ok")
    # Prefer explicit `next` from form (preserves anchor like #sug-xxx);
    # fall back to referrer; last resort workbench.
    next_url = (request.form.get("next") or "").strip()
    if next_url and next_url.startswith("/"):
        return redirect(next_url)
    return redirect(request.referrer or url_for("resume.workbench"))


@bp.route("/attempt/<attempt_id>/accept_all", methods=["POST"])
def accept_all_pending(attempt_id: str):
    """Bulk-approve all pending variants in this attempt."""
    with session_scope() as session:
        attempt = session.get(RewriteAttempt, attempt_id)
        if not attempt:
            flash(_("Attempt not found."), "error")
            return redirect(url_for("resume.workbench"))
        variants = (
            session.execute(
                select(FactVariant).where(
                    FactVariant.job_id == attempt.job_id,
                    FactVariant.user_decision == "pending",
                )
            )
            .scalars()
            .all()
        )
        n = 0
        for v in variants:
            v.user_decision = "approved"
            session.add(
                ReflectionEvent(
                    kind="variant_approved",
                    payload_json={"variant_id": v.id, "fact_id": v.fact_id, "bulk": True},
                    related_fact_id=v.fact_id,
                    related_job_id=v.job_id,
                )
            )
            n += 1
    flash(_("已一键接受 %(n)s 条变换。", n=n), "ok")
    next_url = (request.form.get("next") or "").strip()
    if next_url and next_url.startswith("/"):
        return redirect(next_url)
    return redirect(request.referrer or url_for("resume.workbench"))


@bp.route("/variant/<variant_id>/regenerate", methods=["POST"])
def regenerate_variant(variant_id: str):
    """Re-roll a single variant (per-card ↻ button).

    Reuses parent attempt's keywords + injects this fact's rejection history
    as negative examples. Updates variant in place (preserves variant.id so
    URL anchors stay valid).
    """
    settings = current_app.config["SETTINGS"]
    if not settings.has_llm:
        flash(_("没有配置 LLM_API_KEY，无法重生成变换。"), "error")
        return redirect(request.referrer or url_for("resume.workbench"))

    try:
        with session_scope() as session:
            v = session.get(FactVariant, variant_id)
            if v is None:
                flash(_("Variant not found."), "error")
                return redirect(url_for("resume.workbench"))
            if v.user_decision in ("approved", "edited_by_user"):
                flash(_("这条已批准/编辑过，重生会丢掉你的决定。先撤销再重生。"), "warn")
                return redirect(request.referrer or url_for("resume.workbench"))
            job = session.get(Job, v.job_id)
            if job is None:
                flash(_("Job not found."), "error")
                return redirect(url_for("resume.workbench"))
            _overlay = getattr(settings, "_llm_overlay", None)
            _step_overrides = getattr(_overlay, "step_overrides", None) if _overlay else None
            regenerate_single_variant(
                session, variant=v, job=job,
                provider=settings.llm_provider,
                api_key=settings.llm_api_key,
                model=settings.llm_model,
                adversarial_enabled=settings.adversarial_voice_enabled,
                step_overrides=_step_overrides,
            )
            session.add(
                ReflectionEvent(
                    kind="variant_regenerated",
                    payload_json={"variant_id": v.id, "fact_id": v.fact_id},
                    related_fact_id=v.fact_id,
                    related_job_id=v.job_id,
                )
            )
        flash(_("↻ 已重新生成 — 这条建议变成新版本。"), "ok")
    except RuntimeError as exc:
        flash(_("重生失败: %(exc)s", exc=exc), "error")
    except LLMError as exc:
        flash(_("LLM 错误: %(exc)s", exc=exc), "error")

    next_url = (request.form.get("next") or "").strip()
    if next_url and next_url.startswith("/"):
        return redirect(next_url)
    return redirect(request.referrer or url_for("resume.workbench"))


@bp.route("/attempt/<attempt_id>/promote", methods=["POST"])
def promote_attempt(attempt_id: str):
    """📌 Promote: ✓/✎ variants in this attempt → append FactRevision rows.

    Each fact gets a `promoted_from_attempt` revision. The fact's current text
    is updated to the latest revision. History is preserved — see fact_history
    service. Rollback is per-fact (✗ not at attempt level — see threadon
    rebase-style holes).
    """
    from ..services.fact_history import write_revision

    with session_scope() as session:
        attempt = session.get(RewriteAttempt, attempt_id)
        if attempt is None:
            flash(_("Attempt not found."), "error")
            return redirect(url_for("resume.workbench"))

        # All decided variants on this job with their LATEST decision win.
        # We look at the attempt's job, not just attempt_id, so prior approved
        # variants (which may not have been "owned" by this attempt) also count.
        decided = (
            session.execute(
                select(FactVariant)
                .where(
                    FactVariant.job_id == attempt.job_id,
                    FactVariant.user_decision.in_(["approved", "edited_by_user"]),
                )
                .order_by(FactVariant.created_at.desc())
            )
            .scalars()
            .all()
        )

        # Latest decision per fact wins
        seen: set[str] = set()
        promoted = 0
        for v in decided:
            if v.fact_id in seen:
                continue
            seen.add(v.fact_id)
            fact = session.get(ResumeFact, v.fact_id)
            if fact is None:
                continue
            rev = write_revision(
                session,
                fact=fact,
                new_text=v.text,
                kind="promoted_from_attempt",
                source_attempt_id=attempt.id,
                source_job_id=attempt.job_id,
                source_variant_id=v.id,
                note=f"promoted from variant {v.id} (attempt {attempt.id[:8]})",
            )
            if rev is None:
                continue  # text unchanged; not a real promotion
            promoted += 1
            # Keep ReflectionEvent for back-compat with reflection dashboard
            session.add(
                ReflectionEvent(
                    kind="fact_promoted_from_variant",
                    payload_json={
                        "fact_id": fact.id,
                        "variant_id": v.id,
                        "attempt_id": attempt.id,
                        "revision_id": rev.id,
                    },
                    related_fact_id=fact.id,
                    related_job_id=attempt.job_id,
                )
            )

    if promoted:
        flash(
            f"📌 已沉淀 {promoted} 条到我的简历事实 — 看左侧栏「📌 沉淀历史」"
            f"查看变更详情、单条 fact 演变、或回滚。",
            "ok",
        )
    else:
        flash(_("没有新的变更可沉淀（所有 ✓/✎ 与当前 fact 文本一致）。"), "warn")
    next_url = (request.form.get("next") or "").strip()
    if next_url and next_url.startswith("/"):
        return redirect(next_url)
    return redirect(request.referrer or url_for("resume.workbench"))
