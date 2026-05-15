"""Plain text exporter — ATS-safe output.

Some ATS pipelines reject anything but raw text. We strip Markdown markers
(`#`, `*`, `**`, `-`) and emit clean line-broken paragraphs. Section headings
stay capitalized + spaced for readability.

Pure function. No LLM. No IO.
"""
from __future__ import annotations

import re

from .markdown_exporter import render_markdown


_MD_HEADING_RE = re.compile(r"^#{1,6}\s+", flags=re.MULTILINE)
_MD_BULLET_RE = re.compile(r"^[-*]\s+", flags=re.MULTILINE)
_MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_MD_ITAL_RE = re.compile(r"(?<!\*)\*(?!\*)([^\*]+?)\*(?!\*)")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_MD_CODE_RE = re.compile(r"`([^`]+)`")


def render_txt(resume: dict) -> str:
    """Render JSON Resume → plain text by stripping Markdown markers."""
    md = render_markdown(resume)
    text = md
    # Drop heading prefixes; keep the heading text
    text = _MD_HEADING_RE.sub("", text)
    text = _MD_BULLET_RE.sub("• ", text)
    text = _MD_BOLD_RE.sub(r"\1", text)
    text = _MD_ITAL_RE.sub(r"\1", text)
    text = _MD_LINK_RE.sub(r"\1", text)
    text = _MD_CODE_RE.sub(r"\1", text)
    # Collapse 3+ blank lines to 2
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text
