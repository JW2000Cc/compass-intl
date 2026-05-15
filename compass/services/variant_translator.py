"""ADR-0015 改动 E — variant_translator: bullet text → target-language render.

Per ADR-0015 §跨语言, the translation is a RENDER LAYER on top of the English
master bullet, not a separate fact. Locale-invariant elements (numbers, proper
nouns, JD keywords) are preserved字面 — they MUST appear in the translated
text or the translation has dropped a verifiable claim.

Design:
  · The English bullet is the canonical source — it has already passed
    grounder + adversarial + judge in the English domain (ADR-0015 §1)
  · Translation prompt explicitly lists tokens to preserve unchanged:
      - numbers (from BulletDecomposition.metrics)
      - JD keywords present in the bullet (from BulletDecomposition.jd_keywords)
      - proper nouns (capitalized-word heuristic)
  · After translation, we字面-verify each preservation token appears in the
    output. Missing tokens become warnings — UI surfaces them so the user
    can decide whether to accept anyway or re-translate.
  · No LLM key → no translation (fallthrough returns the English text with
    a warning). Caller decides what to do.

Output is a TranslatedVariant dataclass with:
  - text:        the translated bullet
  - language:    ISO code
  - preserved:   the tokens we required to survive
  - missing:     tokens that did NOT survive (warnings)
  - llm_used:    whether an LLM was actually called

This module is consumed by:
  · resume_writer (writes result to FactVariant.translated_json[language])
  · UI variant card (renders the user's chosen target language)
"""
from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Optional

from .bullet_decomposer import BulletDecomposition
from .llm import LLMConfigError, LLMError, chat

log = logging.getLogger(__name__)


# ─── Output dataclass ─────────────────────────────────────


@dataclass
class TranslatedVariant:
    text: str
    language: str
    preserved: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    """Subset of `preserved` that did NOT survive into `text`."""
    llm_used: bool = False
    warnings: list[str] = field(default_factory=list)

    def to_jsonable(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "TranslatedVariant":
        if not d:
            return cls(text="", language="")
        return cls(
            text=str(d.get("text", "")),
            language=str(d.get("language", "")),
            preserved=[str(p) for p in (d.get("preserved") or [])],
            missing=[str(m) for m in (d.get("missing") or [])],
            llm_used=bool(d.get("llm_used", False)),
            warnings=[str(w) for w in (d.get("warnings") or [])],
        )


# ─── Preservation extraction (deterministic) ──────────────


# 2+ Capitalized words in a row (or single Capitalized followed by digits).
# Catches: "Siemens", "Machine Learning", "ML pipeline" (captures ML),
# "Apache Spark", "Tencent Cloud".
_PROPER_NOUN_RE = re.compile(
    r"\b[A-Z][a-zA-Z0-9+]+(?:\s+[A-Z][a-zA-Z0-9+]+)*\b"
)
# Pure ALLCAPS acronyms 2-6 chars (AWS, GCP, ETL, ATS, KPI, etc.)
_ACRONYM_RE = re.compile(r"\b[A-Z]{2,6}\b")
# Numbers + units (reuse claim_grounder's pattern semantics — anything that
# looks like a metric we want to preserve字面). The trailing `\b` is omitted
# because % / x / K / M / B aren't word chars, breaking boundary detection.
# We use a negative-lookbehind on word chars to guard the head instead.
_NUMERIC_TOKEN_RE = re.compile(
    r"(?<!\w)\d+(?:[.,]\d+)?(?:\s*(?:%|x|K|M|B))?", re.I
)


def collect_preservation_tokens(
    bullet_text: str,
    decomposition: Optional[BulletDecomposition] = None,
) -> list[str]:
    """Return the unique list of tokens that MUST survive translation字面.

    Sources (in priority order, deduplicated case-sensitively):
      1. decomposition.metrics — numeric tokens already validated against fact
      2. decomposition.jd_keywords (in_bullet) — JD vocabulary the user wants ATS hit
      3. Proper nouns and acronyms in the bullet — Siemens, ML, Apache Spark
      4. Numbers / units that didn't make it into decomposition
    """
    seen: set[str] = set()
    out: list[str] = []

    def add(token: str) -> None:
        token = (token or "").strip()
        if not token or token in seen:
            return
        seen.add(token)
        out.append(token)

    if decomposition:
        for m in decomposition.metrics:
            if m.bullet_value:
                add(m.bullet_value)
        for jdk in decomposition.jd_keywords:
            if jdk.in_bullet and jdk.keyword:
                add(jdk.keyword)

    for m in _PROPER_NOUN_RE.finditer(bullet_text):
        add(m.group(0))
    for m in _ACRONYM_RE.finditer(bullet_text):
        add(m.group(0))
    for m in _NUMERIC_TOKEN_RE.finditer(bullet_text):
        add(m.group(0).strip())

    return out


def verify_preservation(translated_text: str, preserved: list[str]) -> list[str]:
    """Return the subset of `preserved` that did NOT survive verbatim into the
    translated text. Comparison is case-sensitive — a translated 'machine
    learning' (lowercased) does not satisfy a preserved 'Machine Learning'
    because resume ATS scanning is largely case-sensitive on technical terms.

    Single-letter or extremely common single tokens (length < 2) are skipped
    to avoid false-positive misses on Italian articles etc.
    """
    if not preserved:
        return []
    missing: list[str] = []
    for token in preserved:
        if not token or len(token) < 2:
            continue
        if token not in translated_text:
            missing.append(token)
    return missing


# ─── LLM translation ──────────────────────────────────────


TRANSLATE_SYSTEM = """You are a resume bullet translator.

Translate the bullet from English to the target language. CRITICAL RULES:

1. Preserve UNCHANGED — copy字面, do NOT translate or transliterate:
   - All numbers, percentages, units (e.g., "23%", "5K", "10x")
   - All proper nouns (companies, schools, products): "Siemens", "MIT"
   - Technical acronyms: "ML", "ETL", "AWS", "ATS", "KPI"
   - Brand-name technologies: "Apache Spark", "Kubernetes"

2. The user will provide an explicit list of "preserve" tokens — they MUST
   appear字面 in your output. If a preserve token is grammatically awkward
   in the target language, keep it anyway in the original form.

3. Use professional, concise resume register — not marketing copy, not
   instructional, not first-person narrative.

4. Match the action-verb tense convention of the target language for resumes
   (German: past participle / Italian: past simple / French: past simple).

5. Output JSON: {"translated": "..."}. No commentary."""


def _llm_translate(
    bullet_text: str,
    target_language: str,
    preserve: list[str],
    *,
    provider: str,
    api_key: str,
    model: str,
    errors: list[str] | None = None,
) -> Optional[str]:
    if not api_key or not target_language:
        return None
    preserve_block = "\n".join(f"  - {t}" for t in preserve) or "  (none)"
    payload = (
        f"## Source bullet (English)\n{bullet_text}\n\n"
        f"## Target language\n{target_language}\n\n"
        f"## Tokens to preserve字面 (do not translate)\n{preserve_block}"
    )
    try:
        resp = chat(
            provider=provider,
            api_key=api_key,
            model=model,
            system=TRANSLATE_SYSTEM,
            user_content=payload,
            max_tokens=400,
            temperature=0.3,
        )
    except LLMError as exc:
        # Parent-class catch covers both LLMConfigError (missing key) and
        # post-retry LLMError (transient 401/429/timeout). Skip translation
        # silently — pipeline continues with original text. Y2 (5-08): record
        # to errors collector if caller wants visibility.
        from .llm_diagnostics import record
        record(errors, "variant_translator", exc,
               context=f"target={target_language}")
        return None
    parsed = resp.parse_json(default={})
    translated = parsed.get("translated")
    if isinstance(translated, str) and translated.strip():
        return translated.strip()
    return None


# ─── Top-level ────────────────────────────────────────────


def translate_variant(
    bullet_text: str,
    target_language: str,
    *,
    decomposition: Optional[BulletDecomposition] = None,
    provider: str = "",
    api_key: str = "",
    model: str = "",
    errors: list[str] | None = None,
) -> TranslatedVariant:
    """Render `bullet_text` in `target_language`, preserving locale-invariant
    tokens (numbers / proper nouns / JD keywords from decomposition).

    Returns TranslatedVariant. If target_language is empty or "en" or matches
    a no-op, the result is the original bullet with language="en".

    No DB writes — pure function. Caller writes result to
    FactVariant.translated_json[target_language].
    """
    if not target_language or target_language.lower() in ("en", "english"):
        return TranslatedVariant(
            text=bullet_text,
            language="en",
            preserved=[],
            missing=[],
            llm_used=False,
        )

    preserve = collect_preservation_tokens(bullet_text, decomposition)

    if not api_key:
        return TranslatedVariant(
            text=bullet_text,
            language="en",  # fallback: still English
            preserved=preserve,
            missing=[],
            llm_used=False,
            warnings=[
                f"no api_key — translation to '{target_language}' skipped, returning English"
            ],
        )

    translated = _llm_translate(
        bullet_text, target_language, preserve,
        provider=provider, api_key=api_key, model=model,
        errors=errors,
    )

    if not translated:
        return TranslatedVariant(
            text=bullet_text,
            language="en",
            preserved=preserve,
            missing=[],
            llm_used=False,
            warnings=[
                f"LLM translation to '{target_language}' failed; returning English"
            ],
        )

    missing = verify_preservation(translated, preserve)
    warnings: list[str] = []
    if missing:
        warnings.append(
            f"{len(missing)} preservation token(s) lost in translation"
        )

    return TranslatedVariant(
        text=translated,
        language=target_language,
        preserved=preserve,
        missing=missing,
        llm_used=True,
        warnings=warnings,
    )
