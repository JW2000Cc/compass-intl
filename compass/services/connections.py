"""Warm-path: people the user already knows.

Source: LinkedIn `Connections.csv` (Settings → Get a copy of your data).
Stored in the Connection table.

Query: given a job's company name, find which connections currently work
there (via fuzzy company-name match — "Foobar Inc" ≈ "Foobar").

This is the WARM layer in the contacts panel — runs first, before the cold
contact_finder, because warm outreach has 15-30× the response rate.
"""
from __future__ import annotations

import csv
import io
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Connection

log = logging.getLogger(__name__)


# ── CSV parsing ─────────────────────────────────────────────────────────────


# LinkedIn's Connections.csv has these columns (exact names per 2024+ exports):
#   First Name, Last Name, URL, Email Address, Company, Position, Connected On
# Earlier exports may have different casing or fewer columns. Be permissive.

_COL_FIRST = ("first name", "firstname", "first")
_COL_LAST = ("last name", "lastname", "last", "surname")
_COL_URL = ("url", "profile url", "linkedin url", "linkedin")
_COL_EMAIL = ("email address", "email", "email_address")
_COL_COMPANY = ("company", "current company", "organization", "current_company")
_COL_POSITION = ("position", "title", "current position", "current_title", "job title")
_COL_CONNECTED = ("connected on", "connected_on", "connection date", "date connected")


def _normalize_header(h: str) -> str:
    return h.strip().lower().lstrip("﻿")


def _resolve_columns(headers: list[str]) -> dict[str, Optional[str]]:
    """Map our canonical names to whatever the CSV actually uses."""
    norm = {h: _normalize_header(h) for h in headers}
    chosen: dict[str, Optional[str]] = {}

    def pick(candidates: tuple[str, ...]) -> Optional[str]:
        for h, n in norm.items():
            if n in candidates:
                return h
        return None

    chosen["first"] = pick(_COL_FIRST)
    chosen["last"] = pick(_COL_LAST)
    chosen["url"] = pick(_COL_URL)
    chosen["email"] = pick(_COL_EMAIL)
    chosen["company"] = pick(_COL_COMPANY)
    chosen["position"] = pick(_COL_POSITION)
    chosen["connected"] = pick(_COL_CONNECTED)
    return chosen


_DATE_FORMATS = ("%d %b %Y", "%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%b %d, %Y")


def _parse_connected_date(s: str | None) -> Optional[datetime]:
    if not s or not s.strip():
        return None
    s = s.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


@dataclass
class ImportResult:
    parsed_rows: int
    inserted: int
    skipped_duplicates: int
    skipped_invalid: int
    error: Optional[str] = None


def _skip_linkedin_preamble(text: str) -> str:
    """LinkedIn's Connections.csv has a 3-5 line preamble before the actual header.

    Looks like:
      Notes:
      "When ..."
      ""
      First Name,Last Name,URL,...

    We find the first line that has 'First Name' or starts with the canonical
    LinkedIn header pattern, and return text from there.
    """
    lines = text.splitlines(keepends=True)
    header_idx = None
    for i, line in enumerate(lines):
        lower = line.lower()
        # Heuristic: look for any plausible header signature
        if "first name" in lower and "last name" in lower:
            header_idx = i
            break
    if header_idx is None or header_idx == 0:
        return text
    return "".join(lines[header_idx:])


def import_linkedin_csv(session: Session, csv_text: str) -> ImportResult:
    """Parse + import a LinkedIn Connections.csv. Idempotent on (name, linkedin_url).

    Returns counts. Existing rows with matching url are updated (current
    company / title may have changed); rows without url are matched by
    (name, current_company) which is fragile but the best we have.
    """
    text = _skip_linkedin_preamble(csv_text)
    if not text.strip():
        return ImportResult(0, 0, 0, 0, error="CSV is empty after preamble strip")

    try:
        reader = csv.reader(io.StringIO(text))
        rows = list(reader)
    except csv.Error as e:
        return ImportResult(0, 0, 0, 0, error=f"CSV parse error: {e}")

    if len(rows) < 2:
        return ImportResult(0, 0, 0, 0, error="CSV has no data rows")

    headers = rows[0]
    cols = _resolve_columns(headers)
    if not cols.get("first") and not cols.get("last"):
        return ImportResult(0, 0, 0, 0,
                            error=f"Could not find name columns. Got: {headers}")

    # Build a quick header → index lookup
    idx = {h: i for i, h in enumerate(headers)}

    parsed = 0
    inserted = 0
    dup = 0
    invalid = 0

    # Pre-load existing url → connection_id map for fast dup check
    existing_by_url: dict[str, str] = {}
    for c in session.execute(select(Connection)).scalars():
        if c.linkedin_url:
            existing_by_url[c.linkedin_url] = c.id

    for r in rows[1:]:
        if not any(cell.strip() for cell in r):
            continue
        parsed += 1

        def get(col_key: str) -> str:
            h = cols.get(col_key)
            if h is None or h not in idx:
                return ""
            i = idx[h]
            return r[i].strip() if i < len(r) else ""

        first = get("first")
        last = get("last")
        name = (first + " " + last).strip()
        if not name:
            invalid += 1
            continue

        url = get("url") or None
        company = get("company") or None
        position = get("position") or None
        email = get("email") or None
        connected_at = _parse_connected_date(get("connected"))

        # De-dup by URL when present
        if url and url in existing_by_url:
            existing = session.get(Connection, existing_by_url[url])
            if existing:
                # Update fields that may have changed
                if company:
                    existing.current_company = company
                if position:
                    existing.current_title = position
                if email and not existing.email:
                    existing.email = email
                existing.imported_at = datetime.now(timezone.utc)
                existing.active = True
                dup += 1
                continue

        # Insert new
        c = Connection(
            name=name,
            current_company=company,
            current_title=position,
            linkedin_url=url,
            email=email,
            source="linkedin_csv",
            connected_at=connected_at,
        )
        session.add(c)
        inserted += 1
        if url:
            session.flush()
            existing_by_url[url] = c.id

    return ImportResult(parsed, inserted, dup, invalid)


# ── Company-name fuzzy matching ─────────────────────────────────────────────


_COMPANY_SUFFIX_RE = re.compile(
    r"\s+(Inc\.?|Corp\.?|LLC|Ltd\.?|GmbH|AG|S\.A\.?|S\.p\.A\.?|B\.V\.?|N\.V\.?"
    r"|PLC|SE|KG|OHG|Co\.?|Group|Holdings|International|Worldwide)\.?$",
    re.IGNORECASE,
)


def _normalize_company(name: str | None) -> str:
    if not name:
        return ""
    s = _COMPANY_SUFFIX_RE.sub("", name.strip()).strip()
    s = re.sub(r"\s+", " ", s)
    return s.lower()


def find_connections_at(session: Session, company: str, *, limit: int = 10) -> list[Connection]:
    """Return active Connections whose current_company matches `company` fuzzily.

    Match priority:
      1. Exact (lowercased) match on normalized company name
      2. Substring match (normalized → normalized) — either way
      3. Token overlap (≥ 2 shared tokens) for multi-word companies
    """
    if not company or not company.strip():
        return []
    target = _normalize_company(company)
    if not target:
        return []
    target_tokens = set(target.split())

    candidates = session.execute(
        select(Connection).where(Connection.active.is_(True))
    ).scalars().all()

    scored: list[tuple[int, Connection]] = []
    for c in candidates:
        if not c.current_company:
            continue
        cand = _normalize_company(c.current_company)
        if not cand:
            continue

        score = 0
        if cand == target:
            score = 100
        elif target in cand or cand in target:
            score = 60
        elif len(target_tokens) >= 2:
            cand_tokens = set(cand.split())
            shared = target_tokens & cand_tokens
            if len(shared) >= 2:
                score = 30 + 5 * len(shared)
            elif shared and len(target_tokens) <= 2:
                score = 20

        if score > 0:
            scored.append((score, c))

    scored.sort(key=lambda x: (-x[0], x[1].name))
    return [c for _, c in scored[:limit]]


def companies_with_connections(session: Session, *, limit: int = 50) -> list[tuple[str, int]]:
    """Aggregate: which companies have user connections, ranked by count.

    Used by `contacts-first` scrape mode (Stage B feature). For now: shown on
    /connections page as "you have N people at these companies".
    """
    rows = session.execute(
        select(Connection.current_company)
        .where(Connection.active.is_(True), Connection.current_company.is_not(None))
    ).all()

    counts: dict[str, int] = {}
    raw_to_display: dict[str, str] = {}
    for (company,) in rows:
        if not company:
            continue
        norm = _normalize_company(company)
        if not norm:
            continue
        counts[norm] = counts.get(norm, 0) + 1
        # Keep the first non-normalized version as the display label
        if norm not in raw_to_display:
            raw_to_display[norm] = company.strip()

    sorted_pairs = sorted(counts.items(), key=lambda x: (-x[1], x[0]))
    return [(raw_to_display[norm], cnt) for norm, cnt in sorted_pairs[:limit]]


def total_active_connections(session: Session) -> int:
    return session.execute(
        select(Connection).where(Connection.active.is_(True))
    ).scalars().all().__len__()
