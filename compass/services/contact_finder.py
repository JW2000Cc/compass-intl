"""Cold-path contact discovery — find people at a target company via public web search.

Ported from v1's hr_finder.py (Job Tracker), modernized:
  - Multi-engine via the `ddgs` library (DDG/Bing/Brave/Yahoo unified) instead
    of hand-written BeautifulSoup parsers per engine.
  - Same proven 3-round strategy (HR-targeted → dept peers → broad fallback).
  - Same company-name normalization (strips Inc/AG/GmbH/etc).
  - Same 30-day per-company cache.
  - Same "current employee" verification via `at <company>` regex.

This is the COLD path. The WARM path (people user already knows) is
services/connections.py and runs first in the contacts panel UI.
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_CACHE_TTL_HOURS = 24 * 30   # 30 days

_LI_SLUG_RE = re.compile(r"linkedin\.com/in/([\w\-]+)")

# Strip common legal suffixes so "at Siemens" matches "Siemens AG"
_SUFFIX_RE = re.compile(
    r"\s+(Inc\.?|Corp\.?|LLC|Ltd\.?|GmbH|AG|S\.A\.?|S\.p\.A\.?|B\.V\.?|N\.V\.?"
    r"|PLC|SE|KG|OHG|Co\.?|Group|Holdings|International|Worldwide)\.?$",
    re.IGNORECASE,
)


def _normalize_company(name: str) -> str:
    return _SUFFIX_RE.sub("", (name or "").strip()).strip()


# ── Job category → department search terms ──────────────────────────────────

_JOB_CATEGORY_MAP: dict[str, tuple[list[str], str]] = {
    "software": (
        ["software", "developer", "backend", "frontend", "fullstack", "devops",
         "ios", "android", "mobile", "web", "sre", "platform", "infrastructure"],
        '"software engineer" OR "developer" OR "engineer"',
    ),
    "data": (
        ["data scientist", "data analyst", "data engineer", "machine learning",
         "ml engineer", "ai engineer", "analytics", "nlp", "deep learning"],
        '"data" OR "machine learning" OR "analytics" OR "data scientist"',
    ),
    "finance": (
        ["finance", "financial", "investment", "banking", "accounting",
         "controller", "treasurer", "cfo", "audit", "tax"],
        '"finance" OR "financial analyst" OR "investment" OR "banking"',
    ),
    "consulting": (
        ["consultant", "consulting", "advisory", "strategy"],
        '"consultant" OR "consulting" OR "strategy"',
    ),
    "marketing": (
        ["marketing", "brand", "growth", "content", "seo", "communications"],
        '"marketing" OR "brand" OR "growth"',
    ),
    "product": (
        ["product manager", "product owner", "pm ", " pm", "product lead"],
        '"product manager" OR "product"',
    ),
    "design": (
        ["designer", "ux ", " ux", "ui ", " ui", "user experience"],
        '"designer" OR "UX" OR "UI"',
    ),
    "sales": (
        ["sales", "account executive", "account manager", "business development"],
        '"sales" OR "account executive" OR "business development"',
    ),
    "operations": (
        ["operations", "ops", "supply chain", "logistics", "procurement"],
        '"operations" OR "supply chain"',
    ),
    "legal": (
        ["legal", "lawyer", "attorney", "counsel", "compliance"],
        '"legal" OR "compliance" OR "counsel"',
    ),
    "research": (
        ["research scientist", "researcher", "research engineer", "scientist", "phd"],
        '"researcher" OR "scientist"',
    ),
}

_HR_TITLE_KEYWORDS = [
    "recruit", "talent", "hr ", " hr", "human resource", "people partner",
    "hiring", "staffing", "talent acquisition", "people ops", "人力资源",
]


def _detect_job_category(job_title: str) -> Optional[str]:
    if not job_title:
        return None
    t = job_title.lower()
    for category, (keywords, _) in _JOB_CATEGORY_MAP.items():
        if any(kw in t for kw in keywords):
            return category
    return None


def _build_hr_query(company: str) -> str:
    return (
        f'site:linkedin.com/in/ "at {company}" '
        '(recruiter OR "talent acquisition" OR "HR" OR "human resources" '
        'OR "recruiting" OR "hiring manager" OR "staffing")'
    )


def _build_dept_query(company: str, job_title: str) -> Optional[str]:
    category = _detect_job_category(job_title)
    if not category:
        return None
    _, search_terms = _JOB_CATEGORY_MAP[category]
    return f'site:linkedin.com/in/ "at {company}" ({search_terms})'


def _build_broad_query(company: str) -> str:
    return f'site:linkedin.com/in/ "at {company}"'


def _hr_score(contact: dict) -> int:
    title = (contact.get("title") or "").lower()
    return sum(1 for kw in _HR_TITLE_KEYWORDS if kw in title)


# ── Cache ───────────────────────────────────────────────────────────────────


def _data_dir() -> Path:
    try:
        from flask import current_app
        return current_app.config["SETTINGS"].data_dir
    except Exception:
        return Path("data")


def _cache_dir() -> Path:
    p = _data_dir() / "contact_cache"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _cache_path(company: str) -> Path:
    safe = re.sub(r"[^\w\- ]", "", company).strip().replace(" ", "_")[:60]
    return _cache_dir() / f"{safe}.json"


def _load_cache(company: str) -> Optional[list[dict]]:
    p = _cache_path(company)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        cached_at = datetime.fromisoformat(data["cached_at"])
        if cached_at.tzinfo is None:
            cached_at = cached_at.replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc) - cached_at
        if age < timedelta(hours=_CACHE_TTL_HOURS):
            log.info(f"[{company}] cache hit ({len(data['contacts'])} contacts)")
            return data["contacts"]
    except Exception as e:
        log.warning(f"cache read error: {e}")
    return None


def _save_cache(company: str, contacts: list[dict]) -> None:
    _cache_path(company).write_text(json.dumps({
        "cached_at": datetime.now(timezone.utc).isoformat(),
        "contacts": contacts,
    }, ensure_ascii=False, indent=2), encoding="utf-8")


# ── Search via ddgs ─────────────────────────────────────────────────────────


def _parse_name_title(text: str) -> tuple[str, str]:
    """Split LinkedIn search result title 'Name - Title at Company' into (name, title)."""
    text = re.sub(r"\s*\|.*$", "", text or "").strip()
    for sep in (" - ", " – ", " · "):
        if sep in text:
            parts = text.split(sep, 1)
            name = parts[0].strip()
            title = parts[1].strip() if len(parts) > 1 else ""
            if name and len(name) < 80 and not name.islower():
                return name, title
    return "", ""


def _name_from_slug(slug: str) -> str:
    parts = slug.split("-")
    clean = [p for p in parts if not (len(p) > 6 and re.search(r"\d", p) and re.search(r"[a-z]", p))]
    return " ".join(p.capitalize() for p in (clean or parts)[:3])


def _run_search(query: str, max_results: int = 25) -> list[dict]:
    """Run the query through ddgs (multi-engine internally with retry).

    Returns list of {linkedin_url, name, title, snippet}.
    Empty list on any failure — caller decides fallback.
    """
    try:
        from ddgs import DDGS
    except ImportError:
        log.error("ddgs not installed — pip install ddgs")
        return []

    try:
        results: list[dict] = []
        seen: set[str] = set()
        with DDGS() as engine:
            for hit in engine.text(query, max_results=max_results, region="wt-wt"):
                url = hit.get("href") or hit.get("link") or ""
                m = _LI_SLUG_RE.search(url)
                if not m:
                    continue
                slug = m.group(1)
                if slug in seen:
                    continue
                seen.add(slug)
                title_full = hit.get("title", "")
                snippet = hit.get("body", "") or hit.get("snippet", "")
                name, title = _parse_name_title(title_full)
                results.append({
                    "linkedin_url": f"https://www.linkedin.com/in/{slug}",
                    "name": name,
                    "title": title,
                    "snippet": snippet,
                })
        return results
    except Exception as e:
        log.warning(f"ddgs search failed: {e}")
        return []


# ── Verification ────────────────────────────────────────────────────────────


def _is_current_employee(contact: dict, company: str) -> bool:
    """Confirm the LinkedIn snippet/title says the person currently works at `company`.

    Negative lookahead defends against false positives like
      "at Google" wrongly matching "at Google DeepMind".
    """
    haystack = ((contact.get("title") or "") + " " + (contact.get("snippet") or "")).lower()
    if not haystack.strip():
        return False
    candidates = {
        company.lower().strip(),
        _normalize_company(company).lower().strip(),
    }
    for name in candidates:
        if not name:
            continue
        pattern = re.escape(f"at {name}") + r"(?!['’]|\s+[a-zA-Z])"
        if re.search(pattern, haystack):
            return True
    return False


# ── Public API ──────────────────────────────────────────────────────────────


_ROUND_DELAY = 2.0  # seconds between search rounds (gentle on engines)


def find_contacts(
    company: str,
    *,
    job_title: str = "",
    max_results: int = 5,
    use_cache: bool = True,
) -> list[dict]:
    """Find current-employee contacts at `company` via public web search.

    Strategy: 3 rounds in priority order. Cached 30 days per company.
        Round 1: HR / recruiter targeted query
        Round 2: Department peers (only if job_title is recognised)
        Round 3: Broad fallback, HR-scored ranking (only if rounds 1+2 short)

    Returns list of dicts with linkedin_url, name, title, snippet.
    """
    if not company or not company.strip():
        return []

    company = company.strip()
    if use_cache:
        cached = _load_cache(company)
        if cached is not None:
            return cached

    log.info(f"[{company}] discovering contacts (job_title={job_title!r})…")
    seen_slugs: set[str] = set()

    def _confirmed(raw: list[dict]) -> list[dict]:
        out = []
        for c in raw:
            slug = c["linkedin_url"].split("/in/")[-1].rstrip("/")
            if slug in seen_slugs:
                continue
            if _is_current_employee(c, company):
                seen_slugs.add(slug)
                out.append(c)
        return out

    # Round 1: HR / recruiter
    r1 = _run_search(_build_hr_query(company), max_results=20)
    hr_contacts = _confirmed(r1)
    log.info(f"  Round 1 (HR): {len(r1)} raw → {len(hr_contacts)} confirmed")

    # Round 2: department peers
    dept_contacts: list[dict] = []
    dept_query = _build_dept_query(company, job_title) if job_title else None
    if dept_query:
        time.sleep(_ROUND_DELAY)
        r2 = _run_search(dept_query, max_results=20)
        dept_contacts = _confirmed(r2)
        category = _detect_job_category(job_title)
        log.info(f"  Round 2 (dept [{category}]): {len(r2)} raw → {len(dept_contacts)} confirmed")

    # Round 3: broad fallback (only if still short)
    broad_contacts: list[dict] = []
    if len(hr_contacts) + len(dept_contacts) < max_results:
        time.sleep(_ROUND_DELAY)
        r3 = _run_search(_build_broad_query(company), max_results=25)
        broad_all = _confirmed(r3)
        broad_contacts = sorted(broad_all, key=lambda c: -_hr_score(c))
        log.info(f"  Round 3 (broad): {len(r3)} raw → {len(broad_contacts)} confirmed")
    else:
        log.info(f"  Round 3: skipped (already {len(hr_contacts)+len(dept_contacts)} contacts)")

    contacts = (hr_contacts + dept_contacts + broad_contacts)[:max_results]

    # Backfill missing names from URL slug
    for c in contacts:
        if not c.get("name"):
            slug = c["linkedin_url"].split("/in/")[-1].rstrip("/")
            c["name"] = _name_from_slug(slug)

    if contacts:
        _save_cache(company, contacts)
    log.info(f"[{company}] returned {len(contacts)} contacts (cached 30d)")
    return contacts


def clear_cache(company: Optional[str] = None) -> int:
    """Clear cache for one company, or all if company is None. Returns count cleared."""
    cd = _cache_dir()
    if company is None:
        files = list(cd.glob("*.json"))
    else:
        p = _cache_path(company)
        files = [p] if p.exists() else []
    n = 0
    for f in files:
        try:
            f.unlink()
            n += 1
        except OSError:
            pass
    return n
