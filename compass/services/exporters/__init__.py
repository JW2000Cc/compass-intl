"""Format-specific exporters built on top of build_json_resume payload.

ADR-0018 alignment layer: each exporter is a pure function
JSONResume → bytes (or str), no LLM, no IO except the file produced.

Available exporters:
  · render_markdown(resume) -> str
  · render_txt(resume)      -> str
  · render_pdf(docx_bytes)  -> bytes   # via LibreOffice headless

DOCX retain rendering lives in services/themes.py:render_docx_theme since it
needs the user's UserTheme (template path), not just the JSON payload.
"""
from .markdown_exporter import render_markdown  # noqa: F401
from .txt_exporter import render_txt  # noqa: F401
from .pdf_exporter import (  # noqa: F401
    LibreOfficeNotFoundError,
    detect_dominant_fonts,
    docx_to_pdf,
    find_libreoffice,
    fonts_likely_to_fall_back,
)
