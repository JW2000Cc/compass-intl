"""LLM-driven keyword alias prediction (multi-language + same-lang variants).

When the user adds a search keyword (e.g. "数据分析师"), this service asks the
LLM to propose 5-8 multi-language variants the user can opt in to. Aliases
broaden SEARCH coverage (different languages on LinkedIn / Indeed) — they do
NOT affect the LLM-based matcher (which reads JD semantically).

Design principle (from Job Mate ADR-0003): never auto-add aliases.
User is always the gatekeeper.

Why this exists at all (over "let user write multi-lang manually"):
  - User in Italy types "数据分析师" → LinkedIn Italy indexes by "analista
    dei dati" → searches return ~0 results.
  - Without alias expansion, the entire scrape pipeline starts broken.
  - Borrowed from Job Mate's keyword_alias.py — proven design, port unchanged.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from .llm import LLMConfigError, LLMError, chat

log = logging.getLogger(__name__)


PREDICT_SYSTEM = """You suggest multi-language search-keyword aliases for a job-search tool.

Goal: when the user enters a CANONICAL keyword (e.g. "Data Analyst") in their
target language, propose terms that mean the SAME ROLE in other languages and
common variants in the SAME language. These are used as additional search
queries against LinkedIn/Indeed/etc. to broaden coverage.

Output ONLY a JSON array of objects:
[
  {"alias": "...", "language": "en|it|fr|de|zh|es|ja|pt|nl|ko", "kind": "translation|variant|seniority|adjacent", "rationale": "<≤15 words>"},
  ...
]

Rules:
- 5-8 aliases total (NOT more — keyword stuffing hurts search quality)
- Cover the candidate's likely target regions (use `regions` hint if given)
- Include 2-3 SAME-language variants (junior/senior, abbreviations, related titles)
- Mark `kind`:
    translation: same role, different language ("Datenanalyst" for "Data Analyst")
    variant: same language, different phrasing ("BI Analyst" for "Data Analyst")
    seniority: junior/senior version of the same role
    adjacent: related role the user might also fit (mark with rationale)
- Skip duplicate, near-duplicate, or trivially-different ones
- For Chinese: prefer the most-common form on Chinese job boards (e.g. 数据分析师 not 数据分析员)
- Be conservative: better to suggest 5 strong aliases than 10 weak ones
"""


@dataclass
class Alias:
    alias: str
    language: str
    kind: str  # translation | variant | seniority | adjacent
    rationale: str


def predict_aliases(
    canonical: str,
    *,
    target_regions: Optional[list[str]] = None,
    user_languages: Optional[list[str]] = None,
    provider: str = "claude",
    api_key: str = "",
    model: str = "",
) -> list[Alias]:
    """Ask LLM to propose aliases. Empty list if no key or LLM unreachable."""
    if not api_key:
        return []
    canonical = (canonical or "").strip()
    if not canonical:
        return []

    user_msg_parts = [f"Canonical keyword: {canonical}"]
    if target_regions:
        user_msg_parts.append(f"Target regions: {', '.join(target_regions)}")
    if user_languages:
        user_msg_parts.append(f"User speaks: {', '.join(user_languages)}")

    try:
        resp = chat(
            provider=provider,
            api_key=api_key,
            model=model,
            system=PREDICT_SYSTEM,
            user_content="\n".join(user_msg_parts),
            max_tokens=800,
        )
    except LLMError as exc:
        log.warning("alias prediction LLM config error: %s", exc)
        return []
    except Exception as exc:  # noqa: BLE001
        log.warning("alias prediction failed: %s", exc)
        return []

    parsed = resp.parse_json(default=[])
    if not isinstance(parsed, list):
        return []

    out: list[Alias] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        alias_text = str(item.get("alias", "")).strip()
        if not alias_text or alias_text.lower() == canonical.lower():
            continue  # skip duplicates of the canonical
        out.append(Alias(
            alias=alias_text[:200],
            language=str(item.get("language", "en"))[:8],
            kind=str(item.get("kind", "translation"))[:20],
            rationale=str(item.get("rationale", ""))[:200],
        ))
    return out[:8]  # cap to 8 even if LLM ignored "5-8 total" rule
