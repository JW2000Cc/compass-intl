"""Multi-profile scrape config — in-place schema upgrade of `data/last_scrape.json`.

5-06 用户原话："为啥不直接在老ui那个接口那里修改" — 这个 module 是 in-place
重做的产物：保留 `last_scrape.json` 文件名（用户已熟悉），schema 原地升级到
multi-profile，老 v3 schema 在第一次 load 时自动迁移并落盘。

Schema layout（单一 source-of-truth in last_scrape.json）：

  - `version` (int) — 1 = multi-profile schema (本格式)
  - `global` — cross-profile 值（user_languages, exclude_categories, ...）
  - `profiles[]` — 每条独立 profile，含 city/country/keywords/user_prompt/
    blacklist/dealbreakers/tier_quota/hours_old/keyword_aliases

向前兼容：v3 schema (`groups[]` 无 `profiles[]`) 在 load 时检测、转换、自动
save 落新 schema —— 用户文件名不变，无人工迁移步骤。

Pure data layer. No LLM, no web, no DB. dealbreaker_gate consumes
ProfileConfig; tier_classifier consumes profile.user_prompt + keywords.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

INTENT_VERSION = 1


# ─────────────────────────────────────────────────────────
# Dataclasses
# ─────────────────────────────────────────────────────────


@dataclass
class Dealbreakers:
    """Hard constraints on a profile. Failures cause the job to be DROPPED
    in dealbreaker_gate, not just down-ranked.
    """

    country_must_be: list[str] = field(default_factory=list)
    """Job.location must contain at least one of these (case-insensitive). Empty = no country gate."""

    language_required: list[str] = field(default_factory=list)
    """User can read these languages — JD in any other language gets dropped (if detected)."""

    language_blocked: list[str] = field(default_factory=list)
    """JDs in these languages get dropped immediately."""

    visa_required: bool = False
    """If True, jobs flagged as 'no visa sponsorship' get dropped."""

    allow_remote_in_region: bool = True
    """If country gate fails BUT job is remote within an allowed region, accept anyway."""


@dataclass
class TierQuota:
    """How many jobs the user wants to see per tier per push, for THIS profile.
    A profile that's "core" might be 70/20/10; an "exploration" profile 30/50/20.
    """

    tier_1: int = 5
    tier_2: int = 3
    tier_3: int = 2


@dataclass
class Blacklist:
    companies: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)


@dataclass
class ProfileConfig:
    """A single search profile — one independent intent within the user's job hunt.

    Examples (the user might have all three):
      id="de-electrical", label="德国-电气核心", keywords=["Electrical Engineer", ...]
      id="de-finance",    label="德国-金融咨询", keywords=["Wealth Manager", ...]
      id="de-data",       label="德国-数据分析", keywords=["Data Analyst", ...]
    """

    id: str
    label: str
    enabled: bool = True
    city: str = ""
    country: str = ""
    radius_km: int = 30
    keywords: list[str] = field(default_factory=list)
    keyword_aliases: dict[str, list[str]] = field(default_factory=dict)
    user_prompt: str = ""
    blacklist: Blacklist = field(default_factory=Blacklist)
    dealbreakers: Dealbreakers = field(default_factory=Dealbreakers)
    tier_quota: TierQuota = field(default_factory=TierQuota)
    hours_old: Optional[int] = 168
    """Time window for jobspy: only return jobs posted within last N hours.
    Default 168 = 1 week. Set to None to disable filter (rarely useful)."""

    @property
    def location_string(self) -> str:
        """For jobspy / scraper: "City, Country" or fallback."""
        if self.city and self.country:
            return f"{self.city}, {self.country}"
        return self.city or self.country or ""

    def to_jsonable(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ProfileConfig":
        bl = d.get("blacklist") or {}
        db = d.get("dealbreakers") or {}
        tq = d.get("tier_quota") or {}
        return cls(
            id=str(d.get("id") or "").strip() or _slug(d.get("label", "profile")),
            label=str(d.get("label") or "Untitled"),
            enabled=bool(d.get("enabled", True)),
            city=str(d.get("city") or "").strip(),
            country=str(d.get("country") or "").strip(),
            radius_km=int(d.get("radius_km", 30)),
            keywords=[k for k in (d.get("keywords") or []) if isinstance(k, str)],
            keyword_aliases=dict(d.get("keyword_aliases") or {}),
            user_prompt=str(d.get("user_prompt") or "").strip(),
            blacklist=Blacklist(
                companies=[c for c in (bl.get("companies") or []) if isinstance(c, str)],
                keywords=[k for k in (bl.get("keywords") or []) if isinstance(k, str)],
            ),
            dealbreakers=Dealbreakers(
                country_must_be=[
                    s for s in (db.get("country_must_be") or []) if isinstance(s, str)
                ],
                language_required=[
                    s for s in (db.get("language_required") or []) if isinstance(s, str)
                ],
                language_blocked=[
                    s for s in (db.get("language_blocked") or []) if isinstance(s, str)
                ],
                visa_required=bool(db.get("visa_required", False)),
                allow_remote_in_region=bool(db.get("allow_remote_in_region", True)),
            ),
            tier_quota=TierQuota(
                tier_1=int(tq.get("tier_1", 5)),
                tier_2=int(tq.get("tier_2", 3)),
                tier_3=int(tq.get("tier_3", 2)),
            ),
            hours_old=_safe_optional_int(d.get("hours_old"), default=168),
        )


def _safe_optional_int(v, default=None):
    if v is None or v == "":
        return None
    try:
        n = int(v)
        return n if n > 0 else None
    except (ValueError, TypeError):
        return default


@dataclass
class GlobalIntent:
    """Cross-profile intent values."""

    user_languages: list[str] = field(default_factory=list)
    """Languages the user is comfortable reading (e.g. ["en", "zh"])."""

    exclude_categories: list[str] = field(default_factory=list)
    """Roles to never show, regardless of profile (bartender, nursing, ...)."""

    min_salary: Optional[float] = None
    remote_preference: str = "any"  # any | hybrid | onsite | remote_only

    @classmethod
    def from_dict(cls, d: dict) -> "GlobalIntent":
        return cls(
            user_languages=[s for s in (d.get("user_languages") or []) if isinstance(s, str)],
            exclude_categories=[
                s for s in (d.get("exclude_categories") or []) if isinstance(s, str)
            ],
            min_salary=d.get("min_salary"),
            remote_preference=str(d.get("remote_preference") or "any"),
        )


@dataclass
class JobSearchIntent:
    """Top-level intent record. version + global + profiles[]."""

    version: int = INTENT_VERSION
    global_: GlobalIntent = field(default_factory=GlobalIntent)
    profiles: list[ProfileConfig] = field(default_factory=list)

    def enabled_profiles(self) -> list[ProfileConfig]:
        return [p for p in self.profiles if p.enabled]

    def find(self, profile_id: str) -> Optional[ProfileConfig]:
        for p in self.profiles:
            if p.id == profile_id:
                return p
        return None

    def to_jsonable(self) -> dict:
        return {
            "version": self.version,
            "global": asdict(self.global_),
            "profiles": [p.to_jsonable() for p in self.profiles],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "JobSearchIntent":
        return cls(
            version=int(d.get("version", INTENT_VERSION)),
            global_=GlobalIntent.from_dict(d.get("global") or {}),
            profiles=[ProfileConfig.from_dict(p) for p in (d.get("profiles") or [])],
        )


# ─────────────────────────────────────────────────────────
# IO + backward-compat lazy import
# ─────────────────────────────────────────────────────────


def scrape_config_path(data_dir: Path) -> Path:
    """Single source-of-truth: data/last_scrape.json (in-place upgraded schema).

    File name preserved for user continuity (5-06 in-place rework decision).
    Old v3 single-global schema and new multi-profile schema both live here —
    load() auto-detects and migrates v3 → multi-profile on first read.
    """
    return data_dir / "last_scrape.json"


def load(data_dir: Path) -> JobSearchIntent:
    """Load multi-profile config from last_scrape.json.

    Auto-detects schema:
      - new multi-profile (has `profiles[]`) → parse directly
      - v3 legacy (has `groups[]` no `profiles[]`) → in-memory migrate, then
        auto-save so next load reads upgraded format

    Auto-persist rationale (5-06 21:33 bug fix): pre-fix, lazy-migrated
    profile lived only in memory on each request. Edit form pre-filled
    dealbreakers from in-memory copy, but sat in a collapsed details block.
    Users who didn't expand it never saw values, then on save POST overwrote
    them with empty (= cleared). Country gating silently disabled. Persisting
    on first load round-trips faithfully.
    """
    p = scrape_config_path(data_dir)
    if not p.exists():
        return JobSearchIntent()  # fresh empty

    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.error("last_scrape.json corrupt: %s; returning empty config", exc)
        return JobSearchIntent()

    # New multi-profile schema
    if "profiles" in data:
        try:
            return JobSearchIntent.from_dict(data)
        except (KeyError, TypeError) as exc:
            log.error("multi-profile parse error: %s; returning empty", exc)
            return JobSearchIntent()

    # v3 legacy (groups[] only) — in-place upgrade
    intent = _migrate_from_v3_schema(data)
    if intent.profiles:
        try:
            save(data_dir, intent)
            log.info("upgraded v3 last_scrape.json → multi-profile schema in-place")
        except OSError as exc:
            log.warning("failed to auto-persist upgrade: %s", exc)
    return intent


_save_lock = threading.Lock()


def save(data_dir: Path, intent: JobSearchIntent) -> None:
    """Atomic write of last_scrape.json.

    Concurrent callers (e.g. 3 simultaneous form POSTs after a triple-click on
    📥 抓取) used to race on a shared `last_scrape.json.tmp` name — first
    rename wins, others got `[Errno 2] No such file` warnings and silently
    dropped their write. Two-fold guard:
      1. Per-call unique tmp name (pid + counter) so writes don't clobber.
      2. Module-level threading.Lock around the rename — ensures the file
         on disk reflects exactly one of the concurrent payloads, not a
         half-flushed mix.
    """
    p = scrape_config_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(f".json.tmp.{os.getpid()}.{threading.get_ident()}")
    tmp.write_text(
        json.dumps(intent.to_jsonable(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    with _save_lock:
        tmp.replace(p)


def _migrate_from_v3_schema(data: dict) -> JobSearchIntent:
    """In-memory migration: v3 (groups[]) → multi-profile (profiles[])."""
    profiles: list[ProfileConfig] = []
    for g in data.get("groups") or []:
        label = str(g.get("name") or "Imported")
        locations = [s for s in (g.get("locations") or []) if isinstance(s, str)]
        keywords = [s for s in (g.get("keywords") or []) if isinstance(s, str)]
        # ADR-0016 补丁: carry over hours_old + per-group blacklist from old groups
        ho = _safe_optional_int(g.get("hours_old"), default=168)
        bl = g.get("blacklist") or {}
        if not locations:
            continue
        # One profile per (group × location) so each profile is single-location
        for loc in locations:
            city, country = _split_city_country(loc)
            profiles.append(
                ProfileConfig(
                    id=_slug(f"{label}-{city or country or 'x'}"),
                    label=f"{label} · {loc}",
                    enabled=bool(g.get("enabled", True)),
                    city=city,
                    country=country,
                    keywords=list(keywords),
                    user_prompt="",  # user fills in after migration
                    blacklist=Blacklist(
                        companies=[c for c in (bl.get("companies") or []) if isinstance(c, str)],
                        keywords=[k for k in (bl.get("keywords") or []) if isinstance(k, str)],
                    ),
                    dealbreakers=Dealbreakers(
                        country_must_be=[country] if country else [],
                    ),
                    hours_old=ho,
                )
            )

    return JobSearchIntent(profiles=profiles)


def _split_city_country(loc: str) -> tuple[str, str]:
    """'Frankfurt, Germany' → ('Frankfurt', 'Germany'). Forgiving on weird input."""
    parts = [p.strip() for p in loc.split(",")]
    if len(parts) >= 2:
        return parts[0], parts[-1]
    return parts[0] if parts else "", ""


def slug(s: str) -> str:
    """Public — generate a URL-safe slug from a free-form label.
    Used by routes/search_config.new() to derive profile.id."""
    out = []
    for ch in (s or "profile"):
        if ch.isalnum():
            out.append(ch.lower())
        elif ch in (" ", "-", "_"):
            out.append("-")
    s2 = "".join(out).strip("-")
    return s2[:40] or "profile"


# Backward-compat alias for any pre-rename callers / tests.
_slug = slug
