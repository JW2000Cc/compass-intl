"""JD translation service.

Detects source language and translates JD text to a user-chosen target language
(remembered as a frequency-based preference across translations). Result is
cached on Job.description_translated to avoid re-spending LLM tokens.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path


from flask import current_app

from .llm import LLMError, chat

log = logging.getLogger(__name__)


# Languages user already understands — skip auto-prompt to translate
NATIVE_LANGUAGES = {"zh", "en"}

# Allowed target languages (UI dropdown options).
# Includes EU/intl coverage: tr (Turkish) added for international edition —
# AIS / job markets in Turkey use Turkish JDs heavily.
SUPPORTED_TARGETS = ["zh", "en", "it", "de", "fr", "es", "pt", "tr", "ja", "ko", "ru"]


def detect_language_simple(text: str) -> str:
    """Cheap language detection by character heuristics. No LLM call.

    Returns ISO 639-1 code: zh / en / it / de / fr / es / pt / ru / ja / ko / unknown.
    Conservative — defaults to "unknown" rather than guessing.
    """
    if not text or len(text) < 30:
        return "unknown"

    sample = text[:2000]

    # Chinese (CJK Unified Ideographs)
    cjk = len(re.findall(r"[一-鿿]", sample))
    if cjk > 30:
        return "zh"

    # Japanese (hiragana / katakana — distinct from Chinese)
    if re.search(r"[぀-ゟ゠-ヿ]", sample):
        return "ja"

    # Korean (hangul)
    if re.search(r"[가-힯]", sample):
        return "ko"

    # Russian (cyrillic)
    if re.search(r"[Ѐ-ӿ]", sample):
        return "ru"

    # Latin-script languages — use stopword frequency. Use word-boundary regex
    # rather than " word " literal so we catch line-start, punctuation-bound,
    # apostrophe-bound forms (e.g. "L'azienda" → "azienda").
    lower = sample.lower()

    def count(words):
        n = 0
        for w in words:
            n += len(re.findall(rf"(?<!\w){re.escape(w)}(?!\w)", lower))
        return n

    # Italian
    it_score = count([
        "di", "che", "una", "uno", "con", "per", "del", "della", "degli",
        "sono", "siamo", "ed", "ti", "ci", "lo", "la", "il", "non",
        "esperienza", "esperienze", "azienda", "aziende", "offriamo",
        "requisiti", "cercando", "conoscenza", "conoscenze", "stiamo",
    ])
    # German
    de_score = count([
        "und", "der", "die", "das", "den", "dem", "ein", "eine", "einen",
        "mit", "für", "von", "auf", "im", "ist", "sind", "wir",
        "erfahrung", "kenntnisse", "unternehmen", "stellenanzeige", "deutsch",
    ])
    # French
    fr_score = count([
        "et", "est", "des", "une", "un", "avec", "pour", "dans", "sur",
        "le", "la", "les", "du", "au", "vous", "nous",
        "expérience", "entreprise", "connaissance", "français",
    ])
    # Spanish
    es_score = count([
        "que", "los", "las", "una", "con", "para", "del", "por",
        "el", "la", "es", "un", "y", "se",
        "experiencia", "empresa", "conocimiento", "español",
    ])
    # Portuguese
    pt_score = count([
        "que", "uma", "com", "para", "do", "da", "de", "no", "na",
        "os", "as", "um", "e",
        "experiência", "empresa", "conhecimento", "português",
    ])
    # English
    en_score = count([
        "the", "and", "with", "for", "a", "of", "to", "in", "is",
        "are", "we", "you", "or",
        "experience", "company", "requirements", "skills",
    ])
    # Turkish — agglutinative + distinct diacritics (ş, ğ, ı, ö, ü, ç).
    # Score boost: every Turkish-specific char is worth ~1 token-equivalent
    # since they don't appear in en/de/fr/es/it/pt.
    tr_score = count([
        "ve", "ile", "için", "bir", "bu", "olan", "olarak",
        "şirket", "iş", "deneyim", "yetenek", "gereksinim",
        "aranıyor", "arıyoruz", "deneyime", "tecrübe",
    ]) + len(re.findall(r"[şğıİöüçŞĞÖÜÇ]", sample))

    scores = {
        "en": en_score, "it": it_score, "de": de_score,
        "fr": fr_score, "es": es_score, "pt": pt_score, "tr": tr_score,
    }
    best, score = max(scores.items(), key=lambda kv: kv[1])

    # 5-token threshold catches even short JDs (~50 words)
    if score < 5:
        return "unknown"
    return best


# Friendly names for the UI
LANGUAGE_NAMES = {
    "zh": "中文", "en": "English", "it": "Italiano", "de": "Deutsch",
    "fr": "Français", "es": "Español", "pt": "Português", "tr": "Türkçe",
    "ru": "Русский", "ja": "日本語", "ko": "한국어",
    "unknown": "未知语言",
}


def needs_translation(
    text: str,
    user_languages: list[str] | None = None,
) -> tuple[bool, str]:
    """Return (should_offer_translation, detected_lang).

    Translation is offered when:
      · detected language is NOT in `user_languages` (user can't read it), OR
      · the user hasn't declared what they read (None or empty list), OR
      · detection failed but the text is long enough to plausibly be a JD
        — let the user decide rather than unilaterally hiding the button.

    Args:
      text: the JD body
      user_languages: ISO codes the user can read (e.g. ["zh", "en"]).

        - `None` / `[]` → user hasn't declared their reading languages. Don't
           assume — offer the translation button so they can decide. Previously
           this fell back to a hardcoded {"zh","en"} default, which surprised
           anyone whose user_languages was empty (silent "where's the button?").
        - non-empty list → only offer when detected lang isn't in the list.
    """
    lang = detect_language_simple(text or "")

    if lang == "unknown":
        return (len(text or "") >= 100, lang)

    if not user_languages:
        # User hasn't declared — surface the button, let them decide.
        return (len(text or "") >= 100, lang)

    return (lang not in set(user_languages), lang)


def _system_prompt(target_lang: str) -> str:
    target_name = LANGUAGE_NAMES.get(target_lang, target_lang)
    return (
        f"You are a professional job-description translator. Translate the input JD into {target_name}.\n\n"
        "Rules:\n"
        "1. Preserve structure: headings, bullets, paragraph breaks must match the source.\n"
        "2. Keep technical terms in original form: stack names, product names, company names, "
        "acronyms (CTO, ML, SRE, etc.) stay as-is.\n"
        "3. Keep salary numbers and currencies in original format.\n"
        "4. Output translation only — no commentary, no preamble.\n"
        f"5. Be natural and idiomatic in {target_name}, but do not over-localize."
    )


# ────────────────────────────────────────────────────────────
# User translation language preferences
# ────────────────────────────────────────────────────────────


def _prefs_path() -> Path:
    return Path(current_app.config["SETTINGS"].data_dir) / "translation_prefs.json"


def _load_prefs() -> dict:
    p = _prefs_path()
    if not p.exists():
        return {"lang_counts": {}, "last_used": None}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"lang_counts": {}, "last_used": None}


def _save_prefs(d: dict) -> None:
    p = _prefs_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")


def record_translation_choice(target_lang: str) -> None:
    """Increment the count for this target lang. Cap at 999 to avoid runaway."""
    if target_lang not in SUPPORTED_TARGETS:
        return
    d = _load_prefs()
    counts = d.setdefault("lang_counts", {})
    counts[target_lang] = min(counts.get(target_lang, 0) + 1, 999)
    d["last_used"] = target_lang
    _save_prefs(d)


def get_preferred_target_lang(default: str = "zh") -> str:
    """Most-frequently-chosen target lang. Falls back to default if no history."""
    d = _load_prefs()
    counts = d.get("lang_counts") or {}
    if not counts:
        return default
    # Pick max count; tie-break with last_used preference
    best_lang = max(counts.keys(), key=lambda k: (counts[k], 1 if k == d.get("last_used") else 0))
    return best_lang


def get_translation_pref_summary() -> dict:
    """Return the prefs dict — UI can show how many times each lang has been used."""
    return _load_prefs()


def translate_jd(
    text: str,
    *,
    target_lang: str = "zh",
    provider: str,
    api_key: str,
    model: str,
) -> str:
    """Translate JD text to target language using configured LLM.

    Raises LLMError if the call fails.
    """
    if not text or not text.strip():
        return ""

    target_name = LANGUAGE_NAMES.get(target_lang, target_lang)
    user_msg = f"Translate the following job description to {target_name}:\n\n{text}"

    res = chat(
        provider=provider,
        api_key=api_key,
        model=model,
        system=_system_prompt(target_lang),
        user_content=user_msg,
        max_tokens=4000,
        temperature=0.3,
    )
    return res.text.strip()
