"""Dealbreaker Gate — deterministic hard filter (ADR-0016).

Runs BEFORE any LLM evaluation. Pure functions, no DB, no network.

Per ADR-0016, the gate enforces:
  · country_must_be       — Job.location 必须含至少一个国家（case-insensitive）
  · language_blocked      — JD 检测语言命中即 drop
  · blacklist.companies   — Job.company 命中即 drop
  · blacklist.keywords    — Job.title/description 含命中即 drop
  · global.exclude_categories — 跨 profile 排除（bartender / nursing / ...）
  · active_keywords       — Job.title/description 至少命中 1 个 profile keyword

Returns GateDecision; callers (scraper.merge_into_db) decide whether to write
ReflectionEvent("hard_gate_blocked") or proceed to soft rank.

The gate is **per-profile** — the same job may pass profile A's gate and fail
profile B's. callers iterate profiles.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

from .scrape_config import GlobalIntent, ProfileConfig

log = logging.getLogger(__name__)


@dataclass
class GateDecision:
    """Verdict from passing a Job through a profile's gate."""

    allow: bool
    profile_id: str
    blocked_by: list[str] = field(default_factory=list)
    """Reasons (multiple may fire). When allow=True this is empty."""

    notes: list[str] = field(default_factory=list)
    """Soft warnings — e.g. 'remote-in-region accepted despite country mismatch'."""

    def to_event_payload(self, job_id: str) -> dict:
        return {
            "job_id": job_id,
            "profile_id": self.profile_id,
            "allow": self.allow,
            "blocked_by": list(self.blocked_by),
            "notes": list(self.notes),
        }


# Cheap language detector heuristic. Not a real NLP model — just enough to
# catch obvious it/de/fr/zh JDs. False negatives are fine (we re-check at
# user click time); false positives are the cost of being deterministic.
# Catches characters / very common stop-words.
_LANG_HINTS: dict[str, list[str]] = {
    "it": [" della ", " degli ", " sulla ", " perché ", " questo ", " questa ", " sviluppato ", " esperienza "],
    "de": [" der ", " die ", " das ", " und ", " mit ", " für ", " sich ", " werden ", " müssen "],
    "fr": [" est ", " avec ", " sans ", " pour ", " sont ", " être ", " cette ", " leurs "],
    "zh": [],  # use char-class detection below
    "es": [" para ", " porque ", " también ", " puede ", " hacer "],
}

_CJK_RE = re.compile(r"[一-鿿]")


def detect_jd_language(text: str) -> Optional[str]:
    """Cheap language detection. Returns ISO code or None.

    Strategy:
      1. CJK char count > 5 → 'zh'
      2. otherwise: count stop-word hits per language, pick winner if margin > 2
    """
    if not text:
        return None
    if len(_CJK_RE.findall(text)) > 5:
        return "zh"
    blob = " " + text.lower() + " "
    scores: dict[str, int] = {}
    for lang, hints in _LANG_HINTS.items():
        if not hints:
            continue
        scores[lang] = sum(blob.count(h) for h in hints)
    if not scores:
        return None
    best = max(scores, key=scores.get)
    if scores[best] >= 3:  # need at least 3 hits to be confident
        sorted_scores = sorted(scores.values(), reverse=True)
        if len(sorted_scores) < 2 or sorted_scores[0] - sorted_scores[1] >= 2:
            return best
    return None


def _job_haystack(job_obj) -> str:
    """Concat title + company + location + description for keyword search.

    Accepts either a Job ORM object or a dict (so it works on raw scraper payloads
    BEFORE merge_into_db).
    """
    if isinstance(job_obj, dict):
        return " ".join(
            str(job_obj.get(k) or "")
            for k in ("title", "company", "location", "description")
        )
    return " ".join(
        str(getattr(job_obj, k, None) or "")
        for k in ("title", "company", "location", "description")
    )


def _location_text(job_obj) -> str:
    if isinstance(job_obj, dict):
        return str(job_obj.get("location") or "")
    return str(getattr(job_obj, "location", "") or "")


def _is_remote(job_obj) -> bool:
    if isinstance(job_obj, dict):
        is_remote = job_obj.get("is_remote")
        if is_remote is not None:
            return bool(is_remote)
        loc = (job_obj.get("location") or "").lower()
    else:
        is_remote = getattr(job_obj, "is_remote", None)
        if is_remote is not None:
            return bool(is_remote)
        loc = (getattr(job_obj, "location", "") or "").lower()
    return "remote" in loc or "anywhere" in loc


# ─────────────────────────────────────────────────────────
# Per-rule checks (each returns block-reason string or None)
# ─────────────────────────────────────────────────────────


def _check_country(job_obj, profile: ProfileConfig) -> Optional[tuple[str, list[str]]]:
    """Returns (reason, [notes]) on block, None on pass.

    `country_must_be` empty → no country gate (pass).
    Allows remote-in-region when configured.

    LinkedIn / Indeed often return location strings without an explicit
    country (e.g. "Frankfurt am Main, Hesse"). Hard-blocking those silently
    eats most of the result set. We treat profile.city presence in the
    location as evidence of country-match, since the user already declared
    "this profile is for {city}, {country}".
    """
    must = [s.lower() for s in profile.dealbreakers.country_must_be]
    if not must:
        return None
    loc_raw = _location_text(job_obj)
    loc = loc_raw.lower()
    if any(c in loc for c in must):
        return None  # explicit country match
    # Soft-match: profile.city appearing in the job location is taken as
    # implicit country evidence. We only allow this when the profile's
    # declared country is one of the must_be entries — otherwise the user
    # is mixing profiles and we shouldn't second-guess.
    profile_city = (getattr(profile, "city", "") or "").lower().strip()
    profile_country = (getattr(profile, "country", "") or "").lower().strip()
    if (
        profile_city
        and profile_country
        and profile_country in must
        and profile_city in loc
    ):
        return ("country_city_inferred", [f"city '{profile_city}' present → assumed {profile_country}"])
    if profile.dealbreakers.allow_remote_in_region and _is_remote(job_obj):
        return ("country_remote_accepted", ["allow_remote_in_region matched"])  # not a block — passthrough with note
    return (
        f"country_mismatch: location='{loc_raw}' lacks any of {profile.dealbreakers.country_must_be}",
        [],
    )


def _check_language_blocked(job_obj, profile: ProfileConfig) -> Optional[str]:
    blocked = {s.lower() for s in profile.dealbreakers.language_blocked}
    if not blocked:
        return None
    if isinstance(job_obj, dict):
        desc = str(job_obj.get("description") or "")
    else:
        desc = str(getattr(job_obj, "description", "") or "")
    detected = detect_jd_language(desc)
    if detected and detected.lower() in blocked:
        return f"language_blocked: detected '{detected}' (in profile.language_blocked)"
    return None


def _check_company_blacklist(job_obj, profile: ProfileConfig) -> Optional[str]:
    bl = {s.lower() for s in profile.blacklist.companies}
    if not bl:
        return None
    company = (
        (job_obj.get("company") if isinstance(job_obj, dict) else getattr(job_obj, "company", None))
        or ""
    ).lower()
    if any(b in company for b in bl):
        return f"company_blacklisted: '{company}' matches blacklist"
    return None


def _check_keyword_blacklist(job_obj, profile: ProfileConfig, global_: GlobalIntent) -> Optional[str]:
    """Either profile blacklist.keywords or global.exclude_categories blocks the job."""
    bl = [s.lower() for s in profile.blacklist.keywords]
    bl += [s.lower() for s in global_.exclude_categories]
    if not bl:
        return None
    blob = _job_haystack(job_obj).lower()
    for term in bl:
        if term and term in blob:
            return f"keyword_blacklisted: '{term}' present"
    return None


def _check_active_keywords(job_obj, profile: ProfileConfig) -> Optional[str]:
    """Job must contain at least one of the profile's active keywords (or aliases)."""
    kws = [s.lower() for s in profile.keywords]
    if not kws:
        return None  # no keywords = profile is "any role at this location" — pass
    # Include aliases in the candidate set
    for canonical, aliases in (profile.keyword_aliases or {}).items():
        if isinstance(aliases, list):
            kws.extend(s.lower() for s in aliases if isinstance(s, str))
        kws.append(canonical.lower())
    blob = _job_haystack(job_obj).lower()
    if any(k in blob for k in kws if k):
        return None
    return f"no_keyword_match: none of {profile.keywords[:3]}{'...' if len(profile.keywords) > 3 else ''} present"


# ─────────────────────────────────────────────────────────
# Top-level gate
# ─────────────────────────────────────────────────────────


def check_job(
    job_obj,
    profile: ProfileConfig,
    *,
    global_: GlobalIntent | None = None,
) -> GateDecision:
    """Run all gate checks for a single (job, profile) pair.

    Block reasons accumulate — we don't short-circuit so the user can see the
    full picture in the ReflectionEvent stream.
    """
    if global_ is None:
        global_ = GlobalIntent()
    blocked: list[str] = []
    notes: list[str] = []

    # country (may also produce a passthrough note)
    cc = _check_country(job_obj, profile)
    if cc is not None:
        reason, n = cc
        if reason.startswith("country_remote_accepted") or reason.startswith("country_city_inferred"):
            notes.extend(n)
        else:
            blocked.append(reason)

    if (r := _check_language_blocked(job_obj, profile)) is not None:
        blocked.append(r)
    if (r := _check_company_blacklist(job_obj, profile)) is not None:
        blocked.append(r)
    if (r := _check_keyword_blacklist(job_obj, profile, global_)) is not None:
        blocked.append(r)
    if (r := _check_active_keywords(job_obj, profile)) is not None:
        blocked.append(r)

    return GateDecision(
        allow=not blocked,
        profile_id=profile.id,
        blocked_by=blocked,
        notes=notes,
    )


def filter_jobs(
    jobs: list,
    profile: ProfileConfig,
    *,
    global_: GlobalIntent | None = None,
) -> tuple[list, list[GateDecision]]:
    """Convenience: split a list of jobs into (passed, blocked_decisions).

    `passed` keeps original job objects in their original order; `blocked_decisions`
    runs parallel — caller can write a ReflectionEvent per blocked decision.
    """
    passed: list = []
    blocked_decisions: list[GateDecision] = []
    for j in jobs:
        decision = check_job(j, profile, global_=global_)
        if decision.allow:
            passed.append(j)
        else:
            blocked_decisions.append(decision)
    return passed, blocked_decisions
