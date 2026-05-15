"""Generate personalized outreach messages — both warm (someone you know) and cold.

Ported from v1's outreach_gen.py (Job Tracker), adapted:
  - Uses Compass's existing LLM client (services/llm.py — supports
    Claude / OpenAI / others) instead of hard-binding to anthropic SDK.
  - Pulls user identity / facts from Compass's IdentityVersion + ResumeFact
    instead of reading a flat resume.txt.
  - Distinguishes warm vs cold path: warm gets richer personalization
    context (the actual relationship), cold uses public snippet only.
  - Honors user's reflective preference brief (resume side) for tone.

Output: a draft message ≤ 280 chars (LinkedIn DM cap) for warm/cold InMail,
or ≤ 150 words for full email. Language matches recipient's region.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from .llm import LLMConfigError, LLMError, chat
from .preference_brief import load_resume_brief

log = logging.getLogger(__name__)


@dataclass
class OutreachInput:
    """All the context needed to draft a single outreach."""
    contact_name: str
    contact_title: str = ""
    contact_company: str = ""
    contact_url: str = ""
    contact_snippet: str = ""

    job_title: str = ""
    job_company: str = ""
    job_location: str = ""
    job_description: str = ""
    job_link: str = ""

    user_resume_summary: str = ""

    purpose: str = "referral_request"
    # "referral_request" | "informational" | "direct_question"

    # Warm-path-only — present if path=="warm"
    relationship_context: Optional[str] = None
    # e.g. "We connected on LinkedIn 2024-03"
    # or  "We were both EE 2018 at XYZ University"

    medium: str = "linkedin_dm"
    # "linkedin_dm" (≤280 chars) | "email" (≤150 words) | "linkedin_request" (≤300 chars)


@dataclass
class OutreachResult:
    success: bool
    message: str = ""
    error: Optional[str] = None
    model: Optional[str] = None
    detected_language: Optional[str] = None


def _system_prompt(inp: OutreachInput) -> str:
    is_warm = inp.relationship_context is not None and inp.relationship_context.strip()

    base = """You are a job-search outreach strategist. Draft a single personalized message
that maximizes the chance the recipient will respond and consider helping the user."""

    medium_rules = {
        "linkedin_dm": "≤ 280 characters (LinkedIn message cap, NOT the longer InMail cap)",
        "linkedin_request": "≤ 300 characters (LinkedIn connection request body)",
        "email": "≤ 150 words. Subject line on its own line at top, then a blank line, then body",
    }.get(inp.medium, "≤ 150 words")

    purpose_rules = {
        "referral_request": (
            "Purpose: explicit referral request. State the role + company by name. "
            "Acknowledge it's an ask. Make it easy to say yes (link to JD) AND easy to say no."
        ),
        "informational": (
            "Purpose: informational chat (NOT a referral ask). Offer time-bounded ask "
            '("15 minutes some week"). Show you researched their work. No direct ATS push.'
        ),
        "direct_question": (
            "Purpose: a single specific question about the role/team. NOT a referral ask. "
            "The question must be answerable from their public profile."
        ),
    }.get(inp.purpose, "")

    warm_block = ""
    if is_warm:
        warm_block = f"""
WARM-PATH: This is someone the user already knows. The relationship is:
  {inp.relationship_context}

Use the relationship as the OPENING (not as the close). Don't over-explain
the past — one sentence reference, then move to the ask. Skip "saw your post"
type cold-opener detail; the relationship IS the credential.
"""
    else:
        warm_block = """
COLD-PATH: The user does not know this recipient personally. Open with ONE
researched detail from their public profile/snippet (something specific they
worked on, said, or built — NOT generic "I'm impressed by your work").

If the snippet has nothing concrete: prefer a direct question about the role
over hollow flattery.
"""

    rules = f"""
Hard rules:
  - Length: {medium_rules}
  - {purpose_rules}
  - One specific researched detail in opening — must be real, NOT invented.
  - No flattery ("I'm a huge fan" / "amazing company" reads like spam).
  - No deadline pressure.
  - One clear ask, not multi-ask.
  - LANGUAGE: detect the recipient's region from contact_company location and use that:
      Germany / Austria / Switzerland (German part) → German
      France → French
      Italy → Italian
      Spain → Spanish
      Netherlands / Belgium (Flemish) → English (most NL/BE professionals use English)
      Other / unknown → English
    Do NOT mix languages within one message.

Output ONLY the message text. No preamble, no commentary, no metadata."""

    return base + "\n\n" + warm_block + rules


def _user_payload(inp: OutreachInput) -> str:
    parts = []
    parts.append("# Recipient")
    parts.append(f"  Name: {inp.contact_name}")
    if inp.contact_title:
        parts.append(f"  Title: {inp.contact_title}")
    if inp.contact_company:
        parts.append(f"  Company: {inp.contact_company}")
    if inp.contact_snippet:
        parts.append(f"  Public snippet: {inp.contact_snippet[:300]}")
    if inp.contact_url:
        parts.append(f"  LinkedIn: {inp.contact_url}")

    parts.append("")
    parts.append("# Target job")
    parts.append(f"  Title: {inp.job_title}")
    parts.append(f"  Company: {inp.job_company}")
    if inp.job_location:
        parts.append(f"  Location: {inp.job_location}")
    if inp.job_description:
        parts.append(f"  JD excerpt: {inp.job_description[:600]}")
    if inp.job_link:
        parts.append(f"  Link: {inp.job_link}")

    parts.append("")
    parts.append("# User (sender) — what they bring")
    parts.append(inp.user_resume_summary[:1200] or "(no resume summary provided)")

    return "\n".join(parts)


def draft_outreach(
    inp: OutreachInput,
    *,
    provider: str,
    api_key: str,
    model: str,
) -> OutreachResult:
    """Single LLM call. Returns either the draft message or an error."""
    if not api_key:
        return OutreachResult(success=False, error="No LLM_API_KEY configured.")

    system = _system_prompt(inp)

    # Mix in resume preference brief if any (so writing tone reflects past feedback)
    try:
        resume_brief_block = load_resume_brief().format_for_prompt()
        if resume_brief_block:
            system += "\n\n" + resume_brief_block
    except Exception as e:
        log.debug(f"resume brief load failed: {e}")

    payload = _user_payload(inp)

    try:
        resp = chat(
            provider=provider, api_key=api_key, model=model,
            system=system, user_content=payload,
            max_tokens=600, temperature=0.5,
        )
    except LLMError as e:
        # Catches LLMConfigError (missing key) AND post-retry LLMError
        # (401/429/timeout/network). Without parent-class catch, transient
        # auth/network errors bubble up as 500 instead of friendly Result.
        return OutreachResult(success=False, error=f"LLM error: {e}")

    text = (resp.text or "").strip().strip('"').strip()
    if not text:
        return OutreachResult(success=False, error="LLM returned empty message.")

    return OutreachResult(
        success=True,
        message=text,
        model=resp.model,
    )


def build_resume_summary_from_facts(facts: list, max_chars: int = 1200) -> str:
    """Compact a list of ResumeFact rows into a short multi-line resume summary.

    Used to feed the LLM during outreach drafting without dumping the full resume.
    """
    if not facts:
        return ""
    by_kind: dict[str, list[str]] = {}
    for f in facts:
        kind = getattr(f, "kind", "other")
        text = (getattr(f, "text", "") or "").strip()
        if not text:
            continue
        by_kind.setdefault(kind, []).append(text)

    parts: list[str] = []
    order = ["experience", "experience_bullet", "project", "skill", "education",
             "language", "certification"]
    for kind in order:
        if kind in by_kind:
            label = kind.replace("_", " ").title()
            for t in by_kind[kind][:5]:
                parts.append(f"- [{label}] {t}")
    # any kinds not in our priority order, dump after
    for kind, items in by_kind.items():
        if kind in order:
            continue
        label = kind.replace("_", " ").title()
        for t in items[:3]:
            parts.append(f"- [{label}] {t}")

    out = "\n".join(parts)
    return out[:max_chars]
