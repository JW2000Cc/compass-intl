"""Gmail integration — auto-detect application status from email.

This service closes Compass's biggest missing feedback loop:
  - User applies via LinkedIn / Greenhouse / Workday / direct email.
  - Within hours/days, employer responds: confirmation / interview invite / rejection / offer.
  - **Most users won't manually update job status in Compass.**
  - Result: funnel data is incomplete; calibration learner sees no real outcome.

This service:
  1. Authenticates with user's Gmail (one-time OAuth)
  2. Periodically (or on-demand) fetches recent emails
  3. LLM classifies each email: application_received / interview_invite / rejection / offer
  4. Maps email → existing Compass Job (by company name + title fuzzy match)
  5. Updates Job.status + emits ReflectionEvent (so calibration learner gets real signal)

Workflow:
  /gmail/setup     # one-time OAuth flow
  /gmail/sync     # manual sync (user clicks); future: cron-like background

Privacy notes:
  - We only read Gmail messages with broad job-keyword filter to limit scope
  - We store **classification result** + email subject snippet only — never full body
  - Full email body is fetched, classified, then discarded
  - User can revoke access anytime via google.com/account
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Job, ReflectionEvent
from .llm import LLMConfigError, LLMError, chat

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────
# OAuth + Gmail API access
# ─────────────────────────────────────────────────────────


GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def gmail_credentials_path(data_dir: Path) -> Path:
    return data_dir / "credentials" / "gmail_token.json"


def gmail_client_secret_path(data_dir: Path) -> Path:
    return data_dir / "credentials" / "gmail_client_secret.json"


def get_gmail_service(data_dir: Path):
    """Return an authenticated Gmail API service.

    Requires google-api-python-client + google-auth-oauthlib.
    User must place a Google Cloud OAuth client secret at
    `<data_dir>/credentials/gmail_client_secret.json` and run /gmail/setup once.
    """
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise RuntimeError(
            "Gmail integration requires google-api-python-client + google-auth-oauthlib. "
            "Install: pip install google-api-python-client google-auth-oauthlib"
        ) from exc

    token_path = gmail_credentials_path(data_dir)
    secret_path = gmail_client_secret_path(data_dir)

    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), GMAIL_SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_path.write_text(creds.to_json(), encoding="utf-8")
    if not creds or not creds.valid:
        if not secret_path.exists():
            raise RuntimeError(
                f"Place Google Cloud OAuth client secret at {secret_path}. "
                "See: https://developers.google.com/gmail/api/quickstart/python"
            )
        flow = InstalledAppFlow.from_client_secrets_file(str(secret_path), GMAIL_SCOPES)
        creds = flow.run_local_server(port=0)
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(creds.to_json(), encoding="utf-8")

    return build("gmail", "v1", credentials=creds)


# ─────────────────────────────────────────────────────────
# Fetch + filter relevant emails
# ─────────────────────────────────────────────────────────


def list_recent_messages(service, *, days: int = 7, max_results: int = 50) -> list[dict]:
    """Fetch headers of recent messages matching job-keyword filter.

    We deliberately pull broad recall — let the LLM classify; cheap to discard
    non-job ones.
    """
    after_ts = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())
    query = (
        f"after:{after_ts} ("
        "subject:(application OR interview OR \"thank you for applying\" OR position OR offer OR hiring) "
        "OR from:(jobs OR talent OR recruit OR hr OR careers)"
        ")"
    )
    results = service.users().messages().list(
        userId="me", q=query, maxResults=max_results,
    ).execute()
    return results.get("messages", []) or []


def fetch_message_full(service, message_id: str) -> dict:
    """Get headers + body snippet for one message."""
    msg = service.users().messages().get(userId="me", id=message_id, format="full").execute()
    headers = {h["name"].lower(): h["value"] for h in msg["payload"].get("headers", [])}
    snippet = msg.get("snippet", "")
    body = _extract_plain_text(msg["payload"])
    return {
        "id": message_id,
        "from": headers.get("from", ""),
        "subject": headers.get("subject", ""),
        "date": headers.get("date", ""),
        "snippet": snippet,
        "body": body[:4000],  # cap to keep prompts cheap
    }


def _extract_plain_text(payload: dict) -> str:
    """Recursively dig out text/plain part from a Gmail API payload."""
    import base64

    if payload.get("mimeType") == "text/plain":
        data = payload.get("body", {}).get("data", "")
        if data:
            try:
                return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode(
                    "utf-8", errors="replace"
                )
            except Exception:
                return ""
    for part in payload.get("parts", []) or []:
        text = _extract_plain_text(part)
        if text:
            return text
    return ""


# ─────────────────────────────────────────────────────────
# LLM classifier
# ─────────────────────────────────────────────────────────


CLASSIFY_SYSTEM = """You read a single email and classify it as ONE of:

- "application_received"   — confirmation that an application was submitted/received
- "interview_invite"       — invitation to phone screen / on-site / coding interview
- "rejection"              — application declined / position filled / no further action
- "offer"                  — formal job offer extended
- "scheduling"             — interview scheduling / calendar coordination (treated separately)
- "follow_up"              — recruiter follow-up / status check
- "not_job_related"        — not actually about a job application

Also extract `company` (best guess) and `position` (best guess) if visible.

Output ONLY JSON:
{
  "kind": "<one of the above>",
  "company": "...",
  "position": "...",
  "confidence": 0.0-1.0,
  "evidence": "<one short sentence quoting decisive language>"
}"""


@dataclass
class EmailClassification:
    kind: str
    company: str
    position: str
    confidence: float
    evidence: str
    raw_message_id: str


def classify_email(email: dict, *, provider: str, api_key: str, model: str) -> Optional[EmailClassification]:
    if not api_key:
        return None
    user_msg = (
        f"From: {email.get('from','')}\n"
        f"Subject: {email.get('subject','')}\n"
        f"Date: {email.get('date','')}\n\n"
        f"Body:\n{email.get('body','')[:3500]}"
    )
    try:
        resp = chat(
            provider=provider, api_key=api_key, model=model,
            system=CLASSIFY_SYSTEM, user_content=user_msg, max_tokens=300,
        )
    except LLMError as exc:
        # Y3 (5-08): record at log.warning level — gmail sync is a background
        # task, no user-facing flash, but log noise needs a stable prefix so
        # `grep "LLM silent failure"` finds it during incident debugging.
        from .llm_diagnostics import record
        record(None, "gmail_sync.classify_email", exc,
               context=f"msg_id={email.get('id','?')}")
        return None
    parsed = resp.parse_json(default={})
    if not isinstance(parsed, dict) or not parsed.get("kind"):
        return None
    return EmailClassification(
        kind=str(parsed.get("kind", "not_job_related")),
        company=str(parsed.get("company", ""))[:200],
        position=str(parsed.get("position", ""))[:300],
        confidence=float(parsed.get("confidence", 0.5) or 0.5),
        evidence=str(parsed.get("evidence", ""))[:400],
        raw_message_id=email["id"],
    )


# ─────────────────────────────────────────────────────────
# Mapping: email → Compass Job (fuzzy match)
# ─────────────────────────────────────────────────────────


def find_matching_job(session: Session, classification: EmailClassification) -> Optional[Job]:
    """Best-effort fuzzy match — by company + title substring overlap."""
    if not classification.company:
        return None
    jobs = session.execute(
        select(Job).where(Job.deleted.is_(False))
    ).scalars().all()

    company_norm = re.sub(r"[^a-z0-9]+", "", classification.company.lower())
    pos_norm = re.sub(r"[^a-z0-9]+", "", (classification.position or "").lower())

    best: Optional[Job] = None
    best_score = 0
    for j in jobs:
        j_company_norm = re.sub(r"[^a-z0-9]+", "", (j.company or "").lower())
        j_title_norm = re.sub(r"[^a-z0-9]+", "", (j.title or "").lower())
        score = 0
        if company_norm and j_company_norm:
            if company_norm == j_company_norm:
                score += 10
            elif company_norm in j_company_norm or j_company_norm in company_norm:
                score += 6
        if pos_norm and j_title_norm:
            if pos_norm == j_title_norm:
                score += 5
            elif pos_norm in j_title_norm or j_title_norm in pos_norm:
                score += 3
        if score > best_score:
            best, best_score = j, score
    return best if best_score >= 6 else None


# ─────────────────────────────────────────────────────────
# Apply classification to Compass DB
# ─────────────────────────────────────────────────────────


_KIND_TO_STATUS = {
    "application_received": "applied",
    "interview_invite": "interview",
    "rejection": "rejected",
    "offer": "offer",
}


def apply_classification(
    session: Session,
    classification: EmailClassification,
    job: Optional[Job],
) -> dict:
    """Update Job.status if mapped + emit ReflectionEvent. Returns summary."""
    summary = {
        "kind": classification.kind,
        "company": classification.company,
        "position": classification.position,
        "confidence": classification.confidence,
        "matched_job_id": job.id if job else None,
        "status_changed": False,
    }
    if job is None or classification.kind not in _KIND_TO_STATUS:
        # Still emit event for transparency — calibration learner can see it
        session.add(ReflectionEvent(
            kind="gmail_email_classified_unmatched",
            payload_json=summary,
        ))
        return summary

    new_status = _KIND_TO_STATUS[classification.kind]
    old_status = job.status

    # Don't overwrite a "stronger" status with a weaker one
    rank = {"new": 0, "reviewed": 1, "applied": 2, "interview": 3, "offer": 4, "rejected": 4, "passed": 1}
    if rank.get(new_status, 0) <= rank.get(old_status, 0) and new_status != old_status:
        # Already in equal or stronger state — log but don't overwrite
        session.add(ReflectionEvent(
            kind="gmail_email_status_skipped",
            payload_json={**summary, "old_status": old_status, "would_be": new_status},
            related_job_id=job.id,
        ))
        return summary

    job.status = new_status
    job.status_changed_at = datetime.now(timezone.utc)
    summary["status_changed"] = True
    summary["from"] = old_status
    summary["to"] = new_status

    session.add(ReflectionEvent(
        kind="gmail_status_auto_update",
        payload_json=summary,
        related_job_id=job.id,
    ))
    return summary


# ─────────────────────────────────────────────────────────
# Top-level sync orchestrator
# ─────────────────────────────────────────────────────────


def run_sync(
    session: Session,
    *,
    data_dir: Path,
    provider: str,
    api_key: str,
    model: str,
    days: int = 7,
    max_results: int = 50,
) -> dict:
    """Full Gmail sync: fetch → classify → apply. Returns summary stats."""
    service = get_gmail_service(data_dir)
    msg_refs = list_recent_messages(service, days=days, max_results=max_results)

    stats = {
        "fetched": len(msg_refs),
        "classified": 0,
        "matched": 0,
        "status_updated": 0,
        "by_kind": {},
        "details": [],
    }

    for ref in msg_refs:
        try:
            email = fetch_message_full(service, ref["id"])
        except Exception as exc:  # noqa: BLE001
            log.warning("fetch failed for %s: %s", ref["id"], exc)
            continue

        c = classify_email(email, provider=provider, api_key=api_key, model=model)
        if c is None or c.kind == "not_job_related":
            continue
        stats["classified"] += 1
        stats["by_kind"][c.kind] = stats["by_kind"].get(c.kind, 0) + 1

        job = find_matching_job(session, c)
        if job is not None:
            stats["matched"] += 1

        result = apply_classification(session, c, job)
        if result.get("status_changed"):
            stats["status_updated"] += 1

        stats["details"].append({
            "subject": email.get("subject", "")[:120],
            "kind": c.kind,
            "company": c.company,
            "matched_job_id": job.id if job else None,
            "status_changed": result.get("status_changed", False),
        })

    session.add(ReflectionEvent(
        kind="gmail_sync_completed",
        payload_json={k: v for k, v in stats.items() if k != "details"},
    ))
    return stats
