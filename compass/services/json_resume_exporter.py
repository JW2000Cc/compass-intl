"""Export Compass facts + approved variants to JSON Resume schema.

JSON Resume (https://jsonresume.org/) is the de-facto open standard for resume data.
Exporting to it gives the user access to **dozens of open-source themes**
(Reactive Resume / RenderCV / jsonresume.org/projects) without Compass having
to ship its own renderer.

Usage:
    payload = build_json_resume(session, job_id=None)
    # or for a specific job (variants for that job override fact text)
    payload = build_json_resume(session, job_id="abc123")
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import FactVariant, IdentityVersion, ResumeFact

log = logging.getLogger(__name__)


def build_json_resume(
    session: Session,
    *,
    job_id: Optional[str] = None,
    target_language: Optional[str] = None,
) -> dict:
    """Assemble a JSON Resume schema-compliant dict from Compass data.

    Args:
        session: SQLAlchemy session
        job_id: when given, prefer approved/edited variants for that job over
            base fact text. Otherwise return the user's "master resume".
        target_language: ISO language code (e.g. "en"/"zh"/"it"). When set, each
            variant's `translated_json[lang]` is used as the bullet text instead
            of `variant.text`. Falls back to `variant.text` when a translation
            is missing — caller is expected to invoke variant_translator
            beforehand to fill missing translations if a guarantee is needed.
            None = use identity's source language as-is (legacy behavior).
    """
    identity = session.execute(
        select(IdentityVersion).where(IdentityVersion.is_current.is_(True))
    ).scalars().first()
    if identity is None:
        return {"basics": {}, "work": [], "education": [], "skills": []}

    pj = identity.parsed_json or {}

    # Build variant override map: fact_id -> variant_text (only approved/edited).
    # When target_language set, prefer translated_json[lang] over .text.
    overrides: dict[str, str] = {}
    if job_id:
        variants = session.execute(
            select(FactVariant).where(
                FactVariant.job_id == job_id,
                FactVariant.user_decision.in_(["approved", "edited_by_user"]),
            )
        ).scalars().all()
        for v in variants:
            text = v.text
            if target_language:
                # FactVariant.translated_json[lang] is a dict produced by
                # variant_translator.TranslatedVariant.to_jsonable() with
                # shape {text, language, preserved, missing, llm_used, warnings}.
                # Earlier code's `isinstance(tx, str)` check ALWAYS failed
                # (tx is a dict), so translation was silently dropped — even
                # when fully populated. Accept either dict (current) or
                # plain string (legacy/forward-compat).
                tj = getattr(v, "translated_json", None) or {}
                tx = tj.get(target_language)
                if isinstance(tx, dict):
                    cand = (tx.get("text") or "").strip()
                    if cand:
                        text = cand
                elif isinstance(tx, str) and tx.strip():
                    text = tx
            overrides[v.fact_id] = text

    facts = session.execute(
        select(ResumeFact).where(
            ResumeFact.identity_id == identity.id, ResumeFact.active.is_(True)
        )
    ).scalars().all()

    # ── basics ─────────────────────────────────────────────
    basics = {
        "name": pj.get("name", ""),
        "email": pj.get("email", ""),
        "phone": pj.get("phone", ""),
        "location": {"address": pj.get("location", "")},
        "summary": pj.get("summary", ""),
    }

    # ── work (from experience facts; bullets per company) ─
    work_by_company: dict[str, dict] = {}
    company_order: list[str] = []
    for f in facts:
        if f.kind == "experience" and f.structured_json:
            sj = f.structured_json
            company = sj.get("company") or "—"
            if company not in work_by_company:
                work_by_company[company] = {
                    "name": company,
                    "position": sj.get("title", ""),
                    "startDate": sj.get("start", ""),
                    "endDate": sj.get("end", ""),
                    "location": sj.get("location", ""),
                    "highlights": [],
                }
                company_order.append(company)
        elif f.kind == "experience_bullet":
            text = overrides.get(f.id, f.text)
            sj = f.structured_json or {}
            belongs = sj.get("belongs_to", "") or ""
            # Try to attach to the right company by matching header
            target = None
            for company, work in work_by_company.items():
                if work.get("name") in belongs:
                    target = work
                    break
            if target is not None:
                target["highlights"].append(text)
            else:
                # Orphan bullet — attach to a generic "Other Experience"
                if "Other Experience" not in work_by_company:
                    work_by_company["Other Experience"] = {
                        "name": "Other Experience",
                        "position": "",
                        "highlights": [],
                    }
                    company_order.append("Other Experience")
                work_by_company["Other Experience"]["highlights"].append(text)
        elif f.kind == "experience_detail":
            # facts discovered via interview — no direct company attachment;
            # surface as additional highlights on the most recent company
            if company_order:
                latest = work_by_company[company_order[0]]
                latest["highlights"].append(overrides.get(f.id, f.text))

    work = [work_by_company[c] for c in company_order]

    # ── education ────────────────────────────────────────
    education: list[dict] = []
    for f in facts:
        if f.kind != "education":
            continue
        sj = f.structured_json or {}
        education.append({
            "institution": sj.get("school", ""),
            "area": sj.get("field", ""),
            "studyType": sj.get("degree", ""),
            "startDate": sj.get("start", ""),
            "endDate": sj.get("end", ""),
            "gpa": sj.get("gpa", ""),
        })

    # ── skills ───────────────────────────────────────────
    by_kind: dict[str, list[str]] = {}
    for f in facts:
        if f.kind.startswith("skill_"):
            cat = f.kind.replace("skill_", "")
            by_kind.setdefault(cat, []).append(overrides.get(f.id, f.text))
    skills = [
        {"name": cat.title(), "keywords": items}
        for cat, items in by_kind.items()
    ]

    # ── languages ────────────────────────────────────────
    languages = [
        {"language": (overrides.get(f.id, f.text)).split("(")[0].strip()}
        for f in facts if f.kind == "language"
    ]

    # ── projects ─────────────────────────────────────────
    projects: list[dict] = []
    for f in facts:
        if f.kind != "project":
            continue
        sj = f.structured_json or {}
        projects.append({
            "name": sj.get("name", "") or (overrides.get(f.id, f.text)[:80]),
            "description": overrides.get(f.id, f.text),
            "keywords": sj.get("tech", []) if isinstance(sj.get("tech"), list) else [],
            "url": sj.get("link", ""),
        })

    # ── certificates ─────────────────────────────────────
    certificates: list[dict] = []
    for f in facts:
        if f.kind != "certification":
            continue
        sj = f.structured_json or {}
        certificates.append({
            "name": sj.get("name", "") or overrides.get(f.id, f.text),
            "issuer": sj.get("issuer", ""),
            "date": str(sj.get("year", "")) if sj.get("year") else "",
        })

    return {
        "$schema": "https://jsonresume.org/schema",
        "basics": basics,
        "work": work,
        "education": education,
        "skills": skills,
        "languages": languages,
        "projects": projects,
        "certificates": certificates,
        "meta": {
            "version": "v1.0.0",
            "lastModified": identity.created_at.isoformat() if identity.created_at else "",
            "exportedFrom": "Compass",
            "tailoredFor": job_id or None,
        },
    }
