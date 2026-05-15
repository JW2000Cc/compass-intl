"""One-shot migration: Job Mate v1 SQLite → Compass v2 SQLite.

Strategy
--------
This is a "data cleansing" migration, NOT a 1:1 copy:

    v1.GlobalPreferences.target_doc + active resume  →  v2.IdentityVersion + ResumeFact*
    v1.Job (with match_decision != null)              →  v2.Job + v2.JobMatch (tier inferred)
    v1.UserAction (kind=feedback / scrape / etc.)     →  v2.ReflectionEvent
    v1.JobArtifact (kind=tailored_resume)             →  v2.FactVariant (best-effort, may be lossy)

Only data with meaningful signal carries over. Pre-status="new" raw scrapes that
were never reviewed are skipped (cleanup opportunity).

Usage:
    python -m tools.migrate_from_v1 \
        --v1-db "../Job Mate/data/jobmate.sqlite" \
        --dry-run
    # 二者同在 ~/Desktop/我的程序/求职工具/ 下，相对路径即可。
    # remove --dry-run to actually write
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from pathlib import Path

from compass.config import Settings
from compass.extensions import init_engine, session_scope
from compass.models import (
    Base,
    IdentityVersion,
    Job,
    JobMatch,
    ReflectionEvent,
    ResumeFact,
)
from compass.extensions import get_engine

log = logging.getLogger("migrate_v1")


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--v1-db", required=True, help="path to v1 jobmate.sqlite")
    p.add_argument("--dry-run", action="store_true", help="report only, no writes")
    return p.parse_args()


def _infer_tier(decision: str | None, score: int | None) -> int:
    """v1 was binary push/skip. Best-effort tier inference for migration only."""
    if decision == "push":
        if score is not None and score >= 70:
            return 1
        return 2
    if decision == "skip":
        return 4
    return 3


def migrate(v1_path: Path, *, dry_run: bool) -> dict:
    if not v1_path.exists():
        raise FileNotFoundError(f"v1 db not found: {v1_path}")

    v1 = sqlite3.connect(str(v1_path))
    v1.row_factory = sqlite3.Row

    settings = Settings.from_env()
    init_engine(settings)
    Base.metadata.create_all(get_engine())

    counts = {"identity": 0, "facts": 0, "jobs": 0, "matches": 0, "events": 0, "skipped_jobs": 0}

    with session_scope() as session:
        # Identity from v1's resume_profiles
        try:
            rp_rows = list(v1.execute("SELECT * FROM resume_profiles WHERE is_active = 1"))
        except sqlite3.OperationalError:
            rp_rows = []
        for rp in rp_rows[:1]:
            iv = IdentityVersion(
                label="imported_from_v1",
                source_filename=rp["source_filename"] if "source_filename" in rp.keys() else None,
                raw_text=rp["parsed_text"] if "parsed_text" in rp.keys() else None,
                parsed_json=json.loads(rp["parsed_json"]) if rp["parsed_json"] else None,
                language=rp["language"] if "language" in rp.keys() else "en",
                is_current=True,
            )
            if not dry_run:
                session.add(iv)
                session.flush()
            counts["identity"] += 1

            # Best-effort fact extraction from parsed_json
            if iv.parsed_json:
                for exp in (iv.parsed_json.get("experience") or []):
                    text = f"{exp.get('title','')} @ {exp.get('company','')}: {' / '.join(exp.get('bullets') or [])[:300]}"
                    if not dry_run:
                        session.add(ResumeFact(identity_id=iv.id, kind="experience", text=text))
                    counts["facts"] += 1
                for ed in (iv.parsed_json.get("education") or []):
                    text = f"{ed.get('degree','')} in {ed.get('field','')} @ {ed.get('school','')}"
                    if not dry_run:
                        session.add(
                            ResumeFact(
                                identity_id=iv.id,
                                kind="education",
                                text=text,
                                sensitive_category="education_credential",
                            )
                        )
                    counts["facts"] += 1

        # Jobs that have meaningful signal
        try:
            job_rows = list(
                v1.execute(
                    "SELECT * FROM jobs WHERE deleted = 0 AND (match_decision IS NOT NULL OR status != 'new')"
                )
            )
        except sqlite3.OperationalError:
            job_rows = []

        for jr in job_rows:
            j = Job(
                id=jr["id"][:12] if jr["id"] else None,
                title=jr["title"] or "",
                company=jr["company"] or "",
                location=jr["location"],
                link=jr["link"] or "",
                source=jr["source"],
                description=jr["description"],
                posted_at=jr["posted_at"],
                is_remote=bool(jr["is_remote"]) if "is_remote" in jr.keys() else False,
                status=jr["status"] or "new",
            )
            if not dry_run:
                session.add(j)
                session.flush()
            counts["jobs"] += 1

            decision = jr["match_decision"] if "match_decision" in jr.keys() else None
            score = jr["match_score"] if "match_score" in jr.keys() else None
            tier = _infer_tier(decision, score)
            jm = JobMatch(
                job_id=j.id,
                tier=tier,
                score=score,
                reason=jr["match_reason"] if "match_reason" in jr.keys() else None,
                model="migrated_from_v1",
            )
            if not dry_run:
                session.add(jm)
            counts["matches"] += 1

        # Events from UserAction
        try:
            action_rows = list(v1.execute("SELECT * FROM user_actions ORDER BY created_at"))
        except sqlite3.OperationalError:
            action_rows = []

        for ar in action_rows:
            kind = ar["kind"] or "unknown"
            try:
                payload = json.loads(ar["payload_json"]) if ar["payload_json"] else {}
            except (json.JSONDecodeError, TypeError):
                payload = {"raw": str(ar["payload_json"])[:500]}
            mapped_kind = {
                "feedback": "job_thumbed_down",
                "scrape": "external_scrape",
                "match": "job_pushed",
            }.get(kind, kind)
            ev = ReflectionEvent(
                kind=mapped_kind,
                payload_json=payload,
                related_job_id=ar["job_id"] if "job_id" in ar.keys() else None,
            )
            if not dry_run:
                session.add(ev)
            counts["events"] += 1

        # JobArtifact (legacy tailored_resume) — preserve as history events
        # (v1 had whole-document tailored markdown; v2 uses per-fact variants —
        # not 1:1 mappable, so we archive as `legacy_artifact_archived` events.)
        try:
            artifact_rows = list(
                v1.execute(
                    "SELECT id, job_id, kind, content, created_at FROM job_artifacts WHERE kind='tailored_resume'"
                )
            )
        except sqlite3.OperationalError:
            artifact_rows = []

        for ar in artifact_rows:
            ev = ReflectionEvent(
                kind="legacy_artifact_archived",
                payload_json={
                    "v1_artifact_id": ar["id"],
                    "kind": ar["kind"],
                    "content_excerpt": (ar["content"] or "")[:500],
                    "created_at_v1": str(ar["created_at"]) if ar["created_at"] else None,
                },
                related_job_id=ar["job_id"] if ar["job_id"] else None,
            )
            if not dry_run:
                session.add(ev)
            counts.setdefault("legacy_artifacts", 0)
            counts["legacy_artifacts"] += 1

    log.info("migration complete: %s%s", counts, " (DRY RUN)" if dry_run else "")
    return counts


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    args = parse_args()
    counts = migrate(Path(args.v1_db).expanduser(), dry_run=args.dry_run)
    print("Migration summary:")
    for k, v in counts.items():
        print(f"  {k:<15} {v}")
    if args.dry_run:
        print("\n(dry-run — no data written. Re-run without --dry-run to apply.)")


if __name__ == "__main__":
    sys.exit(main() or 0)
