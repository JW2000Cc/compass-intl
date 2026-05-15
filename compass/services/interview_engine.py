"""Interview engine — discover hidden facts via dialogue.

The user's朴素 description was: "工科生很少写课程小项目"
Truth: 90% of "missing facts" are unsurfaced, not missing. The interview
engine doesn't generate new facts from nothing — it asks the user questions
that surface real but un-stated facts.

Workflow per fact (or per JD):
    1. Generate 5-8 questions (LLM, scoped to fact kind)
    2. User answers (free-text, can skip)
    3. answers become FactEvidence + may yield new ResumeFact rows

This does NOT change the original fact — it enriches the fact pool with
discovered facts, which become new immutable rows tagged via
sensitive_category=null (free for L1-L3 amplification later).
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    FactEvidence,
    InterviewQA,
    InterviewSession,
    ReflectionEvent,
    ResumeFact,
)
from .llm import LLMConfigError, chat

log = logging.getLogger(__name__)


QUESTION_GEN_SYSTEM = """You are Compass's interview engine.

Your job: given ONE fact about the user (e.g. "Built electrical grid simulation
in coursework"), generate 5-8 specific questions to surface the SPECIFIC
hidden facts that the user has but didn't bother to write down.

The questions should target categories that boost resume signal WITHOUT inventing:
  - technical specifics (language, framework, scale, algorithm)
  - quantification (size, duration, baseline comparison)
  - role clarity (team size, your specific contribution)
  - validation (what scenarios were tested)
  - transferability (where else could this approach apply)
  - learning (what NEW skill did this build)

Rules:
  - 5-8 questions, no more.
  - Be SPECIFIC to the fact. Don't ask generic "what was your role" — ask
    "in this electrical grid project, did you write the solver yourself or
    use a library? which?"
  - Avoid questions whose answer would equal fabrication
    (don't ask "how much budget did you save?" if the fact is a coursework project).
  - Order by importance — most signal-yielding question first.

Output ONLY a JSON array of strings:
["question 1", "question 2", ...]
"""


def generate_questions(
    fact: ResumeFact,
    *,
    provider: str,
    api_key: str,
    model: str,
) -> list[str]:
    if not api_key:
        raise LLMConfigError("Interview question generation requires an LLM API key.")
    payload = (
        f"Fact kind: {fact.kind}\n"
        f"Fact text: {fact.text}\n"
        f"Structured: {fact.structured_json or {}}\n"
    )
    resp = chat(
        provider=provider,
        api_key=api_key,
        model=model,
        system=QUESTION_GEN_SYSTEM,
        user_content=payload,
        max_tokens=600,
    )
    parsed = resp.parse_json(default=[])
    if not isinstance(parsed, list):
        return []
    return [str(q).strip() for q in parsed if isinstance(q, str) and q.strip()][:8]


def start_session(
    session: Session,
    *,
    fact: ResumeFact,
    questions: list[str],
) -> InterviewSession:
    """Create an InterviewSession and pre-populate with questions (no answers yet)."""
    isess = InterviewSession(
        target_kind="fact",
        target_id=fact.id,
        purpose=f"Discover hidden facts about: {fact.text[:80]}",
    )
    session.add(isess)
    session.flush()
    for q in questions:
        session.add(InterviewQA(session_id=isess.id, question=q[:1000]))
    session.add(
        ReflectionEvent(
            kind="interview_started",
            payload_json={"target_kind": "fact", "target_id": fact.id, "questions": len(questions)},
            related_fact_id=fact.id,
        )
    )
    return isess


def record_answer(session: Session, qa_id: int, answer: str | None) -> None:
    qa = session.get(InterviewQA, qa_id)
    if qa is None:
        return
    if answer is None or not answer.strip():
        qa.answer = None
        qa.answer_skipped = True
    else:
        qa.answer = answer.strip()[:2000]
        qa.answer_skipped = False


SYNTHESIS_SYSTEM = """You are Compass's fact synthesizer.

Read the user's interview Q&A about ONE underlying fact. Produce a list of
NEW atomic facts that the user has actually stated in their answers.

Rules — non-negotiable:
  - Only include facts the user EXPLICITLY stated. No inference, no extrapolation.
  - If they said "around 200 nodes" — fact is "~200 nodes". Don't round to "200+".
  - If they didn't answer or skipped — produce NO fact for that question.
  - Each new fact is a single atomic claim, ≤ 25 words.

Output ONLY a JSON array of objects:
[
  {"kind": "experience_detail|skill|tool|metric|methodology",
   "text": "atomic fact text",
   "source_qa_indices": [0, 2]}
]
"""


def synthesize_facts(
    session: Session,
    *,
    isess: InterviewSession,
    parent_fact: ResumeFact,
    provider: str,
    api_key: str,
    model: str,
) -> list[ResumeFact]:
    """Read all Q&A in the session, distill into new ResumeFact rows.

    New facts inherit from the parent fact's identity; they have FactEvidence
    pointing to this interview session.
    """
    qas = (
        session.execute(
            select(InterviewQA)
            .where(InterviewQA.session_id == isess.id)
            .order_by(InterviewQA.id)
        )
        .scalars()
        .all()
    )
    answered = [(i, qa) for i, qa in enumerate(qas) if qa.answer and not qa.answer_skipped]
    if not answered:
        return []

    qa_block = "\n".join(
        f"[{i}] Q: {qa.question}\n    A: {qa.answer}" for i, qa in answered
    )
    payload = f"## Original fact\n{parent_fact.text}\n\n## Q&A\n{qa_block}"

    resp = chat(
        provider=provider,
        api_key=api_key,
        model=model,
        system=SYNTHESIS_SYSTEM,
        user_content=payload,
        max_tokens=1200,
    )
    parsed = resp.parse_json(default=[])
    if not isinstance(parsed, list):
        return []

    new_facts: list[ResumeFact] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text", "")).strip()
        kind = str(item.get("kind", "experience_detail"))[:40]
        if not text:
            continue
        f = ResumeFact(
            identity_id=parent_fact.identity_id,
            kind=kind,
            text=text[:500],
            user_verified=True,  # came from interview = user-verified
        )
        session.add(f)
        session.flush()
        # Evidence link
        indices = item.get("source_qa_indices") or []
        for idx in indices[:5]:
            try:
                idx_i = int(idx)
            except (ValueError, TypeError):
                continue
            if idx_i < 0 or idx_i >= len(answered):
                continue
            _, qa = answered[idx_i]
            session.add(
                FactEvidence(
                    fact_id=f.id,
                    source_kind="interview_qa",
                    source_ref=f"session={isess.id};qa={qa.id}",
                    excerpt=(qa.answer or "")[:300],
                )
            )
        new_facts.append(f)

    isess.completed = True
    session.add(
        ReflectionEvent(
            kind="interview_completed",
            payload_json={"session_id": isess.id, "new_facts": len(new_facts)},
            related_fact_id=parent_fact.id,
        )
    )
    return new_facts


def get_or_create_session(session: Session, fact_id: str) -> Optional[InterviewSession]:
    """Return latest unfinished session for a fact, if any."""
    return (
        session.execute(
            select(InterviewSession)
            .where(InterviewSession.target_kind == "fact", InterviewSession.target_id == fact_id, InterviewSession.completed.is_(False))
            .order_by(InterviewSession.created_at.desc())
        )
        .scalars()
        .first()
    )
