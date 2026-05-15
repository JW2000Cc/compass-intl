"""All Compass ORM models in one file to avoid import cycles.

Design notes
------------
- IdentityVersion is the "main branch" of the user's resume facts.
  ResumeFact rows are immutable once created — edits create new versions.
- FactVariant rows are the rewrites/translations of a fact for a specific job.
  variants reference (fact_id, job_id, level 0-5). Level 5 is hard-banned at
  service layer, never stored.
- ReflectionEvent is the event source — every meaningful user/system action
  appends an event. Drift dashboards read this stream.
- UserCalibration is the learned user boundary; CalibrationDriftPoint is
  a snapshot taken every N days for the drift dashboard.
- ToolAssumption is the system's *current* belief about the user, generated
  periodically from facts + reflections, ALWAYS user-editable.
- DeletedRecord is the audit trail — nothing actually deletes from this DB
  except via explicit purge action.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..extensions import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _uuid() -> str:
    return uuid.uuid4().hex[:12]


# ─────────────────────────────────────────────────────────
# Identity layer — facts are immutable; edits create new versions
# ─────────────────────────────────────────────────────────


class IdentityVersion(Base):
    """A snapshot of the user's identity at a point in time.

    Each upload of a new resume / major life event creates a new version.
    Old versions are kept (for drift analysis), only one is_current=True.
    """
    __tablename__ = "identity_versions"

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=_uuid)
    label: Mapped[str] = mapped_column(String(120), default="")
    source_filename: Mapped[Optional[str]] = mapped_column(String(300))
    raw_text: Mapped[Optional[str]] = mapped_column(Text)
    parsed_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)
    language: Mapped[str] = mapped_column(String(8), default="en")
    is_current: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    facts: Mapped[list["ResumeFact"]] = relationship(back_populates="identity")


class ResumeFact(Base):
    """An atomic fact about the user.

    Examples:
        kind="experience", text="Built electrical grid simulation in coursework"
        kind="skill", text="Python (used in 3 projects, ~6 months total)"
        kind="education", text="BS Electrical Engineering, XYZ University, 2024"

    `text` reflects the CURRENT version. Every time it changes (via promote-from-attempt,
    rollback, or manual_edit) a `FactRevision` row is appended for audit + rollback.
    The first revision (kind="origin") is auto-bootstrapped on first access.

    `structured_json` holds the ADR-0015 element decomposition (Sanity-style
    field-level localization). Schema:

        {
          "verb":   {"canonical": "built", "en": "Built", "it": "Sviluppato"},
          "metric": {"value": 23, "unit": "%", "display": {"en": "23%", "it": "del 23%"}},
          "object": {"en": "ML pipeline", "it": "pipeline ML"},
          "org_canonical": "Siemens",     # locale-invariant — never translated
          "context_canonical": "fraud detection",
        }

    Rules:
      - `metric.value` and `org_canonical` are locale-invariant; never enter LLM as
        text-to-translate (代码 directly emits in render layer).
      - Missing locale on a content field is NOT silent fallback. Render layer
        flags `requires_interview: True` so the user is asked instead of letting
        an automated machine translation slip into the resume.
      - Element schema is OPTIONAL — pre-v4 facts have `structured_json=None`
        and stay readable; element-aware features become available once the
        element decomposer fills it in.
    """
    __tablename__ = "resume_facts"

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=_uuid)
    identity_id: Mapped[str] = mapped_column(ForeignKey("identity_versions.id"))
    kind: Mapped[str] = mapped_column(String(40))  # experience|education|skill|project|cert|language
    text: Mapped[str] = mapped_column(Text)
    structured_json: Mapped[Optional[dict]] = mapped_column(JSON)
    # Anti-fabrication metadata - filled from interview engine
    user_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    sensitive_category: Mapped[Optional[str]] = mapped_column(String(40))
    # e.g. "education_credential", "language_level", "employment_dates" -- triggers L5 hard ban
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    identity: Mapped[IdentityVersion] = relationship(back_populates="facts")
    evidence: Mapped[list["FactEvidence"]] = relationship(back_populates="fact")
    variants: Mapped[list["FactVariant"]] = relationship(back_populates="fact")
    revisions: Mapped[list["FactRevision"]] = relationship(
        back_populates="fact", order_by="FactRevision.created_at.desc()"
    )


class FactRevision(Base):
    """Append-only history of every change to a `ResumeFact.text`.

    Created in 4 situations (kind):
      - origin                 — bootstrap: snapshot of original text on first access
      - promoted_from_attempt  — user clicked 📌 沉淀 in a review panel
      - rolled_back            — user clicked ↺ 设为当前 on an older revision
      - manual_edit            — user typed a new text in /identity fact detail

    The CURRENT text always equals the latest revision's text. To "delete" a
    revision is forbidden by design (see thread on rebase-style history bugs);
    use rolled_back instead — that creates a NEW revision with the old text.
    """
    __tablename__ = "fact_revisions"

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=_uuid)
    fact_id: Mapped[str] = mapped_column(ForeignKey("resume_facts.id"), index=True)
    text: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(30))
    # Provenance — nullable because not every kind has all sources
    source_attempt_id: Mapped[Optional[str]] = mapped_column(String(12))
    source_job_id: Mapped[Optional[str]] = mapped_column(String(12))
    source_variant_id: Mapped[Optional[str]] = mapped_column(String(12))
    note: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)

    fact: Mapped[ResumeFact] = relationship(back_populates="revisions")


class FactEvidence(Base):
    """Provenance link: where did this fact come from?

    Sources include 'resume_upload', 'interview_qa', 'user_manual_input'.
    Used for grounding — every fact must have at least one evidence entry.
    """
    __tablename__ = "fact_evidence"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    fact_id: Mapped[str] = mapped_column(ForeignKey("resume_facts.id"))
    source_kind: Mapped[str] = mapped_column(String(40))
    source_ref: Mapped[Optional[str]] = mapped_column(String(300))  # filename / interview_id / etc.
    excerpt: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    fact: Mapped[ResumeFact] = relationship(back_populates="evidence")


# ─────────────────────────────────────────────────────────
# Variant layer — fact rewrites for specific jobs
# ─────────────────────────────────────────────────────────


class FactVariant(Base):
    """A variant rewrite of a fact for a target job.

    amplification_level rules:
        0  direct copy
        1  synonym rewrite ("maintained" → "operated")
        2  implicit→explicit (audience/scope made explicit)
        3  reframing (same fact, different lens — needs interview confirmation)
        4  inferred quantification (numbers added — needs user approval)
        5  fabrication (NEVER stored — service layer rejects)
    """
    __tablename__ = "fact_variants"

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=_uuid)
    fact_id: Mapped[str] = mapped_column(ForeignKey("resume_facts.id"))
    job_id: Mapped[Optional[str]] = mapped_column(ForeignKey("jobs.id"))
    text: Mapped[str] = mapped_column(Text)
    amplification_level: Mapped[int] = mapped_column(Integer, default=0)
    user_decision: Mapped[str] = mapped_column(String(20), default="pending")
    # pending | approved | rejected | edited_by_user
    rejection_reason: Mapped[Optional[str]] = mapped_column(String(300))
    interview_qa_used: Mapped[Optional[list]] = mapped_column(JSON)  # list of qa_ids
    adversarial_objection: Mapped[Optional[str]] = mapped_column(Text)  # what the "刚正声音" said
    # ADR-0015 五维度结构化建议菜单 — populated by bullet_decomposer:
    #   {"verb": {...}, "metric": {...}, "jd_keywords": {...},
    #    "length": {...}, "tone": {...}}
    # Each dimension carries: candidates, grounding_label, source (code|llm|user_decision),
    # auto_fix (when applicable), needs_interview (bool).
    decomposition_json: Mapped[Optional[dict]] = mapped_column(JSON)
    # ADR-0015 多语言渲染层 — variant_translator output:
    #   {"it": "Sviluppato pipeline ML...", "fr": "...", "zh": "..."}
    # Source language matches identity.language; locale-invariant elements
    # (metric.value, org_canonical) are stitched in by code, not LLM-translated.
    translated_json: Mapped[Optional[dict]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    fact: Mapped[ResumeFact] = relationship(back_populates="variants")


class RewriteAttempt(Base):
    """Each tailored-resume generation = one attempt = many variants.

    Stored for replay / debug / drift analysis.
    """
    __tablename__ = "rewrite_attempts"

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=_uuid)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"))
    identity_id: Mapped[str] = mapped_column(ForeignKey("identity_versions.id"))
    keywords_extracted: Mapped[Optional[list]] = mapped_column(JSON)
    judge_score: Mapped[Optional[float]] = mapped_column(Float)
    judge_report: Mapped[Optional[dict]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


# ─────────────────────────────────────────────────────────
# Interview layer — fact discovery via dialogue
# ─────────────────────────────────────────────────────────


class InterviewSession(Base):
    """A discovery dialogue about a fact (or about a JD)."""
    __tablename__ = "interview_sessions"

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=_uuid)
    target_kind: Mapped[str] = mapped_column(String(20))  # fact | job | drift_review
    target_id: Mapped[Optional[str]] = mapped_column(String(40))
    purpose: Mapped[str] = mapped_column(String(200), default="")
    completed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    qas: Mapped[list["InterviewQA"]] = relationship(back_populates="session")


class InterviewQA(Base):
    """Single Q-A pair within an interview session."""
    __tablename__ = "interview_qas"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("interview_sessions.id"))
    question: Mapped[str] = mapped_column(Text)
    answer: Mapped[Optional[str]] = mapped_column(Text)
    answer_skipped: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    session: Mapped[InterviewSession] = relationship(back_populates="qas")


# ─────────────────────────────────────────────────────────
# Reflection layer — event sourcing for the drift dashboard
# ─────────────────────────────────────────────────────────


class ReflectionEvent(Base):
    """Append-only event stream — the spine of Compass's reflection layer.

    Every meaningful action appends one event. Drift, assumption, and
    self-report features all read from this stream.

    kind taxonomy:
        identity_uploaded
        fact_added | fact_deactivated
        variant_proposed | variant_approved | variant_rejected
        job_pushed | job_thumbed_up | job_thumbed_down
        job_status_changed
        calibration_changed
        assumption_regenerated | assumption_overridden
        adversarial_objection_raised
        drift_alert
    """
    __tablename__ = "reflection_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(40), index=True)
    payload_json: Mapped[Optional[dict]] = mapped_column(JSON)
    related_fact_id: Mapped[Optional[str]] = mapped_column(String(12))
    related_job_id: Mapped[Optional[str]] = mapped_column(String(12))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)


class ToolAssumption(Base):
    """The system's current belief about the user.

    Regenerated every N days from facts + reflections. ALWAYS visible to user.
    Each assumption has an override field — user can replace any assumption,
    and the override sticks until they clear it.
    """
    __tablename__ = "tool_assumptions"

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=_uuid)
    category: Mapped[str] = mapped_column(String(40))
    # values_held | preferred_role_directions | growth_areas | risk_tolerance | etc.
    text: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    user_override: Mapped[Optional[str]] = mapped_column(Text)  # user's replacement
    is_current: Mapped[bool] = mapped_column(Boolean, default=True)
    generated_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


# ─────────────────────────────────────────────────────────
# Calibration layer — learned ethical boundaries
# ─────────────────────────────────────────────────────────


class UserCalibration(Base):
    """Singleton (id=1). The user's currently-learned boundaries."""
    __tablename__ = "user_calibrations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    max_amplification_level: Mapped[int] = mapped_column(Integer, default=2)
    preferred_verbs_do: Mapped[list[str]] = mapped_column(JSON, default=list)
    preferred_verbs_avoid: Mapped[list[str]] = mapped_column(JSON, default=list)
    sensitive_categories_locked: Mapped[list[str]] = mapped_column(JSON, default=list)
    style_rules: Mapped[list[dict]] = mapped_column(JSON, default=list)
    last_review_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class CalibrationDriftPoint(Base):
    """Periodic snapshot of calibration state — feeds the drift dashboard.

    Captured by a background job every drift_check_interval_days.
    """
    __tablename__ = "calibration_drift_points"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    snapshot_json: Mapped[dict] = mapped_column(JSON)
    captured_at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)


# ─────────────────────────────────────────────────────────
# Job layer — kept thin; tier matcher is the upgrade
# ─────────────────────────────────────────────────────────


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=_uuid)
    title: Mapped[str] = mapped_column(String(500))
    company: Mapped[str] = mapped_column(String(300))
    location: Mapped[Optional[str]] = mapped_column(String(300))
    link: Mapped[str] = mapped_column(String(1000))
    source: Mapped[Optional[str]] = mapped_column(String(50))
    description: Mapped[Optional[str]] = mapped_column(Text)
    description_translated: Mapped[Optional[str]] = mapped_column(Text)
    description_translated_lang: Mapped[Optional[str]] = mapped_column(String(8))
    posted_at: Mapped[Optional[str]] = mapped_column(String(40))
    is_remote: Mapped[bool] = mapped_column(Boolean, default=False)
    salary_min: Mapped[Optional[float]] = mapped_column(Float)
    salary_max: Mapped[Optional[float]] = mapped_column(Float)
    salary_currency: Mapped[Optional[str]] = mapped_column(String(8))
    status: Mapped[str] = mapped_column(String(30), default="new")
    # new | reviewed | applied | interview | offer | rejected | passed
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)
    status_changed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    # ADR-0016: which job_search_config.json profile召回了this job. Nullable for
    # legacy rows / ad-hoc scrapes without a profile context. NOT a FK — profiles
    # live in JSON config, not a DB table.
    matched_profile_id: Mapped[Optional[str]] = mapped_column(String(40))

    matches: Mapped[list["JobMatch"]] = relationship(back_populates="job")


class JobMatch(Base):
    """A tier-classification result for a job.

    Replaces v1's binary push/skip with a 5-tier model:
        1 — core (matches your background directly)
        2 — adjacent (transferable; main anti-bubble target)
        3 — learnable (need to learn 1-2 things; learning_gap filled)
        4 — distant (only push if user signals interest)
        5 — unreachable (NEVER pushed; stored for transparency)
    """
    __tablename__ = "job_matches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"))
    tier: Mapped[int] = mapped_column(Integer)
    score: Mapped[Optional[int]] = mapped_column(Integer)  # 0-10
    reason: Mapped[Optional[str]] = mapped_column(Text)
    learning_gap: Mapped[Optional[str]] = mapped_column(Text)
    model: Mapped[Optional[str]] = mapped_column(String(80))
    matched_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    job: Mapped[Job] = relationship(back_populates="matches")


# ─────────────────────────────────────────────────────────
# Trash layer — soft-delete audit trail
# ─────────────────────────────────────────────────────────


class DeletedRecord(Base):
    """Anything 'deleted' is moved here (model + json snapshot).

    Compass never hard-deletes user data without an explicit purge action.
    """
    __tablename__ = "deleted_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    model_name: Mapped[str] = mapped_column(String(40))
    original_id: Mapped[str] = mapped_column(String(40))
    snapshot_json: Mapped[dict] = mapped_column(JSON)
    reason: Mapped[Optional[str]] = mapped_column(String(200))
    deleted_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


# ─────────────────────────────────────────────────────────
# Connection graph — people the user already knows.
# Imported from LinkedIn CSV, GitHub network, manual entry.
# Used as the "warm" layer in find-contacts UX.
# ─────────────────────────────────────────────────────────


class Connection(Base):
    """A person the user knows. Source: LinkedIn CSV import / GitHub / manual."""
    __tablename__ = "connections"

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(200))
    current_company: Mapped[Optional[str]] = mapped_column(String(200))
    current_title: Mapped[Optional[str]] = mapped_column(String(300))
    linkedin_url: Mapped[Optional[str]] = mapped_column(String(400))
    email: Mapped[Optional[str]] = mapped_column(String(200))
    source: Mapped[str] = mapped_column(String(40))
    # "linkedin_csv" | "github" | "manual" | "alumni" | ...
    connected_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    # When the user added them as a connection (LinkedIn "Connected On" column)
    notes: Mapped[Optional[str]] = mapped_column(Text)
    last_contacted_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    imported_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


# ─────────────────────────────────────────────────────────
# Outreach attempts — closed-loop feedback for warm/cold contact
# This is the seed data for 改造 1 (闭环数据流). Every outreach the
# user sends produces a row here. Later analysis reads it back to
# answer "what kind of outreach actually works for me".
# ─────────────────────────────────────────────────────────


class EffortPack(Base):
    """Per-job effort checklist state.

    Most steps are *auto-derived* from other tables (RewriteAttempt /
    OutreachAttempt / Job.status / etc) — this table only stores the explicit
    "user marked it done by hand" overrides. So the absence of an EffortPack
    row for a job just means "no manual overrides", not "no progress".

    See services/effort_pack.py for the derivation logic.
    """
    __tablename__ = "effort_packs"

    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), primary_key=True)
    manual_overrides_json: Mapped[Optional[dict]] = mapped_column(JSON)
    # Maps step_id → "done" | "skipped" | "n/a"
    notes: Mapped[Optional[str]] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    actual_minutes: Mapped[Optional[int]] = mapped_column(Integer)


class OutreachAttempt(Base):
    __tablename__ = "outreach_attempts"

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=_uuid)
    job_id: Mapped[Optional[str]] = mapped_column(ForeignKey("jobs.id"))
    # Who they reached out to. Either an existing Connection (warm) or a
    # cold contact resolved via search (we store profile_url as anchor).
    connection_id: Mapped[Optional[str]] = mapped_column(ForeignKey("connections.id"))
    contact_name: Mapped[str] = mapped_column(String(200))
    contact_title: Mapped[Optional[str]] = mapped_column(String(300))
    contact_company: Mapped[Optional[str]] = mapped_column(String(200))
    contact_url: Mapped[Optional[str]] = mapped_column(String(400))

    path: Mapped[str] = mapped_column(String(20))
    # "warm" (was in user's connection graph) | "cold" (search-engine discovery)
    purpose: Mapped[str] = mapped_column(String(40))
    # "referral_request" | "informational" | "direct_question" | "intro"

    draft_text: Mapped[Optional[str]] = mapped_column(Text)
    # sent_at=None → still a draft (not yet actually sent)
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=None)

    # Closed-loop fields — user fills these in after the fact (or never)
    response_received: Mapped[Optional[bool]] = mapped_column(Boolean)
    response_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    response_kind: Mapped[Optional[str]] = mapped_column(String(40))
    # "accepted" | "ignored" | "declined" | "referred" | "intro_made"
    led_to_interview: Mapped[Optional[bool]] = mapped_column(Boolean)
    led_to_offer: Mapped[Optional[bool]] = mapped_column(Boolean)

    notes: Mapped[Optional[str]] = mapped_column(Text)
