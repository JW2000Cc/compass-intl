"""Compass data model.

Conceptual layers (mirrored in core.py, kept in one file to avoid import cycles):

    Identity layer    — IdentityVersion, ResumeFact, FactEvidence
    Variant layer     — FactVariant, RewriteAttempt
    Interview layer   — InterviewSession, InterviewQA
    Reflection layer  — ReflectionEvent (event sourcing), ToolAssumption
    Calibration layer — UserCalibration, CalibrationDriftPoint
    Job layer         — Job, JobMatch (with tier 1-5)
    Trash layer       — DeletedRecord (soft-delete archive)
"""
from __future__ import annotations

from .core import (
    Base,
    CalibrationDriftPoint,
    Connection,
    DeletedRecord,
    EffortPack,
    FactEvidence,
    FactRevision,
    FactVariant,
    IdentityVersion,
    InterviewQA,
    InterviewSession,
    Job,
    JobMatch,
    OutreachAttempt,
    ReflectionEvent,
    ResumeFact,
    RewriteAttempt,
    ToolAssumption,
    UserCalibration,
)

__all__ = [
    "Base",
    "IdentityVersion",
    "ResumeFact",
    "FactEvidence",
    "FactRevision",
    "FactVariant",
    "RewriteAttempt",
    "InterviewSession",
    "InterviewQA",
    "ReflectionEvent",
    "ToolAssumption",
    "UserCalibration",
    "CalibrationDriftPoint",
    "Job",
    "JobMatch",
    "Connection",
    "OutreachAttempt",
    "EffortPack",
    "DeletedRecord",
]
