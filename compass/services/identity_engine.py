"""Identity engine — turning a resume upload into immutable facts.

Workflow:
  1. parse_resume(path)        markitdown + format-specific fallback
  2. extract_structured(text)  LLM → parsed_json with name/edu/exp/skills/projects
  3. install_identity(...)     persist IdentityVersion + ResumeFact rows

Facts inherit a `sensitive_category` flag where applicable
(education_credential / language_level / employment_dates) — these
trigger L5 hard-bans in the claim grounder.
"""
from __future__ import annotations


import logging

from pathlib import Path
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import FactEvidence, IdentityVersion, ReflectionEvent, ResumeFact
from .llm import LLMConfigError, LLMError, chat

log = logging.getLogger(__name__)


def detect_format(filename: str) -> str:
    name = (filename or "").lower()
    if name.endswith(".pdf"):
        return "pdf"
    if name.endswith(".docx") or name.endswith(".doc"):
        return "docx"
    if name.endswith(".txt") or name.endswith(".md"):
        return "txt"
    return ""


def parse_resume(file_path: Path) -> str:
    """Best-effort full-text extraction. Identical fallback chain to v1."""
    fmt = detect_format(file_path.name)

    try:
        from markitdown import MarkItDown

        md = MarkItDown()
        result = md.convert(str(file_path))
        text = (result.text_content or "").strip()
        if text:
            return text
    except Exception as exc:  # noqa: BLE001
        log.debug("markitdown failed for %s: %s — falling back", file_path.name, exc)

    if fmt == "pdf":
        try:
            from pypdf import PdfReader

            reader = PdfReader(str(file_path))
            return "\n".join((p.extract_text() or "") for p in reader.pages).strip()
        except Exception as exc:  # noqa: BLE001
            log.warning("pypdf failed: %s", exc)
            return ""

    if fmt == "docx":
        try:
            from docx import Document

            doc = Document(str(file_path))
            return "\n".join(p.text for p in doc.paragraphs).strip()
        except Exception as exc:  # noqa: BLE001
            log.warning("python-docx failed: %s", exc)
            return ""

    if fmt == "txt":
        try:
            return file_path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return ""

    return ""


PROFILE_SYSTEM = """You extract a structured profile from a resume.

Return ONLY a JSON object (use empty string / list when unknown):
{
  "name": "",
  "email": "",
  "phone": "",
  "location": "",
  "summary": "",
  "education": [{"school":"","degree":"","field":"","start":"","end":"","gpa":"","notes":""}],
  "experience": [{"company":"","title":"","location":"","start":"","end":"","bullets":[]}],
  "skills": {"technical":[],"languages":[],"tools":[],"soft":[]},
  "projects": [{"name":"","description":"","tech":[],"link":""}],
  "certifications": [{"name":"","issuer":"","year":""}],
  "publications": []
}
- Preserve original wording in bullets — do NOT invent.
- Use ISO-like dates (YYYY-MM) when possible; otherwise leave the original text.
- "summary" is the user's self-summary if present.
- If the resume contains a language-proficiency claim (e.g. "German B1", "English C2"),
  put it under skills.languages with the level as written.
"""


def extract_structured(
    text: str, *, provider: str, api_key: str, model: str,
    errors: list[str] | None = None,
) -> dict[str, Any]:
    if not api_key:
        from .llm_diagnostics import record
        record(errors, "identity.extract_structured",
               RuntimeError("API key 缺失"),
               context="跳过结构化提取，简历仅有原文")
        log.info("no LLM key — skipping profile extraction")
        return {}
    if not text:
        return {}
    user = f"## Resume text\n\n{text[:9000]}"
    try:
        resp = chat(
            provider=provider,
            api_key=api_key,
            model=model,
            system=PROFILE_SYSTEM,
            user_content=user,
            max_tokens=2500,
        )
    except LLMError as exc:
        from .llm_diagnostics import record
        record(errors, "identity.extract_structured", exc,
               context="LLM 失败；简历仅有原文")
        log.warning("structured extraction config error: %s", exc)
        return {}
    parsed = resp.parse_json(default={})
    return parsed if isinstance(parsed, dict) else {}


def detect_lang(text: str) -> str:
    if not text:
        return "en"
    sample = text[:2000].lower()
    chinese = sum(1 for c in sample if "一" <= c <= "鿿")
    if chinese > 30:
        return "zh"
    italian = sum(sample.count(w) for w in (" il ", " la ", " di ", " che ", " per "))
    french = sum(sample.count(w) for w in (" le ", " la ", " de ", " et ", " les "))
    german = sum(sample.count(w) for w in (" der ", " die ", " das ", " und ", " mit "))
    scores = {"it": italian, "fr": french, "de": german}
    best, n = max(scores.items(), key=lambda kv: kv[1])
    return best if n >= 4 else "en"


# ─────────────────────────────────────────────────────────
# Persisting identity + facts — facts are immutable
# ─────────────────────────────────────────────────────────


def install_identity(
    session: Session,
    *,
    raw_text: str,
    parsed_json: dict[str, Any],
    label: str = "",
    source_filename: str | None = None,
) -> IdentityVersion:
    """Create a new IdentityVersion + extract facts from parsed_json.

    Marks any prior is_current=True identity as is_current=False.
    """
    for old in (
        session.execute(select(IdentityVersion).where(IdentityVersion.is_current.is_(True)))
        .scalars()
        .all()
    ):
        old.is_current = False

    iv = IdentityVersion(
        label=label or "uploaded",
        source_filename=source_filename,
        raw_text=raw_text,
        parsed_json=parsed_json or None,
        language=detect_lang(raw_text),
        is_current=True,
    )
    session.add(iv)
    session.flush()

    facts_made = 0
    for fact in _facts_from_parsed_json(parsed_json):
        fact.identity_id = iv.id
        session.add(fact)
        session.flush()
        # Provenance link
        session.add(
            FactEvidence(
                fact_id=fact.id,
                source_kind="resume_upload",
                source_ref=source_filename or "uploaded",
                excerpt=fact.text[:300],
            )
        )
        facts_made += 1

    session.add(
        ReflectionEvent(
            kind="identity_uploaded",
            payload_json={
                "identity_id": iv.id,
                "language": iv.language,
                "facts_extracted": facts_made,
                "filename": source_filename,
            },
        )
    )
    return iv


def _facts_from_parsed_json(pj: dict) -> list[ResumeFact]:
    """Decompose a parsed_json profile into atomic ResumeFact rows.

    Sensitive categories are tagged for L5 hard-ban downstream:
      - education_credential
      - language_level
      - employment_dates
      - certification
    """
    out: list[ResumeFact] = []
    if not isinstance(pj, dict):
        return out

    for ed in (pj.get("education") or []):
        if not isinstance(ed, dict):
            continue
        text = " · ".join(
            x for x in [ed.get("degree"), ed.get("field"), ed.get("school"), f"{ed.get('start','')}-{ed.get('end','')}".strip("-")] if x
        )
        if text:
            out.append(
                ResumeFact(
                    kind="education",
                    text=text[:500],
                    structured_json=ed,
                    sensitive_category="education_credential",
                )
            )

    for exp in (pj.get("experience") or []):
        if not isinstance(exp, dict):
            continue
        header = " · ".join(
            x for x in [exp.get("title"), exp.get("company"), f"{exp.get('start','')}–{exp.get('end','')}".strip("–")] if x
        )
        if header:
            out.append(
                ResumeFact(
                    kind="experience",
                    text=header[:500],
                    structured_json=exp,
                    sensitive_category="employment_dates",
                )
            )
        for bullet in (exp.get("bullets") or []):
            if not isinstance(bullet, str) or not bullet.strip():
                continue
            out.append(
                ResumeFact(
                    kind="experience_bullet",
                    text=bullet.strip()[:500],
                    structured_json={"belongs_to": header},
                )
            )

    skills_obj = pj.get("skills") or {}
    if isinstance(skills_obj, dict):
        for cat in ("technical", "tools", "soft"):
            for s in (skills_obj.get(cat) or []):
                if isinstance(s, str) and s.strip():
                    out.append(ResumeFact(kind=f"skill_{cat}", text=s.strip()[:200]))
        for lang in (skills_obj.get("languages") or []):
            if isinstance(lang, str) and lang.strip():
                out.append(
                    ResumeFact(
                        kind="language",
                        text=lang.strip()[:120],
                        sensitive_category="language_level",
                    )
                )

    for proj in (pj.get("projects") or []):
        if not isinstance(proj, dict):
            continue
        text = " · ".join(x for x in [proj.get("name"), proj.get("description")] if x)
        if text:
            out.append(ResumeFact(kind="project", text=text[:500], structured_json=proj))

    for cert in (pj.get("certifications") or []):
        if isinstance(cert, dict):
            text = " · ".join(x for x in [cert.get("name"), cert.get("issuer"), str(cert.get("year") or "")] if x)
            if text:
                out.append(
                    ResumeFact(
                        kind="certification",
                        text=text[:300],
                        structured_json=cert,
                        sensitive_category="certification",
                    )
                )
        elif isinstance(cert, str) and cert.strip():
            out.append(ResumeFact(kind="certification", text=cert.strip()[:300], sensitive_category="certification"))

    return out


def get_current_identity(session: Session) -> Optional[IdentityVersion]:
    """Latest identity with is_current=True. Order-by-created defends against
    the rare case of concurrent saves leaving multiple rows flagged True
    (save_identity normally clears priors, but a race or failed migration
    could create duplicates). Without ORDER BY, callers got an arbitrary
    "current" identity which silently pointed exports at stale facts."""
    return (
        session.execute(
            select(IdentityVersion)
            .where(IdentityVersion.is_current.is_(True))
            .order_by(IdentityVersion.created_at.desc())
        )
        .scalars()
        .first()
    )
