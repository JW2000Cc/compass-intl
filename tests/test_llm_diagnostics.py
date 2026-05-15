"""Tests for the LLM silent-failure diagnostics helper (Y series).

Pinpoints the bug: pre-fix `except LLMError: return []` patterns silently
degraded resume_writer / adversarial_voice / identity_engine pipelines.
Users saw "composite 0/10" with no clue. Helper threads errors list through
each step; routes flash warn level when warnings collected.
"""
from __future__ import annotations

from compass.services.llm_diagnostics import (
    record,
    summarize_for_flash,
)


def test_record_appends_to_caller_list():
    errs: list[str] = []
    record(errs, "step_x", RuntimeError("api 401"))
    assert len(errs) == 1
    assert "step_x" in errs[0]
    assert "RuntimeError" in errs[0]
    assert "api 401" in errs[0]


def test_record_with_none_collector_is_noop_but_logs():
    """When pipeline doesn't collect, record should still log.warning but not
    raise (and obviously not append to nothing)."""
    record(None, "step_y", ValueError("transient timeout"))


def test_record_includes_context():
    errs: list[str] = []
    record(errs, "step_z", RuntimeError("boom"), context="job_id=abc123")
    assert "job_id=abc123" in errs[0]


def test_record_truncates_long_error_message():
    errs: list[str] = []
    long = "x" * 5000
    record(errs, "step_long", RuntimeError(long))
    # message head capped at 160 in our format
    assert len(errs[0]) < 300


def test_summarize_for_flash_empty():
    assert summarize_for_flash([]) == ""


def test_summarize_for_flash_one_entry():
    msg = summarize_for_flash(["step_x: LLMError — 401"])
    assert "1 个 LLM" in msg
    assert "step_x" in msg


def test_summarize_for_flash_truncates_long_list():
    errs = [f"step_{i}: LLMError" for i in range(5)]
    msg = summarize_for_flash(errs, max_show=2)
    assert "5 个 LLM" in msg
    assert "step_0" in msg
    assert "step_1" in msg
    assert "还有 3 条" in msg


def test_record_to_event_writes_reflection_event(session):
    """record_to_event writes ReflectionEvent("llm_silent_failure") into the
    session. Dashboard's silent-failure counter reads these."""
    from compass.services.llm_diagnostics import record_to_event
    from compass.models import ReflectionEvent
    from sqlalchemy import select

    record_to_event(
        session,
        step="dashboard_test_step",
        exc=RuntimeError("test failure"),
        context="unit test",
    )
    session.flush()
    rows = session.execute(
        select(ReflectionEvent).where(ReflectionEvent.kind == "llm_silent_failure")
    ).scalars().all()
    assert len(rows) == 1
    payload = rows[0].payload_json or {}
    assert payload.get("step") == "dashboard_test_step"
    assert payload.get("error_type") == "RuntimeError"
    assert "test failure" in (payload.get("error_message") or "")
