"""Adversarial voice — the part of Compass that NEVER learns user preferences.

Why this exists:
  The Reflective Flywheel learns user calibration. That's good for
  personalization, but creates a sycophancy risk — the system slowly drifts
  toward "what user accepts" rather than "what's defensible".

  The adversarial voice is the antibody. It speaks from a fixed prompt that
  cannot be modified by user feedback. Every variant proposal goes through it.
  If it raises an objection, the objection is stored on FactVariant.
  adversarial_objection — visible to the user but does NOT block.

  The user sees the objection and chooses. Compass does not silently filter.

ADR-0015 Two-Phase Adversarial (2026-05-06):
  The original v1 design fed the polished `variant_text` directly to a single
  adversarial LLM. Problem identified in ADR-0015: that LLM is from the same
  training distribution as the generator, so polished phrasing systematically
  passes the bar — the antibody gets fooled by style.

  Fix: split adversarial into two phases.
    Phase 1 — Generate `expected_objections` from fact + JD ONLY (no variant
              text in scope). Establishes an INDEPENDENT FACTUAL FOOTHOLD.
    Phase 2 — Then expose the variant text and ask: which expected_objections
              were addressed by NEW VERIFIABLE FACTS, vs only by WORDING.

  The two prompts remain frozen — ADR-0006 frozen-prompt invariant is
  preserved (no user-feedback path mutates either system prompt). The change
  is in INPUT STRUCTURE, not in voice.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

from .llm import LLMConfigError, LLMError, chat

if TYPE_CHECKING:  # avoid runtime circular import
    from ..models import Job, ResumeFact

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────
# Phase 1 — independent objection generation (FROZEN)
# ─────────────────────────────────────────────────────────
# This prompt sees fact + JD only. Generator never sees its output, the user
# never sees this output directly — it is INTERNAL scaffolding so Phase 2 has
# a fact-anchored frame of reference.

EXPECTED_OBJECTIONS_SYSTEM = """You are an extremely skeptical hiring manager.

You see ONLY:
  (a) the candidate's original resume fact (immutable truth)
  (b) the JD they're applying to

Your job: list the OBJECTIONS a tough interviewer would raise about this
candidate's fit for THIS JD, based PURELY on what the fact actually says.

Probe these specifically:
  - Is the fact's scope (personal project / coursework / professional work)
    actually appropriate for the JD's seniority / production expectations?
  - Does the fact mention quantified results, leadership, or ownership? If
    not, what objections follow from those gaps?
  - Is the fact's domain (industry / tech stack / data scale) a stretch
    for what the JD needs?
  - Is anything in the fact that could be challenged by a probing interview
    question?

Output ONLY JSON:
{"expected_objections": ["specific objection in <30 words", ...]}

Provide 2-5 objections. Be specific — never "the experience seems thin".
Be honest, not polite. NEVER soften.
"""


# ─────────────────────────────────────────────────────────
# Phase 2 — variant judged AGAINST expected_objections (FROZEN)
# ─────────────────────────────────────────────────────────

ADVERSARIAL_SYSTEM = """You are an extremely skeptical hiring manager reading a resume.

You have:
  (a) the candidate's original resume fact (immutable truth)
  (b) the JD they're applying to
  (c) a list of EXPECTED_OBJECTIONS you (you yourself, in a prior pass) would
      raise based on (a) and (b) alone — independent of any rewrite
  (d) the candidate's proposed REWRITE

For each expected objection, decide:
  - addressed_by_facts:  the rewrite added VERIFIABLE FACTS that legitimately
                         neutralize this objection (e.g. concrete metrics,
                         scope evidence) — these are fine.
  - addressed_by_wording: the rewrite only changed PHRASING (stronger verbs,
                          buzzwords, vague intensifiers) without adding facts —
                          these are red flags.

Output ONLY JSON:
{
  "objection": "string|null",
  "addressed_by_facts":   ["objection that was legitimately addressed", ...],
  "addressed_by_wording": ["objection that was only papered over", ...]
}

Rules:
  - If `addressed_by_wording` is non-empty, "objection" describes the most
    egregious wording-only fix in <30 words.
  - If everything is genuinely addressed by facts, "objection": null.
  - NEVER soften the objection to be polite.
  - NEVER endorse the rewrite — your job is the antibody.

The user will see "objection" and decide; you do not block.
"""


def raise_objection(
    *,
    fact: "ResumeFact",
    job: "Job",
    variant_text: str,
    provider: str,
    api_key: str,
    model: str,
    errors: list[str] | None = None,
) -> Optional[str]:
    """Two-phase adversarial review (ADR-0015).

    Phase 1 produces a fact-anchored list of expected_objections without
    seeing the variant text. Phase 2 then judges whether the variant
    addresses them with facts vs wording.

    Returns the most egregious wording-only objection, or None if the
    rewrite is genuinely fact-supported (or LLM unavailable).
    """
    if not api_key:
        return None

    jd_excerpt = (job.description or "")[:800]
    jd_block = f"{job.title or '?'} @ {job.company or '?'}\n{jd_excerpt}"

    # ── Phase 1 ──────────────────────────────────────────
    phase1_payload = (
        f"## Source fact\n{fact.text}\n\n"
        f"## JD\n{jd_block}"
    )
    try:
        p1 = chat(
            provider=provider,
            api_key=api_key,
            model=model,
            system=EXPECTED_OBJECTIONS_SYSTEM,
            user_content=phase1_payload,
            max_tokens=400,
            temperature=0.5,
        )
    except LLMError as exc:
        from .llm_diagnostics import record
        record(errors, "adversarial_voice.phase1", exc, context=f"fact={fact.id}")
        return None
    expected = p1.parse_json(default={}).get("expected_objections", [])
    if not isinstance(expected, list):
        expected = []
    expected = [str(o)[:200] for o in expected if isinstance(o, str)]

    # If Phase 1 found nothing to object to, the candidate is a solid fit;
    # no reason to invent objections in Phase 2.
    if not expected:
        return None

    # ── Phase 2 ──────────────────────────────────────────
    phase2_payload = (
        f"## Source fact\n{fact.text}\n\n"
        f"## JD\n{jd_block}\n\n"
        f"## Expected objections\n"
        + "\n".join(f"- {o}" for o in expected)
        + f"\n\n## Candidate rewrite\n{variant_text}"
    )
    try:
        p2 = chat(
            provider=provider,
            api_key=api_key,
            model=model,
            system=ADVERSARIAL_SYSTEM,
            user_content=phase2_payload,
            max_tokens=400,
            temperature=0.4,
        )
    except LLMError as exc:
        from .llm_diagnostics import record
        record(errors, "adversarial_voice.phase2", exc, context=f"fact={fact.id}")
        return None
    parsed = p2.parse_json(default={})
    obj = parsed.get("objection")
    if not obj or not isinstance(obj, str):
        return None
    return obj.strip()[:300]
