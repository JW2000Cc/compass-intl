"""LLM silent-failure diagnostics — surface what got swallowed.

Pattern (5-08 audit): we have ~10 places where `except LLMError: return X`
silently degrades on transient 401/429/timeout. Users hit 'composite 0/10'
or 'no keywords extracted' with no clue. Same class as the scraper
gate_blocked silent count we already fixed.

Two collection mechanisms:
  · Explicit `errors: list[str] | None = None` parameter — propagate through
    pipelines (run_pipeline → extract_jd_keywords → ...). When passed, each
    silent catch site appends. Routes read after.
  · `record_to_event(session, ...)` — for sites without a pipeline collector,
    write a ReflectionEvent('llm_silent_failure'). Dashboards aggregate.

Both can coexist; they don't interfere.
"""
from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger(__name__)


def record(
    errors: Optional[list],
    step: str,
    exc: Exception,
    *,
    context: Optional[str] = None,
) -> None:
    """Append a structured silent-failure entry to a caller-provided list.

    Caller decides what to do with the collected list (flash / event / dump).
    Always also log.warning so stale logs reveal silent failures.
    """
    parts = [f"{step}: {type(exc).__name__}"]
    if context:
        parts.append(f"({context})")
    msg_head = str(exc)[:160]
    if msg_head:
        parts.append(f"— {msg_head}")
    text = " ".join(parts)
    log.warning("LLM silent failure | %s", text)
    if errors is not None:
        errors.append(text)


def record_to_event(
    session,
    *,
    step: str,
    exc: Exception,
    context: Optional[str] = None,
    related_job_id: Optional[str] = None,
) -> None:
    """For sites without a pipeline collector — write to ReflectionEvent stream.

    Best-effort: if session.add fails (e.g. transaction rolled back), log and
    move on; we don't want diagnostics to cascade-break the caller.
    """
    try:
        from ..models import ReflectionEvent
        session.add(
            ReflectionEvent(
                kind="llm_silent_failure",
                payload_json={
                    "step": step,
                    "error_type": type(exc).__name__,
                    "error_message": str(exc)[:300],
                    "context": context,
                },
                related_job_id=related_job_id,
            )
        )
    except Exception:  # noqa: BLE001
        log.exception("failed to record llm silent failure event")
    log.warning("LLM silent failure | %s: %s%s", step, type(exc).__name__, f" {context}" if context else "")


def summarize_for_flash(errors: list[str], *, max_show: int = 2) -> str:
    """Render a list of recorded errors for a one-line flash.

    Caller already attaches the success line ("流水线完成"); we only return
    the warning suffix.
    """
    if not errors:
        return ""
    head = " · ".join(errors[:max_show])
    suffix = f"... 还有 {len(errors) - max_show} 条" if len(errors) > max_show else ""
    return f"⚠ {len(errors)} 个 LLM 步骤静默回退: {head}{suffix}"
