"""Markdown exporter — JSON Resume → Markdown text.

Standard headings (`# Name`, `## Section`, `### Entry`) so the output works in
any Markdown renderer (LinkedIn copy-paste, Notion, Bear, Obsidian, ...).

Pure function. No LLM. No IO. ADR-0018 alignment-layer pattern.
"""
from __future__ import annotations


def _line_or_blank(s: str | None) -> str:
    return s.strip() if isinstance(s, str) and s.strip() else ""


def render_markdown(resume: dict) -> str:
    """Render a JSON Resume payload to Markdown text."""
    out: list[str] = []
    basics = resume.get("basics") or {}

    name = _line_or_blank(basics.get("name"))
    if name:
        out.append(f"# {name}")

    contact_bits = [
        _line_or_blank(basics.get("email")),
        _line_or_blank(basics.get("phone")),
        _line_or_blank((basics.get("location") or {}).get("address")
                       if isinstance(basics.get("location"), dict)
                       else basics.get("location")),
        _line_or_blank(basics.get("url")),
    ]
    contact_bits = [b for b in contact_bits if b]
    if contact_bits:
        out.append(" · ".join(contact_bits))

    summary = _line_or_blank(basics.get("summary"))
    if summary:
        out.append("")
        out.append(summary)

    # work
    work = resume.get("work") or []
    if work:
        out.append("")
        out.append("## Work Experience")
        for w in work:
            company = _line_or_blank(w.get("name"))
            position = _line_or_blank(w.get("position"))
            start = _line_or_blank(w.get("startDate"))
            end = _line_or_blank(w.get("endDate")) or "Present"
            head = position or "Role"
            if company:
                head += f" · {company}"
            if start or end:
                head += f" · {start} – {end}"
            out.append("")
            out.append(f"### {head}")
            for h in (w.get("highlights") or []):
                if isinstance(h, str) and h.strip():
                    out.append(f"- {h.strip()}")

    # education
    edu = resume.get("education") or []
    if edu:
        out.append("")
        out.append("## Education")
        for e in edu:
            inst = _line_or_blank(e.get("institution"))
            area = _line_or_blank(e.get("area"))
            study = _line_or_blank(e.get("studyType"))
            start = _line_or_blank(e.get("startDate"))
            end = _line_or_blank(e.get("endDate"))
            head = " · ".join([x for x in (study, area, inst) if x])
            if start or end:
                head += f" · {start} – {end}".rstrip(" – ")
            out.append("")
            out.append(f"### {head}")
            for c in (e.get("courses") or []):
                if isinstance(c, str) and c.strip():
                    out.append(f"- {c.strip()}")

    # skills
    skills = resume.get("skills") or []
    if skills:
        out.append("")
        out.append("## Skills")
        for s in skills:
            label = _line_or_blank(s.get("name"))
            kws = s.get("keywords") or []
            line = ""
            if label:
                line = f"**{label}**: "
            line += ", ".join(k for k in kws if isinstance(k, str) and k.strip())
            if line.strip(":* "):
                out.append(f"- {line}")

    # projects (optional in JSON Resume)
    projects = resume.get("projects") or []
    if projects:
        out.append("")
        out.append("## Projects")
        for p in projects:
            name_ = _line_or_blank(p.get("name"))
            desc = _line_or_blank(p.get("description"))
            out.append("")
            out.append(f"### {name_}")
            if desc:
                out.append(desc)
            for h in (p.get("highlights") or []):
                if isinstance(h, str) and h.strip():
                    out.append(f"- {h.strip()}")

    # languages
    languages = resume.get("languages") or []
    if languages:
        out.append("")
        out.append("## Languages")
        for lang in languages:
            l_name = _line_or_blank(lang.get("language"))
            fluency = _line_or_blank(lang.get("fluency"))
            line = l_name + (f" ({fluency})" if fluency else "")
            if line:
                out.append(f"- {line}")

    out.append("")
    return "\n".join(out)
