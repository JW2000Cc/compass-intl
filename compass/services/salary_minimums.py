"""Per-region salary minimum thresholds — separate from preference_brief.

Why a structured table (not a free-text brief line):
    Salary preferences vary systematically by region (cost-of-living anchored).
    A user can have:
      Germany     ≥ 2000 €/month
      Netherlands ≥ 2500 €/month
      Italy       — no floor
    Encoding this as 3+ free-text brief lines burns brief cap + forces LLM to
    parse "in Germany" / "in Netherlands" scopes on every classification.
    A keyed table is deterministic, queryable, and compresses to ~one prompt line.

Format (data/salary_minimums.json):
    {
      "rules": [
        {
          "region": "Germany",
          "currency": "EUR",
          "monthly_min": 2000,
          "yearly_min": null,                 # if monthly given, yearly inferred
          "added_at": "...", "note": "..."
        },
        ...
      ]
    }

Region match:
    Compares against job.location case-insensitively. Region string can be a
    country ("Germany"), city ("Berlin"), or substring ("Bay Area"). First
    matching rule wins (most-specific first by length).

Job-against-floor decision:
    If job.salary_min < monthly_min × 12 (or .yearly_min), recommend Tier demotion.
    Caller (tier_classifier) decides what to do with the signal.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


@dataclass
class SalaryFloor:
    region: str                # case-insensitive substring match against job.location
    currency: str = "EUR"
    monthly_min: Optional[float] = None
    yearly_min: Optional[float] = None
    added_at: str = ""
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "SalaryFloor":
        return cls(
            region=str(d.get("region", "")).strip(),
            currency=str(d.get("currency", "EUR")).strip().upper() or "EUR",
            monthly_min=_to_float(d.get("monthly_min")),
            yearly_min=_to_float(d.get("yearly_min")),
            added_at=str(d.get("added_at", "")),
            note=str(d.get("note", "")),
        )

    def yearly_threshold(self) -> Optional[float]:
        if self.yearly_min is not None:
            return self.yearly_min
        if self.monthly_min is not None:
            return self.monthly_min * 12
        return None


def _to_float(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


@dataclass
class SalaryMinimums:
    rules: list[SalaryFloor] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> "SalaryMinimums":
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return cls(rules=[SalaryFloor.from_dict(r) for r in (data.get("rules") or [])])
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            log.warning("salary_minimums load failed: %s", e)
            return cls()

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {"rules": [r.to_dict() for r in self.rules]}
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    def upsert(self, region: str, *, currency: str = "EUR",
               monthly_min: Optional[float] = None,
               yearly_min: Optional[float] = None,
               note: str = "") -> SalaryFloor:
        region = region.strip()
        if not region:
            raise ValueError("region cannot be empty")
        # Replace existing same-region rule
        for i, r in enumerate(self.rules):
            if r.region.lower() == region.lower():
                r.currency = currency.upper()
                r.monthly_min = monthly_min
                r.yearly_min = yearly_min
                r.note = note
                if not r.added_at:
                    r.added_at = datetime.now(timezone.utc).isoformat()
                return r
        new = SalaryFloor(
            region=region, currency=currency.upper(),
            monthly_min=monthly_min, yearly_min=yearly_min,
            added_at=datetime.now(timezone.utc).isoformat(),
            note=note,
        )
        self.rules.append(new)
        return new

    def remove(self, region: str) -> bool:
        before = len(self.rules)
        self.rules = [r for r in self.rules if r.region.lower() != region.lower()]
        return len(self.rules) < before

    def find_match(self, job_location: Optional[str]) -> Optional[SalaryFloor]:
        """Find the most specific (longest) matching rule for a job's location.

        Returns None if no rule matches or location is None.
        """
        if not job_location:
            return None
        loc = job_location.lower()
        # Sort by descending region length so "Berlin, Germany" beats "Germany"
        candidates = sorted(self.rules, key=lambda r: -len(r.region))
        for r in candidates:
            if r.region and r.region.lower() in loc:
                return r
        return None

    def evaluate(self, job_location: Optional[str], job_salary_min: Optional[float],
                 job_salary_currency: Optional[str] = None) -> dict:
        """Evaluate a job against the floors.

        Returns:
          {
            "matched_rule": SalaryFloor or None,
            "below_floor": bool,
            "demote_recommendation": int (Tier offset, 0 = no demotion, 1 = demote one tier),
            "note": str (human-readable reason)
          }

        Currency mismatch: if rule is EUR and job is USD, we don't auto-convert.
        Surface the mismatch but don't demote (be conservative).
        """
        rule = self.find_match(job_location)
        if rule is None:
            return {"matched_rule": None, "below_floor": False,
                    "demote_recommendation": 0, "note": ""}

        threshold = rule.yearly_threshold()
        if threshold is None:
            return {"matched_rule": rule, "below_floor": False,
                    "demote_recommendation": 0,
                    "note": f"{rule.region}: no floor set"}

        if job_salary_min is None:
            return {"matched_rule": rule, "below_floor": False,
                    "demote_recommendation": 0,
                    "note": f"{rule.region} floor {rule.monthly_min}{rule.currency}/mo: job has no salary listed"}

        # Currency mismatch — be conservative, don't demote
        if (job_salary_currency or rule.currency).upper() != rule.currency.upper():
            return {"matched_rule": rule, "below_floor": False,
                    "demote_recommendation": 0,
                    "note": f"currency mismatch ({job_salary_currency} vs {rule.currency}); manual review"}

        if job_salary_min < threshold:
            return {
                "matched_rule": rule,
                "below_floor": True,
                "demote_recommendation": 1,
                "note": f"{rule.region}: job {job_salary_min:.0f}{rule.currency}/yr < floor {threshold:.0f}{rule.currency}/yr",
            }
        return {"matched_rule": rule, "below_floor": False,
                "demote_recommendation": 0,
                "note": f"{rule.region}: ok ({job_salary_min:.0f} ≥ {threshold:.0f}{rule.currency})"}

    def format_for_prompt(self) -> str:
        if not self.rules:
            return ""
        lines = ["User's region-keyed salary floors (yearly equivalent, in their currency):"]
        for r in self.rules:
            t = r.yearly_threshold()
            if t is None:
                lines.append(f"  - {r.region}: no floor")
            else:
                lines.append(f"  - {r.region}: ≥ {t:.0f} {r.currency}/yr")
        return "\n".join(lines)


def _data_dir() -> Path:
    try:
        from flask import current_app
        return current_app.config["SETTINGS"].data_dir
    except Exception:
        return Path("data")


def salary_minimums_path() -> Path:
    return _data_dir() / "salary_minimums.json"


def load() -> SalaryMinimums:
    return SalaryMinimums.load(salary_minimums_path())


def save(table: SalaryMinimums) -> None:
    table.save(salary_minimums_path())
