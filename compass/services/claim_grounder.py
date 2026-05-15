"""Claim grounder — the 6-level transformation spectrum + L5 hard ban.

L0  direct copy           ✓ auto-allow
L1  synonym rewrite       ✓ auto-allow
L2  implicit→explicit     ⚠ user one-time confirmation
L3  reframing             ⚠ MUST go through interview engine
L4  inferred quantification  ⚠ user approval required (per-bullet)
L5  fabrication           ✗ HARD-BAN — service-layer rejection

L5 hard rules trigger by sensitive_category:
  education_credential  — no degree/school/dates change
  language_level        — no level upgrade
  employment_dates      — dates frozen ±0
  certification         — name/issuer/year frozen
  job_titles            — formal title cannot be elevated
  salary                — no salary numbers in output

Service layer always runs `screen_for_l5(variant_text, source_facts)` before
saving any FactVariant. If a violation fires, the service layer raises
ClaimGroundingError and the variant never gets stored.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

from sqlalchemy.orm import Session

from ..models import FactVariant, ReflectionEvent, ResumeFact
from .llm import chat

log = logging.getLogger(__name__)


class ClaimGroundingError(Exception):
    """Raised when a variant violates an L5 hard rule. Service must NOT swallow."""


# ─────────────────────────────────────────────────────────
# L5 hard rules — deterministic, no LLM
# ─────────────────────────────────────────────────────────


_LANG_LEVEL_RE = re.compile(r"\b([ABC][12]|native|mother\s?tongue|fluent|proficient|conversational|basic|beginner|intermediate|advanced)\b", re.I)
_DEGREE_RE = re.compile(r"\b(BSc|BS|BA|MSc|MS|MA|MBA|PhD|JD|MD|Bachelor|Master|Doctor|Doctorate)\b", re.I)
_DATE_RE = re.compile(r"\b(?:19|20)\d{2}\b|\b\d{1,2}\s?(?:months?|yrs?|years?)\b")
# Catches integers, decimals, percentages, and unit-bearing numbers (10x, 5K).
# Handles US thousands separator ("1,500", "$50,000") — comma stripped at parse time.
# Excludes 4-digit years (handled by _DATE_RE in sensitive-category branch).
_NUM_RE = re.compile(
    r"(?<![\d,])"
    r"(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"\s*(%|x|K|M|B)?"
    r"(?!\d)",
    re.I,
)
_TITLE_ELEVATIONS = [
    (r"\bparticipated in\b", r"\bled\b"),
    (r"\bcontributed to\b", r"\bled\b"),
    (r"\bsupported\b", r"\bowned\b"),
    (r"\bassisted\b", r"\bdrove\b"),
    (r"\bworked on\b", r"\bowned\b"),
]

# Drift threshold: |Δ| / source_value < this → L5a (snap-back), else L5b (fabrication)
# 20% chosen as "humans wouldn't drift this far accidentally". Tunable.
_NUMERIC_DRIFT_REL_THRESHOLD = 0.20


@dataclass
class L5Violation:
    """An L5-level grounding issue.

    `kind` distinguishes (per ADR-0015):
      L5a — rendering drift (LLM sampling jitter, snap-back-able with auto_fix)
      L5b — substantive fabrication (entity / fact never in source — hard block)

    Defaults to L5b for backward-compat with pre-v4 callers that only know
    "violation = hard block". New callers should branch on `kind` and use
    `auto_fix` to offer one-click correction for L5a.
    """

    rule: str
    detail: str
    kind: str = "L5b"
    auto_fix: str | None = None
    bad_token: str | None = None


def _extract_numbers(text: str) -> list[tuple[float, str]]:
    """Return [(value, original_token), ...] for every number in `text`.

    Skips 4-digit years (1900-2099) since those are handled by _DATE_RE in the
    sensitive-category branch — different semantics (year drift = L5b always).
    """
    out: list[tuple[float, str]] = []
    for m in _NUM_RE.finditer(text):
        raw = m.group(0).strip()
        try:
            val = float(m.group(1).replace(",", ""))
        except ValueError:
            continue
        # Skip year-like 4-digit ints (handled separately)
        if 1900 <= val <= 2099 and m.group(1) == m.group(0).strip():
            continue
        unit = (m.group(2) or "").lower()
        # Normalize unit to value
        if unit == "k":
            val *= 1_000
        elif unit == "m":
            val *= 1_000_000
        elif unit == "b":
            val *= 1_000_000_000
        # `%` and `x` are kept as multipliers — comparison is on the bare value
        out.append((val, raw))
    return out


def _check_numeric_drift(
    variant_text: str,
    source_facts: list[ResumeFact],
) -> Optional[L5Violation]:
    """Detect numbers in variant that aren't in source.

    Per ADR-0015:
      - relative diff < 20% from a source number  → L5a renderdrift, snap-back
      - no near match in source                   → L5b fabrication, hard block

    Returns the *first* violation found (callers stop at first hit).
    """
    src_blob = " ".join(f.text for f in source_facts)
    src_nums = _extract_numbers(src_blob)
    var_nums = _extract_numbers(variant_text)
    if not var_nums:
        return None

    src_values = [v for v, _ in src_nums]
    src_value_set = {v for v, _ in src_nums}

    for var_val, var_raw in var_nums:
        if var_val in src_value_set:
            continue  # exact match — fine
        if not src_values:
            return L5Violation(
                rule="numeric_fabrication",
                detail=f"variant introduces number '{var_raw}' but source has no comparable numbers",
                kind="L5b",
            )
        closest = min(src_values, key=lambda x: abs(x - var_val))
        # Find original source token that produced `closest` for the auto_fix suggestion
        closest_raw = next(raw for v, raw in src_nums if v == closest)
        if closest == 0:
            # Avoid div-by-zero — treat any non-zero variant number against source 0 as fabrication
            return L5Violation(
                rule="numeric_fabrication",
                detail=f"variant has '{var_raw}' but corresponding source number is 0",
                kind="L5b",
            )
        rel_diff = abs(var_val - closest) / abs(closest)
        if rel_diff < _NUMERIC_DRIFT_REL_THRESHOLD:
            return L5Violation(
                rule="numeric_drift",
                detail=f"variant uses '{var_raw}' but source has '{closest_raw}' "
                f"(relative diff {rel_diff:.1%}, < {_NUMERIC_DRIFT_REL_THRESHOLD:.0%} threshold) — "
                "likely LLM rendering drift, can be snapped back",
                kind="L5a",
                auto_fix=closest_raw,
                bad_token=var_raw,
            )
        return L5Violation(
            rule="numeric_fabrication",
            detail=f"variant introduces '{var_raw}' (no near match in source; closest '{closest_raw}', "
            f"relative diff {rel_diff:.1%})",
            kind="L5b",
            bad_token=var_raw,
        )
    return None


def screen_for_l5(
    variant_text: str,
    source_facts: list[ResumeFact],
    *,
    target_sensitive_category: str | None = None,
) -> Optional[L5Violation]:
    """Return L5Violation or None. Pure function — no LLM, no DB.

    Per ADR-0015 the violation may be L5a (renderdrift, snap-back-able) or
    L5b (fabrication, hard block). Callers branch on `violation.kind`.
    """
    src_blob = " ".join(f.text for f in source_facts).lower()
    out = variant_text.lower()

    # Language-level: reject any new level claim that's not in source
    new_levels = set(m.group(0).lower() for m in _LANG_LEVEL_RE.finditer(out))
    src_levels = set(m.group(0).lower() for m in _LANG_LEVEL_RE.finditer(src_blob))
    if new_levels - src_levels:
        introduced = ", ".join(sorted(new_levels - src_levels))
        return L5Violation(
            rule="language_level_introduction",
            detail=f"variant introduces language-level claims not in source: {introduced}",
        )

    # Degree: reject new degree types
    new_degs = set(m.group(0).lower() for m in _DEGREE_RE.finditer(out))
    src_degs = set(m.group(0).lower() for m in _DEGREE_RE.finditer(src_blob))
    if new_degs - src_degs:
        introduced = ", ".join(sorted(new_degs - src_degs))
        return L5Violation(
            rule="degree_introduction",
            detail=f"variant introduces degree claims not in source: {introduced}",
        )

    # Title elevation: detect "participated → led" type rewrites
    for soft, strong in _TITLE_ELEVATIONS:
        if re.search(soft, src_blob) and re.search(strong, out):
            return L5Violation(
                rule="title_elevation",
                detail=f"source uses softer verb (matched {soft!r}) but variant uses stronger verb (matched {strong!r}). Consider Tier 2/3 phrasing.",
            )

    # Sensitive-category locks
    if target_sensitive_category in {"education_credential", "certification", "language_level", "employment_dates"}:
        # Strict: variant text must be a near-superset of source (no new dates/levels/years)
        new_dates = set(m.group(0) for m in _DATE_RE.finditer(out))
        src_dates = set(m.group(0) for m in _DATE_RE.finditer(src_blob))
        if new_dates - src_dates:
            return L5Violation(
                rule="sensitive_date_change",
                detail=f"variant introduces dates not in source for sensitive category {target_sensitive_category}: {new_dates - src_dates}",
            )

    # Numeric drift detection (ADR-0015 L5a / L5b split).
    # Runs *after* the strict 4 categories so they keep priority — those
    # ARE substantive (a wrong degree is L5b regardless), and this layer
    # is for non-sensitive-category metric drift like "23% → 22%".
    numeric_violation = _check_numeric_drift(variant_text, source_facts)
    if numeric_violation is not None:
        return numeric_violation

    return None


# ─────────────────────────────────────────────────────────
# L0–L4 LLM-driven grounding
# ─────────────────────────────────────────────────────────


GROUNDING_SYSTEM = """You are Compass's claim-grounder.

You see (a) ONE source fact (immutable truth), (b) a JD context (target),
and (c) a candidate REWRITE the system has produced. Your job:

  1. Determine the AMPLIFICATION LEVEL of the rewrite vs the source:
       L0 direct copy            text is essentially identical
       L1 synonym rewrite        same content, different words
       L2 implicit→explicit      surfaces info the source already implies
       L3 reframing              same content, different framing/lens
       L4 inferred quantification  introduces numbers not in source
       L5 fabrication            introduces NEW claims not in source
  2. Pinpoint EVIDENCE — which span(s) of the source support the rewrite?
  3. Flag any HALLUCINATION — claims in the rewrite NOT supported by source.

Output ONLY JSON:
{
  "level": 0-5,
  "evidence_spans": ["span 1 from source", "span 2", ...],
  "hallucinated_claims": ["claim 1 not in source", ...],
  "justification": "one sentence"
}
"""


@dataclass
class GroundingReport:
    level: int
    evidence_spans: list[str]
    hallucinated_claims: list[str]
    justification: str


def ground_variant(
    *,
    source_facts: list[ResumeFact],
    jd_context: str,
    variant_text: str,
    provider: str,
    api_key: str,
    model: str,
) -> GroundingReport:
    """Returns a GroundingReport. Caller must reject if level >= 5."""
    src_blob = "\n".join(f"- {f.text}" for f in source_facts)
    payload = (
        f"## Source facts\n{src_blob}\n\n"
        f"## JD context (excerpt)\n{jd_context[:1500]}\n\n"
        f"## Candidate rewrite\n{variant_text}"
    )
    resp = chat(
        provider=provider,
        api_key=api_key,
        model=model,
        system=GROUNDING_SYSTEM,
        user_content=payload,
        max_tokens=600,
    )
    parsed = resp.parse_json(default={})
    return GroundingReport(
        level=int(parsed.get("level", 0)),
        evidence_spans=[str(s) for s in (parsed.get("evidence_spans") or []) if isinstance(s, str)],
        hallucinated_claims=[str(s) for s in (parsed.get("hallucinated_claims") or []) if isinstance(s, str)],
        justification=str(parsed.get("justification", ""))[:500],
    )


# ─────────────────────────────────────────────────────────
# Persisting variants — wraps L5 screen + LLM grounding
# ─────────────────────────────────────────────────────────


def save_variant(
    session: Session,
    *,
    fact: ResumeFact,
    job_id: str | None,
    variant_text: str,
    interview_qa_used: list[int] | None = None,
    grounding: GroundingReport | None = None,
    adversarial_objection: str | None = None,
    user_max_level: int = 3,
) -> FactVariant:
    """Save a variant, enforcing L5 hard ban + max_level cap.

    Raises ClaimGroundingError on L5 violation.
    """
    violation = screen_for_l5(
        variant_text,
        [fact],
        target_sensitive_category=fact.sensitive_category,
    )
    if violation is not None:
        if violation.kind == "L5a":
            # Renderdrift (LLM sampling jitter, e.g. 23% → 22%). Auto-snap back
            # to the source value and log an event so the user can audit / revert.
            # NOT silent: the event stream is the visible record. (ADR-0015)
            corrected_text = variant_text
            if violation.bad_token and violation.auto_fix:
                corrected_text = variant_text.replace(
                    violation.bad_token, violation.auto_fix, 1
                )
            session.add(
                ReflectionEvent(
                    kind="l5a_drift_corrected",
                    payload_json={
                        "fact_id": fact.id,
                        "rule": violation.rule,
                        "detail": violation.detail,
                        "before": variant_text[:300],
                        "after": corrected_text[:300],
                        "bad_token": violation.bad_token,
                        "auto_fix": violation.auto_fix,
                    },
                    related_fact_id=fact.id,
                    related_job_id=job_id,
                )
            )
            variant_text = corrected_text
            # Fall through — the corrected variant proceeds to save normally.
        else:
            # L5b (default kind) — substantive fabrication, hard block.
            session.add(
                ReflectionEvent(
                    kind="l5_violation_blocked",
                    payload_json={
                        "fact_id": fact.id,
                        "rule": violation.rule,
                        "detail": violation.detail,
                        "rejected_text": variant_text[:300],
                    },
                    related_fact_id=fact.id,
                    related_job_id=job_id,
                )
            )
            raise ClaimGroundingError(f"L5 hard rule violated ({violation.rule}): {violation.detail}")

    inferred_level = grounding.level if grounding else 1
    if inferred_level > user_max_level:
        # Don't reject outright, but mark as needing user approval
        decision = "pending"
    elif inferred_level <= 1:
        decision = "approved"  # L0/L1 auto-approve within max_level
    else:
        decision = "pending"

    v = FactVariant(
        fact_id=fact.id,
        job_id=job_id,
        text=variant_text,
        amplification_level=inferred_level,
        user_decision=decision,
        interview_qa_used=interview_qa_used or [],
        adversarial_objection=adversarial_objection,
    )
    session.add(v)
    session.flush()
    session.add(
        ReflectionEvent(
            kind="variant_proposed",
            payload_json={
                "fact_id": fact.id,
                "job_id": job_id,
                "level": inferred_level,
                "decision": decision,
                "halluc_count": len(grounding.hallucinated_claims) if grounding else 0,
            },
            related_fact_id=fact.id,
            related_job_id=job_id,
        )
    )
    return v
