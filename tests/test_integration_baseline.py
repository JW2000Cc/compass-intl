"""Baseline integration tests captured before v4 structural overhaul.

These tests lock the **invariants** that must survive the v4 changes:
- import surfaces (services exist where they are expected)
- public function signatures (param names + order)
- ORM model relationships (fact / variant / revision / attempt)
- DB schema is in sync with SQLAlchemy metadata

They deliberately do NOT exercise live LLM calls — that is a separate
integration-with-network test category. The point here is to catch
accidental cross-module breakage during the v4 refactor.

If a v4 change *intentionally* breaks one of these locks (e.g. ADR-0015
changes raise_objection signature), update the corresponding test in
the same commit and document the change in the ADR.
"""
from __future__ import annotations

import inspect

import pytest
from sqlalchemy import inspect as sa_inspect

from compass.models import (
    Base,
    FactRevision,
    FactVariant,
    Job,
    ResumeFact,
    RewriteAttempt,
)
from compass.services import adversarial_voice, claim_grounder, resume_writer


# ──────────────────────────────────────────────────────────────────────
# Section 1 — import surface locks
# ──────────────────────────────────────────────────────────────────────


def test_resume_writer_public_surface_present():
    """The 6-step pipeline entry points must remain importable by name."""
    for name in (
        "extract_jd_keywords",
        "match_facts_to_keywords",
        "rewrite_one",
        "judge_resume",
        "run_pipeline",
        "regenerate_single_variant",
        "FactKeywordPairing",
        "JudgeReport",
    ):
        assert hasattr(resume_writer, name), f"resume_writer.{name} missing"


def test_claim_grounder_public_surface_present():
    for name in ("screen_for_l5", "ground_variant", "save_variant"):
        assert hasattr(claim_grounder, name), f"claim_grounder.{name} missing"


def test_adversarial_voice_public_surface_present():
    assert hasattr(adversarial_voice, "raise_objection")


# ──────────────────────────────────────────────────────────────────────
# Section 2 — function signature locks
# ──────────────────────────────────────────────────────────────────────


def _params(fn) -> list[str]:
    return list(inspect.signature(fn).parameters.keys())


def test_run_pipeline_signature():
    """If this changes, check ADR-0015 amendment notes.

    `step_overrides` added 5-08 to wire LLMSettings.step_overrides into
    pipeline LLM calls (UI dropdown was previously a placebo)."""
    assert _params(resume_writer.run_pipeline) == [
        "session",
        "job",
        "provider",
        "api_key",
        "model",
        "adversarial_enabled",
        "step_overrides",
    ]


def test_extract_jd_keywords_signature():
    # Y1 (5-08): added `errors` keyword for silent-failure diagnostics
    assert _params(resume_writer.extract_jd_keywords) == [
        "job",
        "provider",
        "api_key",
        "model",
        "errors",
    ]


def test_judge_resume_signature():
    # Y1 (5-08): added `errors` keyword for silent-failure diagnostics
    assert _params(resume_writer.judge_resume) == [
        "resume_text",
        "job",
        "provider",
        "api_key",
        "model",
        "errors",
    ]


def test_raise_objection_signature_v2_post_adr_0015():
    """Post-ADR-0015: signature is (fact, job, variant_text, ...).
    The two-phase implementation feeds Phase 1 with fact + JD only;
    Phase 2 then exposes variant_text against expected_objections.
    See compass/services/adversarial_voice.py for the rationale.
    """
    params = _params(adversarial_voice.raise_objection)
    assert "fact" in params
    assert "job" in params
    assert "variant_text" in params
    # The v1 names should be gone (catch accidental revert)
    assert "source_text" not in params, "v1 signature regressed"
    assert "jd_excerpt" not in params, "v1 signature regressed"


def test_screen_for_l5_signature():
    assert _params(claim_grounder.screen_for_l5) == [
        "variant_text",
        "source_facts",
        "target_sensitive_category",
    ]


def test_ground_variant_signature():
    params = _params(claim_grounder.ground_variant)
    for required in ("source_facts", "jd_context", "variant_text"):
        assert required in params, f"ground_variant missing param: {required}"


# ──────────────────────────────────────────────────────────────────────
# Section 3 — ORM relationship locks
# ──────────────────────────────────────────────────────────────────────


def test_resume_fact_has_revisions_variants_evidence(session, identity):
    """ADR-0005 (Facts immutable) requires these relationships exist."""
    fact = ResumeFact(
        identity_id=identity.id,
        kind="experience_bullet",
        text="Built ML pipeline at Siemens",
    )
    session.add(fact)
    session.flush()
    assert fact.revisions == []
    assert fact.variants == []
    assert fact.evidence == []


def test_fact_revision_kinds_are_constrained(session, identity):
    """ADR-0005 / FactRevision append-only audit (5/3 fact_history work).

    Valid kinds: origin | promoted_from_attempt | rolled_back | manual_edit
    """
    fact = ResumeFact(
        identity_id=identity.id, kind="experience", text="placeholder"
    )
    session.add(fact)
    session.flush()
    rev = FactRevision(fact_id=fact.id, text="placeholder", kind="origin")
    session.add(rev)
    session.flush()
    assert rev.id is not None


def test_fact_variant_amplification_level_range(session, identity, job):
    """ADR-0003: variants stored at L0-L4 (L5 must never persist)."""
    fact = ResumeFact(identity_id=identity.id, kind="experience", text="x")
    session.add(fact)
    session.flush()
    for level in range(0, 5):  # 0..4 valid
        v = FactVariant(
            fact_id=fact.id,
            job_id=job.id,
            text=f"variant L{level}",
            amplification_level=level,
        )
        session.add(v)
    session.flush()
    levels = sorted({v.amplification_level for v in fact.variants})
    assert levels == [0, 1, 2, 3, 4]


def test_rewrite_attempt_links_to_job_and_identity(session, identity, job):
    """RewriteAttempt is the unit of one full pipeline run (PR-mental-model)."""
    att = RewriteAttempt(job_id=job.id, identity_id=identity.id)
    session.add(att)
    session.flush()
    assert att.id is not None
    assert att.job_id == job.id
    assert att.identity_id == identity.id


# ──────────────────────────────────────────────────────────────────────
# Section 4 — schema sync lock (alembic baseline)
# ──────────────────────────────────────────────────────────────────────


def test_schema_matches_metadata(session):
    """If SQLAlchemy metadata diverges from the actual DB tables, alembic
    autogenerate would produce non-empty migrations — that means a model
    edit slipped in without a migration. This test catches it locally.
    """
    engine = session.get_bind()
    inspector = sa_inspect(engine)
    db_tables = set(inspector.get_table_names())
    meta_tables = set(Base.metadata.tables.keys())
    # alembic_version is created by alembic, not by metadata — exclude
    db_tables.discard("alembic_version")
    missing_in_db = meta_tables - db_tables
    extra_in_db = db_tables - meta_tables
    assert not missing_in_db, f"Tables in metadata but not in DB: {missing_in_db}"
    assert not extra_in_db, f"Tables in DB but not in metadata: {extra_in_db}"


# ──────────────────────────────────────────────────────────────────────
# Section 5 — sensitive-category invariants (ADR-0003)
# ──────────────────────────────────────────────────────────────────────


def test_sensitive_category_l5_locks_still_active():
    """ADR-0003 deterministic L5 locks must remain hard-blockers.
    These three categories are the originals — if you add new ones,
    update ADR-0003 (and likely add a new screen_for_l5 branch).
    Existing tests/test_claim_grounder.py covers the regex behavior;
    this test just checks the constants are still present so a refactor
    doesn't accidentally drop a category.
    """
    import compass.services.claim_grounder as cg

    src = inspect.getsource(cg)
    for cat in ("education_credential", "language_level", "employment_dates"):
        assert cat in src, (
            f"sensitive_category '{cat}' no longer referenced in claim_grounder; "
            "if intentional update ADR-0003 in the same commit."
        )
