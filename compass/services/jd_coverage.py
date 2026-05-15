"""JD keyword coverage — borrowed from Resume-Matcher.

Computes which JD keywords appear in user's facts (hit) vs only in JD (miss),
and produces an HTML rendering of the JD with those keywords highlighted in
two distinct colors. Strictly read-only; no LLM calls; no DB writes.

Why server-side instead of JS:
    - we need word-boundary matching that accounts for accented chars / CJK
    - we already have the data on render → pass via context
    - HTML escaping must happen before substitution (security)
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from markupsafe import Markup, escape


@dataclass
class CoverageResult:
    """Outcome of comparing JD keywords against fact texts.

    `hit_keywords`   — present in JD AND in at least one fact
    `miss_keywords`  — present in JD but no fact match
    `coverage_pct`   — round(100 * hit / total), 0 when no keywords
    `total`          — total unique keywords scanned
    """
    hit_keywords: list[str]
    miss_keywords: list[str]
    coverage_pct: int
    total: int


def _norm(s: str) -> str:
    return s.strip().lower()


def _keyword_in_corpus(kw: str, corpus_lower: str) -> bool:
    """Match a keyword inside a lowercased corpus.

    Uses word-boundary regex for ASCII-only keywords; falls back to plain
    substring search when the keyword contains non-ASCII chars (CJK / accents),
    where \\b doesn't behave intuitively.
    """
    kw = kw.strip()
    if not kw:
        return False
    if kw.isascii() and re.match(r"^[\w\s\-/+#.&]+$", kw):
        pattern = r"(?<![\w]){}(?![\w])".format(re.escape(kw.lower()))
        return re.search(pattern, corpus_lower) is not None
    return kw.lower() in corpus_lower


def compute_coverage(
    keywords: Iterable[str], fact_texts: Iterable[str]
) -> CoverageResult:
    """Return hit / miss / coverage % for JD keywords against fact texts."""
    seen: set[str] = set()
    ordered: list[str] = []
    for k in keywords or []:
        n = _norm(k)
        if n and n not in seen:
            seen.add(n)
            ordered.append(k.strip())

    if not ordered:
        return CoverageResult(hit_keywords=[], miss_keywords=[], coverage_pct=0, total=0)

    corpus = " \n ".join(t for t in fact_texts if t).lower()
    hit, miss = [], []
    for kw in ordered:
        if _keyword_in_corpus(kw, corpus):
            hit.append(kw)
        else:
            miss.append(kw)

    pct = round(100 * len(hit) / len(ordered)) if ordered else 0
    return CoverageResult(
        hit_keywords=hit, miss_keywords=miss, coverage_pct=pct, total=len(ordered)
    )


def render_jd_with_highlights(
    jd_text: str, hit_keywords: list[str], miss_keywords: list[str]
) -> Markup:
    """HTML-escape JD then wrap each keyword occurrence in a colored mark.

    - hits  → <mark class="kw-hit"> (绿,我有)
    - miss  → <mark class="kw-miss"> (橙,JD 要但我没)

    Sort keywords by length DESC to avoid "ML" eating "Machine Learning":
    longer multi-word phrases are matched first, then shorter sub-tokens.
    Each keyword is replaced at most once per occurrence; nested matches are
    avoided by inserting a placeholder before all replacements then expanding.
    """
    if not jd_text:
        return Markup("")

    safe = str(escape(jd_text))

    # Sort longest-first to prefer multi-word matches
    items: list[tuple[str, str]] = []
    for kw in hit_keywords:
        items.append((kw, "kw-hit"))
    for kw in miss_keywords:
        items.append((kw, "kw-miss"))
    items.sort(key=lambda p: len(p[0]), reverse=True)

    # Use unique placeholders so later regex passes don't re-match inside
    # already-marked spans.
    placeholders: dict[str, str] = {}
    out = safe
    for idx, (kw, cls) in enumerate(items):
        if not kw.strip():
            continue
        kw_escaped = str(escape(kw))
        # Same word-boundary logic as compute_coverage
        if kw.isascii() and re.match(r"^[\w\s\-/+#.&]+$", kw):
            pattern = r"(?<![\w])" + re.escape(kw_escaped) + r"(?![\w])"
        else:
            pattern = re.escape(kw_escaped)
        token = f"\x00KW{idx}\x00"
        new_out, n = re.subn(pattern, token, out, flags=re.IGNORECASE)
        if n > 0:
            placeholders[token] = (
                f'<mark class="{cls}" title="'
                f'{"已在简历中" if cls == "kw-hit" else "JD 要求但简历未提及"}">'
                f"{kw_escaped}</mark>"
            )
            out = new_out

    for token, html in placeholders.items():
        out = out.replace(token, html)

    return Markup(out)
