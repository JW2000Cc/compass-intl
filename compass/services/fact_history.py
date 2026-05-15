"""Append-only fact text history.

Every change to ResumeFact.text writes a FactRevision row; this module is the
single chokepoint so we never silently mutate without auditing.

Design notes:
  - REVISIONS ARE APPEND-ONLY. Don't add a delete function. To undo, call
    `rollback_to(...)` which writes a NEW revision with old content. (See
    user-thread on git-rebase-style holes — deleting a revision in the
    middle of a chain is semantically undefined.)
  - The first revision per fact has kind="origin" and is auto-bootstrapped
    on first write so existing facts (created before this module existed)
    don't end up with an empty history.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import FactRevision, ResumeFact

VALID_KINDS = {"origin", "promoted_from_attempt", "rolled_back", "manual_edit"}


def _has_origin(session: Session, fact_id: str) -> bool:
    return (
        session.execute(
            select(FactRevision.id).where(
                FactRevision.fact_id == fact_id,
                FactRevision.kind == "origin",
            ).limit(1)
        ).scalar_one_or_none()
        is not None
    )


def ensure_origin(session: Session, fact: ResumeFact) -> None:
    """Idempotent: write an `origin` revision if the fact has none yet.

    Lets us migrate historical facts (created before fact_revisions existed)
    without an explicit ALTER/migration step.
    """
    if _has_origin(session, fact.id):
        return
    session.add(
        FactRevision(
            fact_id=fact.id,
            text=fact.text,
            kind="origin",
            note="auto-bootstrapped from existing fact text",
        )
    )
    session.flush()


def write_revision(
    session: Session,
    *,
    fact: ResumeFact,
    new_text: str,
    kind: str,
    source_attempt_id: Optional[str] = None,
    source_job_id: Optional[str] = None,
    source_variant_id: Optional[str] = None,
    note: Optional[str] = None,
) -> Optional[FactRevision]:
    """Append a new revision and update fact.text. No-op if text unchanged.

    Returns the new FactRevision row (or None if text was identical).
    """
    if kind not in VALID_KINDS:
        raise ValueError(f"invalid revision kind: {kind!r}")
    if kind == "origin":
        # origin is internal; callers should use ensure_origin
        raise ValueError("use ensure_origin() to write origin revisions")

    new_text = (new_text or "").strip()
    if not new_text:
        return None

    # Bootstrap origin lazily so the chain is complete
    ensure_origin(session, fact)

    if new_text == fact.text:
        return None  # no-op — don't pollute history with identical revisions

    rev = FactRevision(
        fact_id=fact.id,
        text=new_text,
        kind=kind,
        source_attempt_id=source_attempt_id,
        source_job_id=source_job_id,
        source_variant_id=source_variant_id,
        note=note,
    )
    session.add(rev)
    fact.text = new_text
    session.flush()
    return rev


def list_revisions(session: Session, fact_id: str) -> list[FactRevision]:
    """All revisions for a fact, newest first. Bootstrap origin if missing."""
    fact = session.get(ResumeFact, fact_id)
    if fact is not None:
        ensure_origin(session, fact)
    return list(
        session.execute(
            select(FactRevision)
            .where(FactRevision.fact_id == fact_id)
            .order_by(FactRevision.created_at.desc())
        )
        .scalars()
        .all()
    )


def rollback_to(
    session: Session, *, fact: ResumeFact, target_revision_id: str
) -> Optional[FactRevision]:
    """↺ Set old revision as current — writes a NEW `rolled_back` revision.

    Never mutates or deletes the target revision; the chain stays intact.
    """
    target = session.get(FactRevision, target_revision_id)
    if target is None or target.fact_id != fact.id:
        raise ValueError("revision not found or does not belong to this fact")
    return write_revision(
        session,
        fact=fact,
        new_text=target.text,
        kind="rolled_back",
        note=f"rolled back to revision {target.id} ({target.created_at:%Y-%m-%d %H:%M})",
    )
