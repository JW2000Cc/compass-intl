"""Template inspiration — convert any uploaded resume template into a Compass-
compatible Jinja template (HTML or DOCX), preserving original formatting where
possible.

Architecture: 4 routes by input format, each with different fidelity guarantee.

  DOCX → docx_route          99% fidelity (XML untouched, only text replaced)
  HTML → html_route          97% fidelity (DOM untouched, only text replaced)
  PDF  → pdf_route → docx    85-95% (depends on pdf2docx conversion quality)
  IMG  → image_route         70-80% (vision LLM recreates from scratch)

Public API:
    inspire_from_file(file_bytes, filename, llm_settings) → InspireResult
    save_inspired(InspireResult, name, description) → user theme record
"""
from __future__ import annotations

import logging

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────
# Result types
# ────────────────────────────────────────────────────────────


@dataclass
class InspireResult:
    """Output of an inspire run.

    `theme_kind`: "docx" | "html" — what to save as
    `theme_bytes`: the rendered template bytes (utf-8 HTML or .docx binary)
    `preview_html`: HTML string for in-browser preview (mammoth-converted for docx)
    `route_used`: which path produced this ("docx" / "html" / "pdf" / "image")
    `fidelity_estimate`: rough 0-1 score for transparency
    `notes`: per-row LLM annotations (which text → which field)
    `warnings`: list of caveats user should know
    """
    theme_kind: str
    theme_bytes: bytes
    preview_html: str
    route_used: str
    fidelity_estimate: float
    notes: list[dict]
    warnings: list[str]
    error: Optional[str] = None


# ────────────────────────────────────────────────────────────
# Format detection
# ────────────────────────────────────────────────────────────


def detect_format(filename: str, file_bytes: bytes) -> str:
    """Return one of: 'docx' | 'html' | 'pdf' | 'image' | 'unknown'."""
    ext = Path(filename).suffix.lower()

    # Magic bytes (more trustworthy than extension)
    if file_bytes.startswith(b"PK"):
        # DOCX is a zip starting with PK
        if ext in {".docx", ".docm"} or b"word/" in file_bytes[:4096]:
            return "docx"
        return "unknown"
    if file_bytes.startswith(b"%PDF"):
        return "pdf"
    if file_bytes.startswith(b"\x89PNG") or file_bytes.startswith(b"\xff\xd8\xff"):
        return "image"
    if ext in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
        return "image"

    # Text-y: check for HTML signature
    head = file_bytes[:2048].decode("utf-8", errors="ignore").lower()
    if "<html" in head or "<!doctype html" in head:
        return "html"
    if ext in {".html", ".htm"}:
        return "html"

    return "unknown"


# ────────────────────────────────────────────────────────────
# Public API — dispatcher
# ────────────────────────────────────────────────────────────


def inspire_from_file(
    file_bytes: bytes,
    filename: str,
    *,
    provider: str,
    api_key: str,
    model: str,
    force_route: Optional[str] = None,
) -> InspireResult:
    """Convert uploaded template file to Compass-compatible template."""
    fmt = force_route or detect_format(filename, file_bytes)
    log.info("inspire_from_file: filename=%s detected=%s", filename, fmt)

    try:
        if fmt == "docx":
            from .docx_route import inspire_docx
            return inspire_docx(file_bytes, filename, provider=provider, api_key=api_key, model=model)
        if fmt == "html":
            from .html_route import inspire_html
            return inspire_html(file_bytes, filename, provider=provider, api_key=api_key, model=model)
        if fmt == "pdf":
            from .pdf_route import inspire_pdf
            return inspire_pdf(file_bytes, filename, provider=provider, api_key=api_key, model=model)
        if fmt == "image":
            from .image_route import inspire_image
            return inspire_image(file_bytes, filename, provider=provider, api_key=api_key, model=model)
    except Exception as exc:  # noqa: BLE001
        log.exception("inspire_from_file failed for %s (route=%s): %s", filename, fmt, exc)
        return InspireResult(
            theme_kind="html", theme_bytes=b"", preview_html="",
            route_used=fmt, fidelity_estimate=0.0,
            notes=[], warnings=[],
            error=f"{fmt} 路处理失败：{exc}",
        )

    return InspireResult(
        theme_kind="html", theme_bytes=b"", preview_html="",
        route_used="unknown", fidelity_estimate=0.0,
        notes=[], warnings=[],
        error=f"未支持的格式：{Path(filename).suffix or '(无扩展名)'}",
    )
