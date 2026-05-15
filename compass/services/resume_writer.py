"""Resume writer — multi-step pipeline.

  Step 1  extract_jd_keywords        LLM extracts target keyword list from JD
  Step 2  match_facts_to_keywords    deterministic + LLM hybrid: which facts
                                     could legitimately address each keyword
  Step 3  rewrite_per_bullet         per fact, generate a tailored variant
                                     (target keyword in mind, not new claims)
  Step 4  ground_each_variant        run claim_grounder on every variant
  Step 5  adversarial_screen         run adversarial_voice on every variant
  Step 6  judge_three_roles          ATS / HR / Hiring Manager triple-judge

The pipeline outputs a RewriteAttempt + many FactVariant rows. The user
reviews each variant in the UI (approve / reject / edit), and approved
variants are assembled into the final tailored resume.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    FactVariant,
    Job,
    ReflectionEvent,
    ResumeFact,
    RewriteAttempt,
    UserCalibration,
)
from .adversarial_voice import raise_objection
from .claim_grounder import (
    ClaimGroundingError,
    GroundingReport,
    ground_variant,
    save_variant,
)
from .identity_engine import get_current_identity
from .llm import LLMConfigError, LLMError, chat

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────
# Helpers — inheritance from prior attempts on same job
# ─────────────────────────────────────────────────────────


def _build_prior_decisions(session: Session, job_id: str) -> dict[str, FactVariant]:
    """Map fact_id → most recent FactVariant for this job.

    Used by run_pipeline to skip regenerating facts the user already
    approved/edited (so 「再跑一次」 doesn't destroy their work).
    """
    rows = (
        session.execute(
            select(FactVariant)
            .where(FactVariant.job_id == job_id)
            .order_by(FactVariant.created_at.desc())
        )
        .scalars()
        .all()
    )
    out: dict[str, FactVariant] = {}
    for v in rows:
        if v.fact_id not in out:  # first hit = most recent
            out[v.fact_id] = v
    return out


def _build_rejected_history(
    session: Session, job_id: str
) -> dict[str, list[tuple[str, Optional[str]]]]:
    """Map fact_id → list of (text, reason) for prior rejected variants.

    Used as negative examples so re-runs produce different output, not the
    same LLM garbage with different temperature.
    """
    rows = (
        session.execute(
            select(FactVariant)
            .where(FactVariant.job_id == job_id, FactVariant.user_decision == "rejected")
            .order_by(FactVariant.created_at.desc())
        )
        .scalars()
        .all()
    )
    out: dict[str, list[tuple[str, Optional[str]]]] = {}
    for v in rows:
        out.setdefault(v.fact_id, []).append((v.text, v.rejection_reason))
    return out


# ─────────────────────────────────────────────────────────
# Step 1 — JD keyword extraction
# ─────────────────────────────────────────────────────────


KEYWORD_SYSTEM = """Extract 8-12 keywords from this job description that the
candidate's resume should cover for ATS and recruiter screening. Prioritize
hard skills, methodology, domain terminology. AVOID generic words ("teamwork",
"communication") unless they are uniquely emphasized in the JD.

Output ONLY a JSON array of strings:
["keyword 1", "keyword 2", ...]"""


def extract_jd_keywords(
    job: Job, *, provider: str, api_key: str, model: str,
    errors: list[str] | None = None,
) -> list[str]:
    if not api_key:
        from .llm_diagnostics import record
        record(errors, "extract_jd_keywords", RuntimeError("API key 缺失"),
               context="step 1 跳过；用户得到 0 个关键词")
        return []
    payload = f"## JD\nTitle: {job.title}\nCompany: {job.company}\n\n{(job.description or '')[:3000]}"
    try:
        resp = chat(
            provider=provider,
            api_key=api_key,
            model=model,
            system=KEYWORD_SYSTEM,
            user_content=payload,
            max_tokens=400,
        )
    except LLMError as exc:
        from .llm_diagnostics import record
        record(errors, "extract_jd_keywords", exc, context="step 1 LLM 失败")
        return []
    parsed = resp.parse_json(default=[])
    return [str(k).strip() for k in parsed if isinstance(k, str) and k.strip()][:12]


# ─────────────────────────────────────────────────────────
# Step 2 — match facts to keywords
# ─────────────────────────────────────────────────────────


@dataclass
class FactKeywordPairing:
    fact: ResumeFact
    matched_keywords: list[str] = field(default_factory=list)


MATCH_SYSTEM = """For each FACT, decide which KEYWORDS it can legitimately
address WITHOUT fabrication. A keyword "matches" a fact if the fact already
contains evidence supporting it OR closely-related evidence that justifies
re-framing.

Output ONLY JSON, mapping fact index → list of matched keyword indices:
{"0": [2, 5], "1": [], "2": [1, 3, 7]}

Be conservative. If you're not sure, don't match. The grounder will reject
overreaches downstream anyway, but unnecessary matches waste cycles."""


def match_facts_to_keywords(
    facts: list[ResumeFact],
    keywords: list[str],
    *,
    provider: str,
    api_key: str,
    model: str,
    errors: list[str] | None = None,
) -> list[FactKeywordPairing]:
    pairings = [FactKeywordPairing(fact=f) for f in facts]
    if not facts or not keywords or not api_key:
        if not api_key and (facts and keywords):
            from .llm_diagnostics import record
            record(errors, "match_facts_to_keywords",
                   RuntimeError("API key 缺失"),
                   context="step 2 跳过；fact↔keyword 匹配丢失")
        return pairings
    fact_block = "\n".join(f"[{i}] {f.text[:200]}" for i, f in enumerate(facts))
    kw_block = "\n".join(f"[{i}] {k}" for i, k in enumerate(keywords))
    payload = f"## Facts\n{fact_block}\n\n## Keywords\n{kw_block}"
    try:
        resp = chat(
            provider=provider,
            api_key=api_key,
            model=model,
            system=MATCH_SYSTEM,
            user_content=payload,
            max_tokens=600,
        )
    except LLMError as exc:
        from .llm_diagnostics import record
        record(errors, "match_facts_to_keywords", exc, context="step 2 LLM 失败")
        return pairings
    parsed = resp.parse_json(default={})
    if not isinstance(parsed, dict):
        return pairings
    for k, v in parsed.items():
        try:
            fi = int(k)
        except (ValueError, TypeError):
            continue
        if 0 <= fi < len(pairings) and isinstance(v, list):
            pairings[fi].matched_keywords = [
                keywords[int(i)] for i in v if isinstance(i, int) and 0 <= int(i) < len(keywords)
            ]
    return pairings


# ─────────────────────────────────────────────────────────
# Step 3 — rewrite per bullet
# ─────────────────────────────────────────────────────────


REWRITE_SYSTEM = """Rewrite this resume fact for the target JD.

NON-NEGOTIABLE rules:
  - DO NOT add facts not in the original.
  - DO NOT change degrees, dates, language levels, or formal titles.
  - DO use professional industry vocabulary FROM THE JD when truthful.
  - DO surface implicit information already in the original.
  - Keep it ≤ 35 words.

Target keywords to embed (only if truthful): {keywords}

Output ONLY the rewritten text, nothing else (no quotes, no commentary)."""


def rewrite_one(
    fact: ResumeFact,
    matched_keywords: list[str],
    *,
    provider: str,
    api_key: str,
    model: str,
    negative_examples: Optional[list[tuple[str, Optional[str]]]] = None,
    errors: list[str] | None = None,
) -> str:
    """Generate a tailored variant of `fact` for the JD's matched keywords.

    `negative_examples`: list of (rejected_text, reason_or_None) from prior
    user-rejected variants on the same fact. Fed into the prompt as "do not
    produce something similar to:" so re-runs actually differ.
    """
    if not api_key:
        return fact.text
    if not matched_keywords:
        return fact.text  # no rewrite needed
    sys = REWRITE_SYSTEM.replace("{keywords}", ", ".join(matched_keywords))

    # Inject resume preference brief — accumulated user feedback on past rewrites
    # (e.g. "bullets too long", "avoid leadership claims", "more quantification").
    # Empty if user hasn't given any resume feedback yet.
    from .preference_brief import load_resume_brief
    resume_brief = load_resume_brief()
    brief_block = resume_brief.format_for_prompt()
    if brief_block:
        sys += "\n\n" + brief_block

    # Append negative examples from this user's prior rejections on this fact —
    # tells the LLM "do NOT regenerate something similar". This is what makes
    # 「再跑一次」 actually produce different output instead of LLM-temperature roulette.
    if negative_examples:
        neg_block = "\n\nThe user has previously REJECTED these variants for this same fact. Do NOT produce anything similar to them; choose a different angle or framing."
        for i, (rej_text, rej_reason) in enumerate(negative_examples[:5], 1):
            neg_block += f"\n  {i}. {rej_text!r}"
            if rej_reason:
                neg_block += f"  (reason: {rej_reason})"
        sys += neg_block

    payload = f"Original fact: {fact.text}"
    try:
        resp = chat(
            provider=provider,
            api_key=api_key,
            model=model,
            system=sys,
            user_content=payload,
            max_tokens=160,
        )
        text = resp.text.strip().strip('"').strip()
        return text or fact.text
    except LLMError as exc:
        from .llm_diagnostics import record
        record(errors, "rewrite_one", exc, context=f"fact={fact.id}")
        return fact.text


# ─────────────────────────────────────────────────────────
# Step 6 — judge from three roles
# ─────────────────────────────────────────────────────────


JUDGE_SYSTEM = """You are evaluating a tailored resume from THREE perspectives.

Output ONLY JSON:
{
  "ats": {"score": 0-10, "feedback": "..."},
  "hr": {"score": 0-10, "feedback": "..."},
  "hiring_manager": {"score": 0-10, "feedback": "..."}
}

ATS bot perspective: Can it parse cleanly? Keyword density vs JD?
HR screener (6-second scan): Top 3 lines clear? Logos / titles attention-grabbing?
Hiring manager: Does this person solve the actual problem? Specific & credible?

Be honest and specific in feedback (one sentence each)."""


@dataclass
class JudgeReport:
    ats: dict
    hr: dict
    hiring_manager: dict
    composite_score: float

    def as_dict(self) -> dict:
        return {
            "ats": self.ats,
            "hr": self.hr,
            "hiring_manager": self.hiring_manager,
            "composite_score": self.composite_score,
        }


def judge_resume(
    resume_text: str,
    job: Job,
    *,
    provider: str,
    api_key: str,
    model: str,
    errors: list[str] | None = None,
) -> JudgeReport:
    if not api_key:
        from .llm_diagnostics import record
        record(errors, "judge_resume", RuntimeError("API key 缺失"),
               context="step 6 跳过；composite=0/10")
        return JudgeReport(ats={}, hr={}, hiring_manager={}, composite_score=0.0)
    payload = (
        f"## JD\nTitle: {job.title}\nCompany: {job.company}\n{(job.description or '')[:1500]}\n\n"
        f"## Resume (markdown)\n{resume_text[:4000]}"
    )
    try:
        resp = chat(
            provider=provider,
            api_key=api_key,
            model=model,
            system=JUDGE_SYSTEM,
            user_content=payload,
            max_tokens=600,
        )
    except LLMError as exc:
        from .llm_diagnostics import record
        record(errors, "judge_resume", exc, context="step 6 LLM 失败；composite=0/10")
        return JudgeReport(ats={}, hr={}, hiring_manager={}, composite_score=0.0)
    parsed = resp.parse_json(default={})
    ats = parsed.get("ats", {}) or {}
    hr = parsed.get("hr", {}) or {}
    hm = parsed.get("hiring_manager", {}) or {}
    scores = []
    for d in (ats, hr, hm):
        try:
            scores.append(float(d.get("score", 0)))
        except (ValueError, TypeError):
            pass
    composite = round(sum(scores) / max(len(scores), 1), 2)
    return JudgeReport(ats=ats, hr=hr, hiring_manager=hm, composite_score=composite)


# ─────────────────────────────────────────────────────────
# Orchestrator — run the full pipeline
# ─────────────────────────────────────────────────────────


def _pick_model(step_name: str, default: str, overrides: dict | None) -> str:
    """Resolve a per-step model override → fall back to default.

    Wires `LLMSettings.step_overrides` into every chat() call inside the
    pipeline so the UI's per-step dropdown actually changes which model is
    used. Without this, step_overrides would persist to JSON but never
    actually influence runtime — silent feature failure.
    """
    return ((overrides or {}).get(step_name) or default).strip() or default


def run_pipeline(
    session: Session,
    *,
    job: Job,
    provider: str,
    api_key: str,
    model: str,
    adversarial_enabled: bool = True,
    step_overrides: dict | None = None,
) -> RewriteAttempt:
    """Run the full 6-step pipeline. Returns the persisted RewriteAttempt.

    Variants are saved as FactVariant rows linked to (fact_id, job_id).
    Caller can later assemble approved variants into a final document.

    Each LLM step records into `pipeline_errors` on transient/silent failure.
    Final list lands in `attempt.judge_report["pipeline_warnings"]` for the
    route to surface in flash. Without this users get '流水线完成 · composite
    0/10' and never know LLM 401'd.
    """
    if not api_key:
        raise LLMConfigError("Resume pipeline requires an LLM API key.")
    pipeline_errors: list[str] = []

    identity = get_current_identity(session)
    if identity is None:
        raise RuntimeError("No current IdentityVersion — upload a resume first.")

    facts = (
        session.execute(
            select(ResumeFact).where(
                ResumeFact.identity_id == identity.id, ResumeFact.active.is_(True)
            )
        )
        .scalars()
        .all()
    )

    cal = session.get(UserCalibration, 1)
    user_max_level = (cal.max_amplification_level if cal else 2)

    # ── Inherit prior decisions on this job (PR-style re-run) ──
    # Build a map fact_id → most-recent variant. This lets us SKIP regenerating
    # facts the user already approved/edited (preserves their work) and feed
    # rejection reasons back as negative examples (so LLM produces something
    # actually different, not the same garbage).
    prior_decisions: dict[str, FactVariant] = _build_prior_decisions(session, job.id)
    rejected_history: dict[str, list[tuple[str, Optional[str]]]] = _build_rejected_history(session, job.id)

    n_inherited = 0
    n_skipped_decided = 0

    # Step 1
    keywords = extract_jd_keywords(
        job, provider=provider, api_key=api_key,
        model=_pick_model("extract_jd_keywords", model, step_overrides),
        errors=pipeline_errors,
    )
    log.info("extracted %d keywords from JD", len(keywords))

    # Step 2
    pairings = match_facts_to_keywords(
        facts, keywords, provider=provider, api_key=api_key,
        model=_pick_model("match_facts_to_keywords", model, step_overrides),
        errors=pipeline_errors,
    )

    # Step 3 + 4 + 5: rewrite, ground, adversarial
    saved_variants: list[FactVariant] = []
    for p in pairings:
        if not p.matched_keywords:
            continue  # don't rewrite facts without keyword targets

        # ── Inheritance: if user already decided ✓ or ✎ on this fact, leave it. ──
        prior = prior_decisions.get(p.fact.id)
        if prior is not None and prior.user_decision in ("approved", "edited_by_user"):
            saved_variants.append(prior)
            n_inherited += 1
            continue  # do NOT regenerate; user's decision stands

        # ── Negative-prompt injection: if user previously rejected variants
        # on this fact, feed their text + reason back to LLM so re-run differs.
        negs = rejected_history.get(p.fact.id, [])

        try:
            text = rewrite_one(
                p.fact,
                p.matched_keywords,
                provider=provider,
                api_key=api_key,
                model=_pick_model("rewrite_per_bullet", model, step_overrides),
                negative_examples=negs or None,
                errors=pipeline_errors,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("rewrite_one failed for fact %s: %s", p.fact.id, exc)
            continue
        if text == p.fact.text:
            continue  # no change

        try:
            grounding = ground_variant(
                source_facts=[p.fact],
                jd_context=(job.description or "")[:1500],
                variant_text=text,
                provider=provider,
                api_key=api_key,
                model=_pick_model("ground_variant", model, step_overrides),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("grounding failed: %s", exc)
            grounding = GroundingReport(level=1, evidence_spans=[], hallucinated_claims=[], justification="grounding error")

        objection = None
        if adversarial_enabled:
            try:
                objection = raise_objection(
                    fact=p.fact,
                    job=job,
                    variant_text=text,
                    provider=provider,
                    api_key=api_key,
                    model=_pick_model("raise_objection", model, step_overrides),
                    errors=pipeline_errors,
                )
            except Exception as exc:  # noqa: BLE001
                from .llm_diagnostics import record
                record(pipeline_errors, "adversarial_voice", exc,
                       context=f"fact={p.fact.id}")
                objection = None

        try:
            v = save_variant(
                session,
                fact=p.fact,
                job_id=job.id,
                variant_text=text,
                grounding=grounding,
                adversarial_objection=objection,
                user_max_level=user_max_level,
            )
            saved_variants.append(v)

            # ── ADR-0015 改动 B/E opt-in wiring (feature-flagged) ──────
            # Read flags fresh per-attempt so the user can flip them in the UI
            # between runs without restarting.
            try:
                from .llm_settings import load as load_llm_settings
                from flask import current_app

                _llm_overlay = load_llm_settings(
                    current_app.config["SETTINGS"].data_dir
                )
            except Exception:  # noqa: BLE001
                _llm_overlay = None

            if _llm_overlay and _llm_overlay.is_enabled("decompose_bullets"):
                try:
                    from .bullet_decomposer import decompose

                    decomp = decompose(
                        fact_text=p.fact.text,
                        bullet_text=text,
                        jd_keywords=keywords,
                        jd_excerpt=(job.description or "")[:1000],
                        provider=provider, api_key=api_key,
                        model=_pick_model("bullet_decompose", model, step_overrides),
                    )
                    v.decomposition_json = decomp.to_jsonable()
                except Exception as exc:  # noqa: BLE001
                    log.warning("bullet decompose failed for variant %s: %s", v.id, exc)

            if _llm_overlay and _llm_overlay.is_enabled("translate_variants"):
                try:
                    from .scrape_config import load as load_intent
                    from .variant_translator import translate_variant

                    user_langs = (
                        load_intent(current_app.config["SETTINGS"].data_dir)
                        .global_.user_languages
                    )
                    # Decomposition (if just generated) provides preservation tokens
                    decomposition_obj = None
                    if v.decomposition_json:
                        from .bullet_decomposer import BulletDecomposition

                        decomposition_obj = BulletDecomposition.from_dict(
                            v.decomposition_json
                        )
                    translated_map = {}
                    for lang in (user_langs or []):
                        if lang.lower() in ("en", "english"):
                            continue
                        tv = translate_variant(
                            text, target_language=lang,
                            decomposition=decomposition_obj,
                            provider=provider, api_key=api_key,
                            model=_pick_model("translate_variant", model, step_overrides),
                            errors=pipeline_errors,
                        )
                        translated_map[lang] = tv.to_jsonable()
                    if translated_map:
                        v.translated_json = translated_map
                except Exception as exc:  # noqa: BLE001
                    log.warning("variant translate failed for variant %s: %s", v.id, exc)
        except ClaimGroundingError as exc:
            log.info("variant rejected (L5): %s", exc)
            continue

    # Step 6: judge a "draft assembly" of accepted variants for an early signal
    draft_text = _assemble_draft(facts, saved_variants)
    judge_report = judge_resume(
        draft_text, job, provider=provider, api_key=api_key,
        model=_pick_model("judge_resume", model, step_overrides),
        errors=pipeline_errors,
    )

    # ── ADR-0015 改动 C opt-in wiring (deterministic, free) ──────
    # Native-language ATS literal-keyword scan. Augments JudgeReport with a
    # `native_ats` field — UI renders coverage + missing keywords inline.
    native_ats_payload: dict | None = None
    try:
        from .llm_settings import load as load_llm_settings
        from flask import current_app

        _llm_overlay = load_llm_settings(
            current_app.config["SETTINGS"].data_dir
        )
        if _llm_overlay and _llm_overlay.is_enabled("native_ats_check"):
            from .native_ats_check import native_ats_check

            ats_report = native_ats_check(draft_text, job.description or "")
            native_ats_payload = {
                "total": ats_report.total,
                "matched": ats_report.matched,
                "missing": ats_report.missing,
                "coverage_ratio": ats_report.coverage_ratio,
                "multi_word_keywords": ats_report.multi_word_keywords,
            }
    except Exception as exc:  # noqa: BLE001
        log.warning("native ats check failed: %s", exc)

    # Persist the attempt — merge native_ats into judge_report dict so review UI
    # can render it. (5-07 fix: previously the native_ats key was added to a
    # local `jd` dict but a fresh `as_dict()` call below dropped it.)
    judge_report_dict = judge_report.as_dict()
    if native_ats_payload is not None:
        judge_report_dict["native_ats"] = native_ats_payload
    # Y1 (5-08): stash pipeline_warnings so the route can flash a warning level
    # when LLM steps degraded silently. Otherwise users see "composite 0/10" with
    # no clue that LLM 401'd / timed out.
    if pipeline_errors:
        judge_report_dict["pipeline_warnings"] = pipeline_errors

    attempt = RewriteAttempt(
        job_id=job.id,
        identity_id=identity.id,
        keywords_extracted=keywords,
        judge_score=judge_report.composite_score,
        judge_report=judge_report_dict,
    )
    session.add(attempt)
    session.add(
        ReflectionEvent(
            kind="rewrite_attempt_completed",
            payload_json={
                "job_id": job.id,
                "variants": len(saved_variants),
                "keywords": len(keywords),
                "composite_score": judge_report.composite_score,
                "pipeline_warnings": pipeline_errors[:],
            },
            related_job_id=job.id,
        )
    )
    session.flush()  # autoflush=False — caller needs attempt.id immediately
    return attempt


def _assemble_draft(
    facts: list[ResumeFact], variants: list[FactVariant]
) -> str:
    """Quick markdown assembly: variant text where present, else fact text."""
    by_fact = {v.fact_id: v for v in variants}
    out = ["# Draft Resume\n"]
    by_kind: dict[str, list[str]] = {}
    for f in facts:
        v = by_fact.get(f.id)
        text = v.text if v else f.text
        by_kind.setdefault(f.kind, []).append(text)
    for kind in ("experience", "experience_bullet", "experience_detail", "project", "skill_technical", "skill_tools", "skill_soft", "language", "education", "certification"):
        items = by_kind.get(kind)
        if not items:
            continue
        out.append(f"\n## {kind.replace('_', ' ').title()}\n")
        for it in items:
            out.append(f"- {it}")
    return "\n".join(out)


def regenerate_single_variant(
    session: Session,
    *,
    variant: FactVariant,
    job: Job,
    provider: str,
    api_key: str,
    model: str,
    adversarial_enabled: bool = True,
    step_overrides: dict | None = None,
) -> FactVariant:
    """Re-generate ONE variant in place — used by per-card ↻ button.

    Reuses the original fact + the latest attempt's matched_keywords.
    Pulls in any prior rejected texts on this fact as negative examples,
    so a re-roll actually produces something new.
    """
    fact = session.get(ResumeFact, variant.fact_id)
    if fact is None:
        raise RuntimeError("Underlying fact not found.")

    # Reuse the latest attempt's keywords — don't re-extract JD
    latest_attempt = (
        session.execute(
            select(RewriteAttempt)
            .where(RewriteAttempt.job_id == job.id)
            .order_by(RewriteAttempt.created_at.desc())
            .limit(1)
        )
        .scalar_one_or_none()
    )
    keywords = (latest_attempt.keywords_extracted if latest_attempt else None) or []
    # Choose keywords most relevant to this fact (re-pair just this one fact)
    pairings = match_facts_to_keywords(
        [fact], keywords, provider=provider, api_key=api_key,
        model=_pick_model("match_facts_to_keywords", model, step_overrides),
    )
    matched = pairings[0].matched_keywords if pairings else []
    if not matched:
        matched = keywords[:3]  # fallback so we still attempt a rewrite

    # Collect this fact's rejection history + ALSO include current variant's text
    # as a negative ("user is asking for something different from what's shown now")
    negs: list[tuple[str, Optional[str]]] = [(variant.text, "user clicked ↻ — give a different angle")]
    rej_rows = (
        session.execute(
            select(FactVariant)
            .where(
                FactVariant.fact_id == fact.id,
                FactVariant.job_id == job.id,
                FactVariant.user_decision == "rejected",
            )
            .order_by(FactVariant.created_at.desc())
            .limit(4)
        )
        .scalars()
        .all()
    )
    for r in rej_rows:
        negs.append((r.text, r.rejection_reason))

    new_text = rewrite_one(
        fact, matched,
        provider=provider, api_key=api_key,
        model=_pick_model("rewrite_per_bullet", model, step_overrides),
        negative_examples=negs,
    )
    if not new_text or new_text == fact.text or new_text == variant.text:
        # LLM gave us nothing new — bail out, leave existing variant alone
        raise RuntimeError("LLM 没有生成不同的版本，再试一次。")

    # Re-ground + re-objection for the new text
    try:
        grounding = ground_variant(
            source_facts=[fact],
            jd_context=(job.description or "")[:1500],
            variant_text=new_text,
            provider=provider, api_key=api_key,
            model=_pick_model("ground_variant", model, step_overrides),
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("regen grounding failed: %s", exc)
        grounding = GroundingReport(level=variant.amplification_level, evidence_spans=[], hallucinated_claims=[], justification="grounding error")

    objection = None
    if adversarial_enabled:
        try:
            objection = raise_objection(
                fact=fact, job=job, variant_text=new_text,
                provider=provider, api_key=api_key,
                model=_pick_model("raise_objection", model, step_overrides),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("regen adversarial failed: %s", exc)
            objection = None

    # Update variant in place — preserves variant.id so URL anchors stay valid.
    variant.text = new_text
    variant.amplification_level = grounding.level
    variant.adversarial_objection = objection
    variant.user_decision = "pending"  # reset — a fresh roll needs a fresh decision
    variant.rejection_reason = None
    session.flush()
    return variant
