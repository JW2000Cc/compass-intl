"""User-controlled blacklist — companies + keywords to filter out at scrape time.

Stored in a flat JSON file (`<data_dir>/blacklist.json`) — deliberately NOT a
DB table, to avoid schema change after v1.0 lock. The blacklist is small,
hand-edited content; JSON file is the right shape.

Filter applied in scraper.merge_into_db: any incoming job whose company OR
description contains a blacklisted entry is dropped before persistence.

Borrowed concept from Job Tracker (v1) — proven need: after a week of use,
some companies / keywords become obvious noise (e.g. recurring agency
postings, keywords that always indicate spam). v1 had per-location +
global blacklist; v3 keeps just global (simpler; per-location can come
later if real need emerges).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


def _path(data_dir: Path) -> Path:
    return data_dir / "blacklist.json"


def load(data_dir: Path) -> dict:
    """Return {'companies': [...], 'keywords': [...]} — empty defaults if file missing."""
    p = _path(data_dir)
    if not p.exists():
        return {"companies": [], "keywords": []}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return {
            "companies": [c for c in (data.get("companies") or []) if isinstance(c, str)],
            "keywords":  [k for k in (data.get("keywords") or []) if isinstance(k, str)],
        }
    except Exception as exc:  # noqa: BLE001
        log.warning("Failed to load blacklist from %s: %s", p, exc)
        return {"companies": [], "keywords": []}


def save(data_dir: Path, *, companies: list[str], keywords: list[str]) -> None:
    """Persist blacklist. Normalizes (lowercase strip, dedup, sort)."""
    p = _path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    norm_companies = sorted({c.strip() for c in companies if c and c.strip()})
    norm_keywords = sorted({k.strip().lower() for k in keywords if k and k.strip()})
    p.write_text(
        json.dumps({"companies": norm_companies, "keywords": norm_keywords}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def from_natural_language(
    description: str,
    *,
    provider: str,
    api_key: str,
    model: str,
) -> dict:
    """LLM converts natural-language preference description into structured blacklist.

    Solves the 'user can't think of every keyword + every language' problem.
    User says "I don't want unpaid jobs / no MLM / no outsourcing companies" →
    LLM returns:
      {
        "companies": [],   (no specific companies named)
        "keywords": ["unpaid", "stage non retribuito", "mlm", "outsourcing", ...]
      }

    Always returns suggestions for review — never auto-applies.
    """
    if not api_key or not description.strip():
        return {"companies": [], "keywords": []}

    from .llm import LLMConfigError, LLMError, chat

    system = """You convert a user's natural-language description of "what kinds
of jobs they don't want" into a structured blacklist for a job-search tool.

The blacklist filters at scrape time using case-insensitive substring match
against company name and JD body. So you should output:

1. SPECIFIC company names the user mentioned (only if explicitly named)
2. KEYWORDS that, if present in JD body or company name, indicate the unwanted job

For each keyword, generate 2-4 multi-language variants (en/it/de/fr/zh) that
the same concept might appear under in different countries' postings.

Be CONSERVATIVE — better fewer strong keywords than many weak ones.
A keyword that appears in legitimate JDs (e.g. "junior" appears in non-spam
junior positions) should NOT be added. Only add keywords that strongly indicate
the unwanted category.

Output ONLY JSON:
{
  "companies": ["Acme Corp", ...],
  "keywords": ["unpaid", "stage non retribuito", "mlm", "Schneeballsystem", ...],
  "rationale": "<one short sentence explaining your choices>"
}"""

    try:
        resp = chat(
            provider=provider, api_key=api_key, model=model,
            system=system,
            user_content=f"User's description:\n{description.strip()[:1500]}",
            max_tokens=600,
        )
    except LLMError:
        return {"companies": [], "keywords": []}
    except Exception as exc:  # noqa: BLE001
        log.warning("from_natural_language failed: %s", exc)
        return {"companies": [], "keywords": []}

    parsed = resp.parse_json(default={})
    if not isinstance(parsed, dict):
        return {"companies": [], "keywords": []}

    return {
        "companies": [c for c in (parsed.get("companies") or []) if isinstance(c, str)][:30],
        "keywords":  [k for k in (parsed.get("keywords") or [])  if isinstance(k, str)][:50],
        "rationale": str(parsed.get("rationale", ""))[:300],
    }


def effectiveness_audit(session, *, days: int = 30) -> dict:
    """Reflective layer: how well is the blacklist actually performing?

    Counts:
      - blocked_total: jobs filtered out by blacklist in last N days
      - tier_classifier_overlap: of blocked jobs, how many would have been
        classified Tier 4/5 anyway (= LLM was going to silence them).
        Higher overlap = blacklist is redundant; lower = blacklist is doing
        unique work the LLM missed.
      - unique_value: blocked jobs that LLM would NOT have silenced
        (= blacklist doing real work). Reverse: companies repeatedly judged
        Tier 5 → suggest adding to blacklist (since LLM is paying tokens
        each time to do what blacklist could do for free).

    This makes the blacklist part of Compass's reflective loop — users see
    if their rules are doing real work or are just superstition.
    """
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import select
    from ..models import Job, JobMatch, ReflectionEvent

    since = datetime.now(timezone.utc) - timedelta(days=days)

    # Blocked-by-blacklist count from external_scrape events (we record blocked count there)
    scrape_events = session.execute(
        select(ReflectionEvent).where(
            ReflectionEvent.kind == "external_scrape",
            ReflectionEvent.created_at >= since,
        )
    ).scalars().all()
    blocked_total = sum(
        (e.payload_json or {}).get("blocked", 0) for e in scrape_events
    )

    # Companies repeatedly judged Tier 5 (= would block again next time)
    tier5_companies: dict[str, int] = {}
    rows = session.execute(
        select(Job, JobMatch)
        .join(JobMatch)
        .where(JobMatch.tier == 5, Job.created_at >= since)
    ).all()
    for j, _ in rows:
        c = (j.company or "").strip()
        if c:
            tier5_companies[c] = tier5_companies.get(c, 0) + 1

    # Companies seen 3+ times at Tier 5 → strong signal to add to blacklist
    repeat_offenders = sorted(
        [(c, n) for c, n in tier5_companies.items() if n >= 3],
        key=lambda x: -x[1],
    )[:10]

    return {
        "blocked_total": blocked_total,
        "blocked_by_kind": {},  # could be expanded later (blocked-by-company vs by-keyword)
        "repeat_tier5_companies": [
            {"company": c, "count": n} for c, n in repeat_offenders
        ],
        "lookback_days": days,
    }


def is_blacklisted(
    company: Optional[str],
    description: Optional[str],
    *,
    blacklist: dict,
) -> tuple[bool, str]:
    """Return (True, reason) if job hits any blacklist entry. Reason for logging.

    - Company match: case-insensitive substring
    - Keyword match: case-insensitive whole-word in description (or company)
    """
    if not blacklist:
        return False, ""

    company_norm = (company or "").lower()
    desc_norm = (description or "").lower()

    for c in blacklist.get("companies") or []:
        c_norm = c.lower()
        if c_norm and c_norm in company_norm:
            return True, f"blacklisted company: {c}"

    for k in blacklist.get("keywords") or []:
        k_norm = k.lower()
        if k_norm and (k_norm in desc_norm or k_norm in company_norm):
            return True, f"blacklisted keyword: {k}"

    return False, ""
