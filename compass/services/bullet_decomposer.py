"""ADR-0015 改动 B — bullet 五维度结构化建议菜单生成。

The output of run_pipeline's Step 3 (rewrite_one) is a polished bullet text.
This module decomposes that bullet into a STRUCTURED MENU the UI can render
as per-dimension suggestions, each carrying a grounding label and source tag.

Five dimensions (per ADR-0015):
  · verb         — alternative action verbs (LLM candidates with grounding)
  · metric       — numeric / quantitative claims (deterministic字面 check vs fact)
  · jd_keywords  — keywords from JD: in_bullet? in_fact? needs interview?
  · length       — word count vs resume convention (deterministic rule)
  · tone         — register/seniority verbs (LLM candidates with grounding)

The point of decomposition: the user sees per-dimension trade-offs, not a
single black-box "polished bullet". Each suggestion comes with a CLEAR
provenance (code-derived OR LLM-suggested) and a clear grounding (safe /
check / needs_interview / blocked).

The module returns BulletDecomposition; downstream consumers:
  · resume_writer.run_pipeline writes it to FactVariant.decomposition_json
  · UI _decomposition_panel.html renders it as the structured menu (改动 G)
  · user_facing_translator (改动 G2) rewrites the field labels into plain
    user-language before showing the user

LLM is OPTIONAL — the deterministic dimensions (metric / jd_keywords / length)
work without an API key. Without LLM, verb_candidates and tone_candidates are
empty but the menu is still useful (字面 grounding + length warning).
"""
from __future__ import annotations


import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Optional

from .llm import LLMConfigError, LLMError, chat

log = logging.getLogger(__name__)


# Deterministic length rules — typical resume bullet
_LENGTH_TOO_SHORT = 8       # words
_LENGTH_TOO_LONG = 25       # words
# Cheap word-tokenizer: split on whitespace, strip punctuation. Good enough for
# resume-bullet length estimates; not for NLP analysis.
_WORD_RE = re.compile(r"\b[\w][\w\.\-+]{0,}\b", re.UNICODE)


# ─────────────────────────────────────────────────────────
# Output dataclasses
# ─────────────────────────────────────────────────────────


@dataclass
class GroundingTag:
    """A grounding signal on a verb / tone candidate.

    level:
      "safe"        — directly supported by fact (synonym OK)
      "check"       — implicit but defensible (one-click user confirm)
      "interview"   — needs interview-driven discovery before use
      "blocked"     — fact does not support; do not use
    """

    level: str
    rationale: str = ""


@dataclass
class VerbCandidate:
    """A candidate verb (or tone phrase). Source is always 'llm' for now —
    the deterministic verb extraction from fact.structured_json is a future
    enhancement (after fact_decomposer).
    """

    text: str
    grounding: GroundingTag
    source: str = "llm"


@dataclass
class MetricStatus:
    """字面 metric check vs fact. Source is always 'code'."""

    bullet_value: Optional[str]
    """The metric token as it appears in the bullet ('23%', '10K', '5 years')."""
    in_fact: bool
    """True if this exact token also appears in the source fact text."""
    drift_kind: Optional[str] = None
    """If not in_fact: 'L5a' (renderdrift, snap-able) or 'L5b' (fabrication).
    None when in_fact=True."""
    auto_fix: Optional[str] = None
    """If drift_kind == 'L5a', the suggested value to snap to."""


@dataclass
class JDKeywordMatch:
    """For each JD keyword: is it in the bullet? Is the user able to back it
    up from a fact?"""

    keyword: str
    in_bullet: bool
    in_fact: bool
    needs_interview: bool = False
    """True when keyword absent from fact — using it would require ADR-0007
    interview-driven discovery."""


@dataclass
class LengthAssessment:
    """Length verdict against resume convention.
    recommendation ∈ {'ok', 'too_short', 'too_long'}"""

    words: int
    recommendation: str


@dataclass
class BulletDecomposition:
    """The structured menu. UI renders one section per dimension."""

    verb_candidates: list[VerbCandidate] = field(default_factory=list)
    metrics: list[MetricStatus] = field(default_factory=list)
    jd_keywords: list[JDKeywordMatch] = field(default_factory=list)
    length: LengthAssessment = field(
        default_factory=lambda: LengthAssessment(words=0, recommendation="ok")
    )
    tone_candidates: list[VerbCandidate] = field(default_factory=list)
    llm_used: bool = False

    def to_jsonable(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "BulletDecomposition":
        if not d:
            return cls()
        return cls(
            verb_candidates=[
                _verbcand_from_dict(v) for v in (d.get("verb_candidates") or [])
            ],
            metrics=[
                MetricStatus(
                    bullet_value=m.get("bullet_value"),
                    in_fact=bool(m.get("in_fact", False)),
                    drift_kind=m.get("drift_kind"),
                    auto_fix=m.get("auto_fix"),
                )
                for m in (d.get("metrics") or [])
            ],
            jd_keywords=[
                JDKeywordMatch(
                    keyword=str(k.get("keyword", "")),
                    in_bullet=bool(k.get("in_bullet", False)),
                    in_fact=bool(k.get("in_fact", False)),
                    needs_interview=bool(k.get("needs_interview", False)),
                )
                for k in (d.get("jd_keywords") or [])
            ],
            length=LengthAssessment(
                words=int((d.get("length") or {}).get("words", 0)),
                recommendation=(d.get("length") or {}).get("recommendation", "ok"),
            ),
            tone_candidates=[
                _verbcand_from_dict(v) for v in (d.get("tone_candidates") or [])
            ],
            llm_used=bool(d.get("llm_used", False)),
        )


def _verbcand_from_dict(d: dict) -> VerbCandidate:
    g = d.get("grounding") or {}
    return VerbCandidate(
        text=str(d.get("text", "")),
        grounding=GroundingTag(
            level=str(g.get("level", "check")),
            rationale=str(g.get("rationale", "")),
        ),
        source=str(d.get("source", "llm")),
    )


# ─────────────────────────────────────────────────────────
# Deterministic dimensions (no LLM)
# ─────────────────────────────────────────────────────────


def _word_count(text: str) -> int:
    return len(_WORD_RE.findall(text or ""))


def assess_length(bullet_text: str) -> LengthAssessment:
    n = _word_count(bullet_text)
    if n < _LENGTH_TOO_SHORT:
        verdict = "too_short"
    elif n > _LENGTH_TOO_LONG:
        verdict = "too_long"
    else:
        verdict = "ok"
    return LengthAssessment(words=n, recommendation=verdict)


def assess_metrics(bullet_text: str, fact_text: str) -> list[MetricStatus]:
    """Reuse the L5a/L5b drift logic from claim_grounder — same fact-anchored
    fuzzy threshold, same auto_fix. Returns one MetricStatus per number found
    in bullet (or empty list if no numbers)."""
    from .claim_grounder import (
        _NUMERIC_DRIFT_REL_THRESHOLD,
        _extract_numbers,
    )

    out: list[MetricStatus] = []
    bullet_nums = _extract_numbers(bullet_text)
    fact_nums = _extract_numbers(fact_text)
    fact_value_set = {v for v, _ in fact_nums}
    fact_values = [v for v, _ in fact_nums]

    for bullet_val, bullet_raw in bullet_nums:
        if bullet_val in fact_value_set:
            out.append(MetricStatus(bullet_value=bullet_raw, in_fact=True))
            continue
        if not fact_values:
            out.append(
                MetricStatus(
                    bullet_value=bullet_raw,
                    in_fact=False,
                    drift_kind="L5b",
                )
            )
            continue
        closest = min(fact_values, key=lambda x: abs(x - bullet_val))
        if closest == 0:
            out.append(
                MetricStatus(
                    bullet_value=bullet_raw, in_fact=False, drift_kind="L5b"
                )
            )
            continue
        rel_diff = abs(bullet_val - closest) / abs(closest)
        if rel_diff < _NUMERIC_DRIFT_REL_THRESHOLD:
            closest_raw = next(raw for v, raw in fact_nums if v == closest)
            out.append(
                MetricStatus(
                    bullet_value=bullet_raw,
                    in_fact=False,
                    drift_kind="L5a",
                    auto_fix=closest_raw,
                )
            )
        else:
            out.append(
                MetricStatus(
                    bullet_value=bullet_raw, in_fact=False, drift_kind="L5b"
                )
            )
    return out


def assess_jd_keywords(
    bullet_text: str,
    fact_text: str,
    jd_keywords: list[str],
) -> list[JDKeywordMatch]:
    """Per-keyword: in bullet? in fact? Without fact support, using it
    in the bullet would need ADR-0007 interview discovery."""
    bullet_lower = (bullet_text or "").lower()
    fact_lower = (fact_text or "").lower()
    out: list[JDKeywordMatch] = []
    for kw in jd_keywords:
        if not isinstance(kw, str) or not kw.strip():
            continue
        kw_lower = kw.lower().strip()
        in_bullet = kw_lower in bullet_lower
        in_fact = kw_lower in fact_lower
        out.append(
            JDKeywordMatch(
                keyword=kw,
                in_bullet=in_bullet,
                in_fact=in_fact,
                needs_interview=(in_bullet and not in_fact),
            )
        )
    return out


def decompose_deterministic(
    bullet_text: str,
    fact_text: str,
    jd_keywords: list[str],
) -> BulletDecomposition:
    """The无LLM 下的 decomposition. Always works, even without API key."""
    return BulletDecomposition(
        verb_candidates=[],
        metrics=assess_metrics(bullet_text, fact_text),
        jd_keywords=assess_jd_keywords(bullet_text, fact_text, jd_keywords),
        length=assess_length(bullet_text),
        tone_candidates=[],
        llm_used=False,
    )


# ─────────────────────────────────────────────────────────
# LLM dimensions (verb + tone candidates)
# ─────────────────────────────────────────────────────────


CANDIDATES_SYSTEM = """You are a resume bullet decomposer.

Given (a) the user's source fact (immutable truth), (b) a proposed bullet
rewrite, and (c) the target JD, produce candidate ALTERNATIVE verbs and tones
the user could use, EACH with a grounding label.

Grounding labels:
  "safe"      — directly supported by the source fact (synonym OK)
  "check"     — fact implies it but doesn't say it; one-click user confirm OK
  "interview" — fact does not show it; user must answer 1-3 questions to back it up
  "blocked"   — fact contradicts or is silent on this; do not use this verb

Verbs are action words (Built / Developed / Led / Architected / Owned).
Tones are seniority/scope phrasings (e.g. "as IC", "leading the team",
"end-to-end", "in production").

OUTPUT ONLY JSON. No commentary. Format:
{
  "verb_candidates": [
    {"text": "Developed", "grounding": {"level": "safe", "rationale": "≤25 words why"}},
    {"text": "Architected", "grounding": {"level": "blocked", "rationale": "fact says contributor not architect"}}
  ],
  "tone_candidates": [
    {"text": "as the project lead", "grounding": {"level": "interview", "rationale": "fact shows ownership claim is unclear"}}
  ]
}

Provide 3-5 verb candidates and 2-4 tone candidates. Be HONEST about grounding
— blocked entries are valuable (they tell the user what NOT to use)."""


def _candidates_from_llm(
    fact_text: str,
    bullet_text: str,
    jd_excerpt: str,
    *,
    provider: str,
    api_key: str,
    model: str,
    errors: list[str] | None = None,
) -> tuple[list[VerbCandidate], list[VerbCandidate]]:
    """Return (verb_candidates, tone_candidates). Empty lists on any failure."""
    if not api_key:
        return [], []
    payload = (
        f"## Source fact (immutable)\n{fact_text}\n\n"
        f"## Current bullet rewrite\n{bullet_text}\n\n"
        f"## Target JD (excerpt)\n{jd_excerpt[:1000]}"
    )
    try:
        resp = chat(
            provider=provider,
            api_key=api_key,
            model=model,
            system=CANDIDATES_SYSTEM,
            user_content=payload,
            max_tokens=600,
            temperature=0.4,
        )
    except LLMError as exc:
        # Parent-class catch covers LLMConfigError + post-retry LLMError.
        # Decomposition is opt-in enrichment — fail soft, return empty.
        from .llm_diagnostics import record
        record(errors, "bullet_decomposer.candidates", exc,
               context="opt-in enrichment 失败")
        return [], []
    parsed = resp.parse_json(default={})
    verbs = [_verbcand_from_dict(v) for v in (parsed.get("verb_candidates") or [])
             if isinstance(v, dict)]
    tones = [_verbcand_from_dict(v) for v in (parsed.get("tone_candidates") or [])
             if isinstance(v, dict)]
    # Cap the number of candidates so the menu doesn't blow up
    return verbs[:6], tones[:4]


# ─────────────────────────────────────────────────────────
# Top-level
# ─────────────────────────────────────────────────────────


def decompose(
    *,
    fact_text: str,
    bullet_text: str,
    jd_keywords: list[str],
    jd_excerpt: str = "",
    provider: str = "",
    api_key: str = "",
    model: str = "",
) -> BulletDecomposition:
    """Generate the structured decomposition for one bullet rewrite.

    No DB writes — pure function. Caller (resume_writer) writes the result
    to FactVariant.decomposition_json.

    LLM-derived candidates are optional — without an api_key, the result is
    deterministic-only (still useful for the user-facing menu).
    """
    base = decompose_deterministic(bullet_text, fact_text, jd_keywords)
    if not api_key:
        return base
    verbs, tones = _candidates_from_llm(
        fact_text, bullet_text, jd_excerpt,
        provider=provider, api_key=api_key, model=model,
    )
    return BulletDecomposition(
        verb_candidates=verbs,
        metrics=base.metrics,
        jd_keywords=base.jd_keywords,
        length=base.length,
        tone_candidates=tones,
        llm_used=bool(verbs or tones),
    )
