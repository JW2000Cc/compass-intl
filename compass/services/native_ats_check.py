"""Deterministic ATS keyword coverage check in the JD's native language.

Per ADR-0015 (跨语言场景下 grounder 失效那一节):
  · LLM judge 在英文母版上跑 grounding / adversarial / role-judge — 那是语义层
  · 但用户实际投出去的是目标语言的简历，被目标语言 ATS 字面扫
  · 字面命中跟语义评分是两个不同维度，不要平均

This module is the字面 layer. No LLM. Deterministic, fast, language-agnostic
in mechanism (works for it / de / fr / zh — anything where word boundaries
and case are meaningful).

Design notes:
  · We extract *token candidates* from the JD using simple heuristics
    (length, casing, multiword proper-noun-like sequences). This is
    intentionally crude — ATS systems do字面 matching too, so over-engineering
    semantic NER would diverge from what an ATS actually scans.
  · Match strategy is case-insensitive exact + small-edit-distance fuzzy
    (Levenshtein ≤ 2 for tokens length ≥ 6, exact only otherwise — short
    tokens are too easy to false-match).
  · Stop-words are language-pluggable (lazy: a small built-in IT/EN list).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# Tokens shorter than this won't be considered keyword candidates from JD.
_MIN_TOKEN_LEN = 4

# Stop-words shared across IT/EN. Coverage doesn't need to be exhaustive —
# the ATS itself doesn't care about "the/and"; we just don't want them
# polluting our keyword candidate list.
_STOP_WORDS = frozenset(
    {
        # English
        "with", "from", "this", "that", "into", "such", "your", "must",
        "have", "will", "been", "were", "what", "when", "they", "their",
        "these", "those", "would", "could", "should", "shall", "about",
        "above", "after", "again", "below", "between", "during", "other",
        "some", "than", "then", "there", "where", "which", "while", "more",
        # Italian
        "della", "delle", "dello", "degli", "dalla", "dalle", "dallo",
        "dagli", "nella", "nelle", "nello", "negli", "alla", "alle", "allo",
        "agli", "sulla", "sulle", "sullo", "sugli", "questo", "questa",
        "questi", "queste", "quello", "quella", "quelli", "quelle", "essere",
        "avere", "fare", "anche", "ancora", "dopo", "prima", "molto", "poco",
        "tanto", "solo", "loro", "noi", "voi", "lui", "lei", "ogni", "stesso",
        "stessa", "tutto", "tutta", "tutti", "tutte", "come", "dove", "quando",
        "perché", "perche", "perchè", "mentre", "tale", "quale", "quali",
        "alcuni", "alcune", "qualche", "altro", "altra", "altri", "altre",
        # German
        "und", "oder", "aber", "wenn", "dann", "diese", "dieser", "dieses",
        "diesen", "kann", "können", "wird", "werden", "ist", "sind", "war",
        "waren", "haben", "hatte", "hatten", "sich", "auch", "noch", "über",
        "unter", "nach", "vor", "bei", "mit", "ohne", "durch", "gegen",
        # French
        "avec", "sans", "pour", "dans", "sous", "sur", "vers", "chez",
        "cette", "cette", "ces", "celui", "celle", "ceux", "celles", "leur",
        "leurs", "notre", "nos", "votre", "vos", "tout", "toute", "tous",
        "toutes", "même", "meme", "très", "tres", "aussi", "encore", "déjà",
        "deja", "alors", "ainsi", "donc", "puis", "ensuite", "comme",
    }
)

# Multi-word proper-noun-like sequence: 2+ Capitalized words in a row.
# Catches: "Machine Learning", "Apache Spark", "Università di Milano".
_MULTIWORD_CAP_RE = re.compile(
    r"\b[A-ZÀ-Ý][a-zà-ÿ]+(?:\s+(?:di|de|del|della|of|the|and|e)\s+|\s+)[A-ZÀ-Ý][a-zà-ÿ]+(?:\s+[A-ZÀ-Ý][a-zà-ÿ]+)*\b"
)

# Single ALLCAPS / mixed-case acronyms: 2-6 chars (ATS, AI, ML, KPI, ETL, AWS, GCP)
_ACRONYM_RE = re.compile(r"\b[A-Z]{2,6}\b")

# Tokens that are clearly technical (mixed case with special chars or numbers):
# Python3, Node.js, K8s, C++, .NET — too varied to regex perfectly, fallback to
# generic word extraction below.

_WORD_RE = re.compile(r"\b[\w][\w\.\-+]{2,}\b", re.UNICODE)


# ─────────────────────────────────────────────────────────


@dataclass
class NativeATSReport:
    """Coverage of JD-native-language keywords in the resume.

    coverage_ratio = matched / total_candidates  (0.0–1.0)
    total / matched / missing are token strings as they appeared in the JD.
    """

    total: int
    matched: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    coverage_ratio: float = 0.0
    multi_word_keywords: list[str] = field(default_factory=list)


def extract_jd_keywords_native(jd_text: str) -> list[str]:
    """Extract字面 keyword candidates from the JD without translating.

    Returns a deduplicated, ordered list of tokens an ATS is likely to scan for.
    Lower-cases for stop-word filtering but preserves the original casing in
    the returned list so the report shows the user "what the JD literally said".
    """
    if not jd_text or not jd_text.strip():
        return []

    seen: set[str] = set()
    out: list[str] = []

    # 1) Multi-word capitalized sequences first (highest signal: technical
    #    names, schools, frameworks, "Machine Learning Engineer")
    for m in _MULTIWORD_CAP_RE.finditer(jd_text):
        token = m.group(0).strip()
        key = token.lower()
        if key not in seen:
            seen.add(key)
            out.append(token)

    # 2) Acronyms (single token, 2-6 caps)
    for m in _ACRONYM_RE.finditer(jd_text):
        token = m.group(0)
        key = token.lower()
        if key not in seen:
            seen.add(key)
            out.append(token)

    # 3) Generic words ≥ MIN_TOKEN_LEN, non-stop-word
    for m in _WORD_RE.finditer(jd_text):
        token = m.group(0)
        if len(token) < _MIN_TOKEN_LEN:
            continue
        key = token.lower()
        if key in _STOP_WORDS:
            continue
        if key in seen:
            continue
        # Skip pure numbers (handled by drift check elsewhere)
        if token.replace(".", "").replace("-", "").isdigit():
            continue
        seen.add(key)
        out.append(token)

    return out


def _levenshtein(a: str, b: str) -> int:
    """Tiny pure-python Levenshtein. Sufficient for short tokens."""
    if a == b:
        return 0
    if len(a) < len(b):
        a, b = b, a
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        curr = [i]
        for j, cb in enumerate(b, 1):
            ins = curr[j - 1] + 1
            dele = prev[j] + 1
            sub = prev[j - 1] + (0 if ca == cb else 1)
            curr.append(min(ins, dele, sub))
        prev = curr
    return prev[-1]


def _token_present(token: str, haystack_lower: str) -> bool:
    """Token hits if exact substring (case-insensitive) OR for tokens ≥ 6 chars,
    fuzzy substring within Levenshtein ≤ 2 of any whitespace-bounded word.

    For multi-word tokens longer than 2 words: also try every contiguous
    2-word sub-phrase. Rationale: JD multi-word extraction may inadvertently
    include leading sentence verbs (e.g. "Cerchiamo Machine Learning Engineer"),
    but the resume only needs to hit "Machine Learning" — that's how ATS字面
    scanning works in practice.
    """
    needle = token.lower()
    if needle in haystack_lower:
        return True
    # Multi-word: try contiguous 2-grams (covers leading-verb pollution)
    words = needle.split()
    if len(words) > 2:
        for i in range(len(words) - 1):
            sub = f"{words[i]} {words[i + 1]}"
            if sub in haystack_lower:
                return True
    if len(needle) < 6:
        return False  # short tokens require exact — too risky to fuzzy
    if " " in needle:
        return False  # multi-word fuzzy is over-permissive — exact only
    # Fuzzy fallback: scan each word in haystack
    for word in re.findall(r"\b[\w][\w\.\-+]{4,}\b", haystack_lower):
        if _levenshtein(needle, word) <= 2:
            return True
    return False


def native_ats_check(
    resume_text: str,
    jd_text: str,
) -> NativeATSReport:
    """Compute字面 keyword coverage of resume against JD's native-language tokens.

    See module docstring for design rationale. Returns NativeATSReport.
    """
    keywords = extract_jd_keywords_native(jd_text)
    if not keywords:
        return NativeATSReport(total=0, coverage_ratio=0.0)

    resume_lower = (resume_text or "").lower()
    matched: list[str] = []
    missing: list[str] = []
    multi_word: list[str] = []
    for kw in keywords:
        if " " in kw:
            multi_word.append(kw)
        if _token_present(kw, resume_lower):
            matched.append(kw)
        else:
            missing.append(kw)

    return NativeATSReport(
        total=len(keywords),
        matched=matched,
        missing=missing,
        coverage_ratio=len(matched) / len(keywords) if keywords else 0.0,
        multi_word_keywords=multi_word,
    )
