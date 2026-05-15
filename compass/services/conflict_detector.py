"""Detect conflicts between user's stated criteria and revealed preferences.

Stated sources (what user has explicitly told the system):
    - scrape config keywords + locations (per-group, from data/scrape_groups.json)
    - salary_minimums table
    - IdentityVersion + active facts (sensitive_categories signal what's locked)
    - UserCalibration (max_amplification_level, sensitive_categories_locked)

Revealed (what their feedback accumulated):
    - preference_brief (jobs / resume)

Severity ladder:
    "hard"     — direct contradiction with confirmed_count ≥ 3 ⇒ banner alert
    "moderate" — contradiction with confirmed_count 1-2 ⇒ feedback page warning
    "mild"     — soft tension; no warning, just visible in dashboard

This is the structural enrichment of the existing reflection_engine.stated_vs_revealed
(which only counts pushed/up/down). Now we have semantic signals to work with.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path


from .preference_brief import Brief
from .salary_minimums import SalaryMinimums

log = logging.getLogger(__name__)


@dataclass
class Conflict:
    line_id: str
    line_text: str
    line_side: str
    line_axis: str
    confirmed_count: int
    stated_source: str       # "scrape_keyword" | "scrape_location" | "salary_floor" | "calibration" | "fact_lock"
    stated_value: str
    severity: str            # "hard" | "moderate" | "mild"
    explanation: str         # one-sentence why this is a conflict

    def to_dict(self) -> dict:
        return asdict(self)


def _scrape_keywords_and_locations() -> tuple[list[str], list[str]]:
    """Read user's stated keywords + locations from `last_scrape.json` —
    the single source of truth (multi-profile schema, ADR-0016 in-place).

    Reads only `enabled` profiles. v3 legacy schema (`groups[]`) is auto-upgraded
    by scrape_config.load() on first read.
    """
    try:
        from flask import current_app
        data_dir = current_app.config["SETTINGS"].data_dir
    except Exception:
        data_dir = Path("data")

    p = data_dir / "last_scrape.json"
    if not p.exists():
        return [], []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return [], []

    kws: list[str] = []
    locs: list[str] = []

    # New multi-profile schema
    if "profiles" in data:
        for prof in (data.get("profiles") or []):
            if not prof.get("enabled", True):
                continue
            for k in (prof.get("keywords") or []):
                if isinstance(k, str) and k.strip():
                    kws.append(k.strip().lower())
            city = (prof.get("city") or "").strip()
            country = (prof.get("country") or "").strip()
            loc = ", ".join(x for x in (city, country) if x)
            if loc:
                locs.append(loc.lower())
        return kws, locs

    # v3 legacy fallback (pre-upgrade)
    for g in (data.get("groups") or []):
        if not g.get("enabled", True):
            continue
        for k in (g.get("keywords") or []):
            if isinstance(k, str) and k.strip():
                kws.append(k.strip().lower())
        for l in (g.get("locations") or []):
            if isinstance(l, str) and l.strip():
                locs.append(l.strip().lower())
    return kws, locs


def _normalize(s: str) -> str:
    return s.strip().lower()


def _line_mentions(line_text: str, target: str) -> bool:
    """Does line_text semantically reference 'target'?

    Heuristic: case-insensitive substring match. LLM-based check would be more
    accurate but this runs on every brief change — keep it cheap.
    """
    return _normalize(target) in _normalize(line_text)


def detect_jobs_conflicts(
    jobs_brief: Brief,
    salary: SalaryMinimums,
) -> list[Conflict]:
    """Scan jobs brief for conflicts with stated criteria. Cheap, no LLM."""
    conflicts: list[Conflict] = []
    stated_kws, stated_locs = _scrape_keywords_and_locations()

    for line in jobs_brief.active_lines():
        # Negative-side conflicts: brief says "avoid X" but X is in stated keywords/locations
        if line.side == "-":
            for kw in stated_kws:
                if _line_mentions(line.text, kw):
                    severity = "hard" if line.confirmed_count >= 3 else "moderate"
                    conflicts.append(Conflict(
                        line_id=line.id, line_text=line.text,
                        line_side=line.side, line_axis=line.axis,
                        confirmed_count=line.confirmed_count,
                        stated_source="scrape_keyword",
                        stated_value=kw,
                        severity=severity,
                        explanation=(
                            f'Brief says avoid "{line.text}" '
                            f'but "{kw}" is in your scrape keywords '
                            f"(confirmed {line.confirmed_count}× — "
                            f"{'strong' if severity == 'hard' else 'moderate'} signal)"
                        ),
                    ))
                    break  # don't flag same line twice for diff keywords

            for loc in stated_locs:
                if _line_mentions(line.text, loc):
                    severity = "hard" if line.confirmed_count >= 3 else "moderate"
                    conflicts.append(Conflict(
                        line_id=line.id, line_text=line.text,
                        line_side=line.side, line_axis=line.axis,
                        confirmed_count=line.confirmed_count,
                        stated_source="scrape_location",
                        stated_value=loc,
                        severity=severity,
                        explanation=(
                            f'Brief says avoid "{line.text}" '
                            f'but "{loc}" is in your scrape locations'
                        ),
                    ))
                    break

        # Positive-side conflicts vs salary floors:
        # If brief says "+ low-comp ok" but user has a salary floor for same region — tension.
        # (Less common; the typical pattern is the floor itself encodes the preference.)

    return conflicts


def detect_resume_conflicts(
    resume_brief: Brief,
    *,
    max_amplification_level: int,
    locked_sensitive_categories: list[str],
) -> list[Conflict]:
    """Scan resume brief for conflicts with calibration / sensitive locks."""
    conflicts: list[Conflict] = []

    for line in resume_brief.active_lines():
        text = line.text.lower()

        # Conflict: brief asks for more aggressive rewrites but max_amplification_level low
        wants_aggressive = (line.side == "+" and any(
            t in text for t in ("更激进", "more aggressive", "stronger", "更强",
                                "amplif", "scale up", "boost")
        ))
        if wants_aggressive and max_amplification_level <= 1:
            severity = "hard" if line.confirmed_count >= 3 else "moderate"
            conflicts.append(Conflict(
                line_id=line.id, line_text=line.text,
                line_side=line.side, line_axis=line.axis,
                confirmed_count=line.confirmed_count,
                stated_source="calibration",
                stated_value=f"max_amplification_level={max_amplification_level}",
                severity=severity,
                explanation=(
                    f'Brief leans toward "{line.text}" but your '
                    f"calibration cap is L{max_amplification_level} — "
                    "rewrites can't go above that"
                ),
            ))

        # Conflict: brief asks for more quantification but a sensitive category locks it
        wants_quant = (line.side == "+" and "quantif" in text and "employment_dates" in locked_sensitive_categories)
        if wants_quant and "employment_dates" in locked_sensitive_categories:
            conflicts.append(Conflict(
                line_id=line.id, line_text=line.text,
                line_side=line.side, line_axis=line.axis,
                confirmed_count=line.confirmed_count,
                stated_source="fact_lock",
                stated_value="employment_dates locked",
                severity="moderate",
                explanation=(
                    f'Brief asks for more quantification but employment_dates '
                    "is sensitive-locked at L0/L1"
                ),
            ))

    return conflicts


def all_conflicts() -> list[Conflict]:
    """Convenience: load both briefs + stated sources and return all conflicts."""
    from .preference_brief import load_jobs_brief, load_resume_brief
    from .salary_minimums import load as load_salary
    from ..extensions import session_scope
    from ..models import UserCalibration

    jobs = load_jobs_brief()
    resume = load_resume_brief()
    salary = load_salary()

    out = list(detect_jobs_conflicts(jobs, salary))
    with session_scope() as s:
        cal = s.get(UserCalibration, 1)
        max_lvl = cal.max_amplification_level if cal else 2
        locked = list(cal.sensitive_categories_locked or []) if cal else []
    out.extend(detect_resume_conflicts(
        resume,
        max_amplification_level=max_lvl,
        locked_sensitive_categories=locked,
    ))
    return out
