"""Preference brief — accumulated user feedback that steers recommendations.

Architecture:
    Two independent briefs:
      preference_brief_jobs.json    → consumed by tier_classifier
      preference_brief_resume.json  → consumed by resume_writer

    Each brief is a list of lines. Each line has:
      text        — short canonical phrasing (≤ 60 chars)
      side        — "+" preference / "-" aversion
      axis        — semantic dimension (company_stage / location / comp / ...)
      scope       — optional free-text scope ("for IC roles", "in Germany")
      origin      — first time this preference was extracted
      confirmations — subsequent times the user said the same thing
      anchor      — (resume only) variant_id + bullet_index when applicable

Caps & decay:
    HARD_CAP        = 16    Hard upper bound; new line evicts lowest-score line
    SOFT_WARN       = 12    UI warns user to consider cleanup
    PER_AXIS_WARN   = 4     UI suggests merge when one axis fills
    EXPIRY_DAYS     = 30    Lines expire if not re-confirmed

Eviction by score:
    score = sqrt(confirmed_count) / (days_since_last_confirmation + 1)
    High-confirmed + recent lines stay; one-off + stale lines fade.

Multi-language:
    Stored text is in the user's resume language (canonical).
    Origin/confirmation history preserves the user's *actual original phrasing*
    in whatever language they wrote it. dedup-merge LLM is told to bridge
    semantically across languages.

This module is storage + manipulation. NL extraction is in preference_extractor.
Conflict detection (stated vs revealed) is in conflict_detector.
"""
from __future__ import annotations

import json
import logging
import math
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

HARD_CAP = 16
SOFT_WARN = 12
PER_AXIS_WARN = 4
EXPIRY_DAYS = 30
KEEP_FULL_CONFIRMATIONS = 8  # older confirmations stored as count only

JOB_AXES = {"role", "company_stage", "location", "comp", "scope",
            "culture", "remote", "industry"}
RESUME_AXES = {"bullet_length", "quantification", "leadership_claim",
               "tone", "technical_depth", "structure", "wording"}

VALID_SIDES = {"+", "-"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse(s: str | None) -> Optional[datetime]:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


@dataclass
class ConfirmationEntry:
    """Each time user confirmed a preference: which item triggered it + their words."""
    item_id: str          # job_id or variant_id
    item_label: str       # "Sr PM @ Foobar" or "v_abc bullet #2"
    original_text: str    # the user's actual NL feedback at this moment
    at: str               # ISO datetime
    bullet_index: Optional[int] = None  # resume only

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class BriefLine:
    """One preference statement."""
    id: str
    text: str                      # canonical phrasing (in user's resume language)
    side: str                      # "+" or "-"
    axis: str
    scope: Optional[str] = None    # free-text, optional
    confidence: float = 0.7
    confirmed_count: int = 1
    added_at: str = ""
    last_confirmed_at: str = ""
    expires_at: str = ""
    origin: Optional[ConfirmationEntry] = None
    confirmations: list[ConfirmationEntry] = field(default_factory=list)

    @classmethod
    def new(cls, *, text: str, side: str, axis: str, scope: Optional[str],
            confidence: float, origin: ConfirmationEntry) -> "BriefLine":
        now = _now()
        return cls(
            id=uuid.uuid4().hex[:10],
            text=text, side=side, axis=axis, scope=scope,
            confidence=confidence,
            confirmed_count=1,
            added_at=_iso(now),
            last_confirmed_at=_iso(now),
            expires_at=_iso(now + timedelta(days=EXPIRY_DAYS)),
            origin=origin,
            confirmations=[],
        )

    def is_expired(self) -> bool:
        exp = _parse(self.expires_at)
        return exp is not None and _now() > exp

    def days_since_last_confirmed(self) -> int:
        last = _parse(self.last_confirmed_at)
        if last is None:
            return EXPIRY_DAYS
        return max(0, (_now() - last).days)

    def eviction_score(self) -> float:
        """Higher = stickier. sqrt(count) / (days + 1)."""
        return math.sqrt(self.confirmed_count) / (self.days_since_last_confirmed() + 1)

    def confirm(self, entry: ConfirmationEntry) -> None:
        """Record another time this preference was stated. Refreshes expiry."""
        self.confirmed_count += 1
        self.last_confirmed_at = entry.at
        self.expires_at = _iso(_parse(entry.at) + timedelta(days=EXPIRY_DAYS))
        self.confirmations.append(entry)
        # Cap full-detail confirmations; older ones live as count only
        if len(self.confirmations) > KEEP_FULL_CONFIRMATIONS:
            self.confirmations = self.confirmations[-KEEP_FULL_CONFIRMATIONS:]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["origin"] = self.origin.to_dict() if self.origin else None
        d["confirmations"] = [c.to_dict() for c in self.confirmations]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "BriefLine":
        origin = ConfirmationEntry(**d["origin"]) if d.get("origin") else None
        confs = [ConfirmationEntry(**c) for c in (d.get("confirmations") or [])]
        return cls(
            id=d["id"], text=d["text"], side=d["side"], axis=d["axis"],
            scope=d.get("scope"),
            confidence=float(d.get("confidence", 0.7)),
            confirmed_count=int(d.get("confirmed_count", 1)),
            added_at=d.get("added_at", ""),
            last_confirmed_at=d.get("last_confirmed_at", ""),
            expires_at=d.get("expires_at", ""),
            origin=origin,
            confirmations=confs,
        )


@dataclass
class ArchivedLine:
    """A line that was evicted / expired. Kept for provenance audit."""
    line: BriefLine
    archived_at: str
    archive_reason: str   # "expired" | "evicted_for_room" | "deleted_by_user"
    eviction_displaced_by: Optional[str] = None  # text of the line that bumped this out

    def to_dict(self) -> dict:
        return {
            "line": self.line.to_dict(),
            "archived_at": self.archived_at,
            "archive_reason": self.archive_reason,
            "eviction_displaced_by": self.eviction_displaced_by,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ArchivedLine":
        return cls(
            line=BriefLine.from_dict(d["line"]),
            archived_at=d.get("archived_at", ""),
            archive_reason=d.get("archive_reason", "unknown"),
            eviction_displaced_by=d.get("eviction_displaced_by"),
        )


@dataclass
class Brief:
    """Container for one type of preferences (jobs OR resume)."""
    kind: str                                    # "jobs" or "resume"
    canonical_lang: str = "en"                   # resume language; fallback "en"
    lines: list[BriefLine] = field(default_factory=list)
    archive: list[ArchivedLine] = field(default_factory=list)

    # ── persistence ─────────────────────────────────────────

    @classmethod
    def load(cls, path: Path, kind: str) -> "Brief":
        """Load from JSON, or return empty Brief if missing/corrupt."""
        if not path.exists():
            return cls(kind=kind)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return cls(
                kind=kind,
                canonical_lang=data.get("canonical_lang", "en"),
                lines=[BriefLine.from_dict(d) for d in (data.get("lines") or [])],
                archive=[ArchivedLine.from_dict(d) for d in (data.get("archive") or [])],
            )
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            log.warning("Brief load failed at %s — starting fresh: %s", path, e)
            return cls(kind=kind)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Trim archive to last 100 entries to bound file size
        archive = self.archive[-100:]
        data = {
            "kind": self.kind,
            "canonical_lang": self.canonical_lang,
            "lines": [l.to_dict() for l in self.lines],
            "archive": [a.to_dict() for a in archive],
        }
        # Atomic-ish write: write to temp then replace
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    # ── queries ─────────────────────────────────────────────

    def active_lines(self) -> list[BriefLine]:
        return [l for l in self.lines if not l.is_expired()]

    def expired_lines(self) -> list[BriefLine]:
        return [l for l in self.lines if l.is_expired()]

    def by_axis(self) -> dict[str, list[BriefLine]]:
        out: dict[str, list[BriefLine]] = {}
        for l in self.active_lines():
            out.setdefault(l.axis, []).append(l)
        return out

    def axes_at_warn(self) -> list[str]:
        """Axes with ≥ PER_AXIS_WARN lines — UI suggests merge."""
        return [ax for ax, lines in self.by_axis().items() if len(lines) >= PER_AXIS_WARN]

    def is_at_soft_warn(self) -> bool:
        return len(self.active_lines()) >= SOFT_WARN

    def is_full(self) -> bool:
        return len(self.active_lines()) >= HARD_CAP

    def find(self, line_id: str) -> Optional[BriefLine]:
        for l in self.lines:
            if l.id == line_id:
                return l
        return None

    # ── mutation ────────────────────────────────────────────

    def prune_expired(self) -> int:
        """Move expired lines to archive. Returns count moved."""
        now_iso = _iso(_now())
        kept: list[BriefLine] = []
        moved = 0
        for l in self.lines:
            if l.is_expired():
                self.archive.append(ArchivedLine(
                    line=l, archived_at=now_iso, archive_reason="expired",
                ))
                moved += 1
            else:
                kept.append(l)
        self.lines = kept
        return moved

    def add_or_merge(
        self,
        *,
        text: str,
        side: str,
        axis: str,
        scope: Optional[str],
        confidence: float,
        origin: ConfirmationEntry,
        merge_target_id: Optional[str] = None,
    ) -> tuple[BriefLine, str]:
        """Add a new line OR merge into an existing one.

        merge_target_id: if caller (extractor) determined via LLM that this is
        a duplicate of an existing line, pass that id and we merge into it
        (incrementing confirmed_count, refreshing expiry, appending confirmation).

        Returns: (line, action) where action is "added" | "merged" | "evicted_other".
        """
        if side not in VALID_SIDES:
            raise ValueError(f"invalid side {side!r}")

        if merge_target_id:
            existing = self.find(merge_target_id)
            if existing and not existing.is_expired():
                existing.confirm(origin)
                # Optionally pick clearer phrasing (caller passed in via text)
                if text and len(text) <= 60 and text != existing.text:
                    # Only replace if substantially clearer (heuristic: shorter & non-empty)
                    if len(text) < len(existing.text):
                        existing.text = text
                if scope and not existing.scope:
                    existing.scope = scope
                return existing, "merged"

        # Adding new — check cap
        action = "added"
        active = self.active_lines()
        if len(active) >= HARD_CAP:
            # Evict lowest-score active line
            evictee = min(active, key=lambda l: l.eviction_score())
            self.lines.remove(evictee)
            self.archive.append(ArchivedLine(
                line=evictee,
                archived_at=_iso(_now()),
                archive_reason="evicted_for_room",
                eviction_displaced_by=text,
            ))
            action = "evicted_other"

        new_line = BriefLine.new(
            text=text, side=side, axis=axis, scope=scope,
            confidence=confidence, origin=origin,
        )
        self.lines.append(new_line)
        return new_line, action

    def edit_line(self, line_id: str, *, text: Optional[str] = None,
                  scope: Optional[str] = None, side: Optional[str] = None) -> Optional[BriefLine]:
        l = self.find(line_id)
        if l is None:
            return None
        if text is not None:
            l.text = text
        if scope is not None:
            l.scope = scope or None
        if side is not None and side in VALID_SIDES:
            l.side = side
        return l

    def delete_line(self, line_id: str) -> bool:
        l = self.find(line_id)
        if l is None:
            return False
        self.lines.remove(l)
        self.archive.append(ArchivedLine(
            line=l, archived_at=_iso(_now()), archive_reason="deleted_by_user",
        ))
        return True

    def extend_line(self, line_id: str, days: int = 30) -> Optional[BriefLine]:
        l = self.find(line_id)
        if l is None:
            return None
        new_exp = max(_now(), _parse(l.expires_at) or _now()) + timedelta(days=days)
        l.expires_at = _iso(new_exp)
        return l

    def reactivate_archived(self, archived_idx: int) -> Optional[BriefLine]:
        """Move an archived line back to active. Resets expiry."""
        if archived_idx < 0 or archived_idx >= len(self.archive):
            return None
        archived = self.archive.pop(archived_idx)
        line = archived.line
        now = _now()
        line.last_confirmed_at = _iso(now)
        line.expires_at = _iso(now + timedelta(days=EXPIRY_DAYS))
        self.lines.append(line)
        return line

    # ── prompt formatting ──────────────────────────────────

    def format_for_prompt(self) -> str:
        """Render active lines as a system-prompt section.

        Empty if no active lines — caller can decide whether to omit the block.
        """
        active = self.active_lines()
        if not active:
            return ""
        positives = [l for l in active if l.side == "+"]
        negatives = [l for l in active if l.side == "-"]

        out = ["Recent stated preferences (give moderate weight; primary signal is still the JD/fact content):"]
        if positives:
            out.append("Leans toward:")
            for l in positives:
                weight = "★" * min(3, int(math.sqrt(l.confirmed_count)))
                line = f"  - {l.text}"
                if l.scope:
                    line += f" [{l.scope}]"
                if weight:
                    line += f" {weight}"
                out.append(line)
        if negatives:
            out.append("Leans against:")
            for l in negatives:
                weight = "★" * min(3, int(math.sqrt(l.confirmed_count)))
                line = f"  - {l.text}"
                if l.scope:
                    line += f" [{l.scope}]"
                if weight:
                    line += f" {weight}"
                out.append(line)
        out.append("")
        out.append("Note: preferences may be in a different language than the JD/fact. "
                   "Bridge semantically — don't require literal keyword match. "
                   "Don't override hard JD-vs-resume fit just because a preference matches.")
        return "\n".join(out)


# ─────────────────────────────────────────────────────────
# Module-level convenience: paths + load/save shortcuts
# ─────────────────────────────────────────────────────────


def _data_dir() -> Path:
    """Resolve data dir from Settings if app context, else default."""
    try:
        from flask import current_app
        return current_app.config["SETTINGS"].data_dir
    except Exception:
        return Path("data")


def jobs_brief_path() -> Path:
    return _data_dir() / "preference_brief_jobs.json"


def resume_brief_path() -> Path:
    return _data_dir() / "preference_brief_resume.json"


def load_jobs_brief() -> Brief:
    return Brief.load(jobs_brief_path(), kind="jobs")


def load_resume_brief() -> Brief:
    return Brief.load(resume_brief_path(), kind="resume")


def save_jobs_brief(brief: Brief) -> None:
    brief.save(jobs_brief_path())


def save_resume_brief(brief: Brief) -> None:
    brief.save(resume_brief_path())
