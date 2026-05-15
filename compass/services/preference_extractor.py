"""Extract structured preference signal from user's free-form NL feedback.

Single LLM call per feedback submission:
    Input  — user's NL text + context (job or resume) + existing brief lines
    Output — 1-3 structured lines + per-line "merge into existing line X" decision

Multi-language: prompt explicitly asks LLM to bridge across languages and to
dedupe semantic duplicates of any existing line regardless of phrasing.

Anchor (resume side only): if user's feedback is bullet-specific, extractor
records bullet_index in the resulting confirmation entry.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from .llm import LLMConfigError, chat
from .preference_brief import (
    JOB_AXES,
    RESUME_AXES,
    Brief,
    BriefLine,
    ConfirmationEntry,
)

log = logging.getLogger(__name__)


_JOB_AXES_HINT = """role | company_stage | location | comp | scope | culture | remote | industry"""
_RESUME_AXES_HINT = """bullet_length | quantification | leadership_claim | tone | technical_depth | structure | wording"""


def _system_prompt(context: str, canonical_lang: str, existing: list[BriefLine]) -> str:
    is_job = context == "jobs"
    axes_hint = _JOB_AXES_HINT if is_job else _RESUME_AXES_HINT
    existing_block = ""
    if existing:
        rows = []
        for l in existing:
            scope_part = f" [scope: {l.scope}]" if l.scope else ""
            rows.append(f'  id={l.id} side={l.side} axis={l.axis}{scope_part} text="{l.text}"')
        existing_block = (
            "\n## Existing brief lines (consider for merge)\n" + "\n".join(rows)
        )

    target_text = "the canonical phrasing of an existing line, IF you choose to merge"
    return f"""You extract structured preference signal from a user's free-form
feedback about a {'job posting' if is_job else 'resume rewrite'}.

The user's resume language is "{canonical_lang}". Output text must be in this
language for consistency, REGARDLESS of what language the user wrote in.

## Output format (strict JSON)

{{
  "lines": [
    {{
      "text": "≤ 60 chars, action-oriented, in {canonical_lang}",
      "side": "+" or "-",
      "axis": "{axes_hint}",
      "scope": null OR free-text limiter (e.g. "in Germany", "for IC roles", "for senior roles"),
      "confidence": 0.0-1.0,
      "merge_into": null OR id-of-an-existing-line (if this is a duplicate)
    }}
  ],
  "ambiguity_note": null OR "<reason the input was too vague to extract>"
}}

## Rules

- ≤ 3 lines per submission. Don't pad. If the user's input is too vague,
  return [] for lines and explain in ambiguity_note.
- text must be ≤ 60 chars and action-oriented:
  ✓ "避开 early-stage" / "Avoid early-stage" / "Evita fase iniziale"
  ✗ "User said they don't like early-stage companies"
- side: "+" for "lean toward", "-" for "avoid"
- axis MUST be from the allowed list — pick the closest fit
- scope: ONLY add if the preference is genuinely conditional. Most preferences
  are global. Region-specific salary floors → scope="in <region>". Role-level
  conditions → scope="for IC roles" / "for senior roles".
  ⚠ Do NOT add a scope just to be precise — if the preference is global, leave null.
- confidence < 0.5 → drop the line entirely (caller will warn user to be more specific)
- merge_into: cross-language dedup. If the new feedback says THE SAME THING
  as an existing line above (in any language), put that id here.
  "避开 early-stage" and "Evita fase iniziale" → same → merge.
  "避开 early-stage" and "倾向 stable mid-size" → different sides of same axis → DO NOT merge.
- If you merge, "text" should still be filled with {target_text}, in the canonical language.
{existing_block}

## Common axis disambiguation

- "工资低" / "comp太低" → axis="comp"
- "太创业氛围 / scrappy" → axis="company_stage"
- "通勤太远" / "must be remote" → axis="remote" (NOT location)
- "在德国 / in Germany" → scope, NOT axis
- "团队太小 / 公司太小" → axis="company_stage" (not "scope")

Output ONLY the JSON. Nothing else.
"""


@dataclass
class ExtractedLine:
    text: str
    side: str
    axis: str
    scope: Optional[str]
    confidence: float
    merge_into: Optional[str]   # existing line id, or None


@dataclass
class ExtractResult:
    lines: list[ExtractedLine]
    ambiguity_note: Optional[str]
    raw_response: str   # for debugging


def extract(
    *,
    user_text: str,
    context: str,                  # "jobs" or "resume"
    canonical_lang: str,
    existing_lines: list[BriefLine],
    provider: str,
    api_key: str,
    model: str,
) -> ExtractResult:
    """Single LLM call. Returns structured lines + ambiguity warning."""
    if context not in ("jobs", "resume"):
        raise ValueError(f"unknown context {context!r}")

    if not api_key:
        raise LLMConfigError("Preference extraction requires LLM_API_KEY.")

    valid_axes = JOB_AXES if context == "jobs" else RESUME_AXES

    system = _system_prompt(context, canonical_lang, existing_lines)
    payload = f"## User feedback\n{user_text.strip()}"

    try:
        resp = chat(
            provider=provider, api_key=api_key, model=model,
            system=system, user_content=payload,
            max_tokens=600, temperature=0.2,
        )
    except LLMConfigError:
        raise

    parsed = resp.parse_json(default={})
    raw_lines = parsed.get("lines") or []
    ambiguity = parsed.get("ambiguity_note") or None

    out_lines: list[ExtractedLine] = []
    for item in raw_lines:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text", "")).strip()[:60]
        side = str(item.get("side", "")).strip()
        axis = str(item.get("axis", "")).strip().lower()
        scope = item.get("scope")
        if scope is not None:
            scope = str(scope).strip()[:80] or None
        try:
            confidence = float(item.get("confidence", 0.5))
        except (TypeError, ValueError):
            confidence = 0.5
        merge_into = item.get("merge_into")
        if merge_into is not None:
            merge_into = str(merge_into).strip() or None

        # Validate
        if not text or side not in {"+", "-"}:
            continue
        if axis not in valid_axes:
            log.debug("Extracted unknown axis %r — dropping line", axis)
            continue
        if confidence < 0.5:
            continue

        out_lines.append(ExtractedLine(
            text=text, side=side, axis=axis, scope=scope,
            confidence=confidence, merge_into=merge_into,
        ))

    return ExtractResult(
        lines=out_lines[:3],
        ambiguity_note=str(ambiguity)[:200] if ambiguity else None,
        raw_response=resp.text[:500],
    )


def commit_extraction(
    brief: Brief,
    result: ExtractResult,
    *,
    item_id: str,
    item_label: str,
    user_original_text: str,
    bullet_index: Optional[int] = None,
) -> list[tuple[BriefLine, str]]:
    """Apply extracted lines to a brief. Returns [(line, action)] tuples.

    action ∈ {"added", "merged", "evicted_other"}.

    Caller handles persistence (save_jobs_brief / save_resume_brief).
    """
    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).isoformat()
    applied: list[tuple[BriefLine, str]] = []
    for ext_line in result.lines:
        origin = ConfirmationEntry(
            item_id=item_id, item_label=item_label,
            original_text=user_original_text[:300],
            at=now_iso,
            bullet_index=bullet_index,
        )
        line, action = brief.add_or_merge(
            text=ext_line.text,
            side=ext_line.side,
            axis=ext_line.axis,
            scope=ext_line.scope,
            confidence=ext_line.confidence,
            origin=origin,
            merge_target_id=ext_line.merge_into,
        )
        # If newly added, the origin we passed becomes the line's origin
        # (Brief.add_or_merge already wires it via BriefLine.new).
        applied.append((line, action))
    return applied
