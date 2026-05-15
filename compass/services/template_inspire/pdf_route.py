"""PDF route — convert PDF → DOCX with pdf2docx, then route through DOCX route.

Fidelity: 85-95% for clean text-based PDFs. Drops to image-route if scan-only.
"""
from __future__ import annotations

import io
import logging
import tempfile
from pathlib import Path

from . import InspireResult

log = logging.getLogger(__name__)


def pdf_has_extractable_text(file_bytes: bytes) -> bool:
    """Quick check: does the PDF contain extractable text?
    If False, pdf2docx will produce a useless DOCX (just embedded images);
    we should route to image_route as fallback.
    """
    try:
        import pypdf
        reader = pypdf.PdfReader(io.BytesIO(file_bytes))
        # Sample first 3 pages for text
        sample = ""
        for page in reader.pages[:3]:
            sample += page.extract_text() or ""
            if len(sample) > 200:
                return True
        return len(sample.strip()) > 50
    except Exception as exc:
        log.warning("pdf text-extractability check failed: %s", exc)
        return True  # be permissive; let pdf2docx try


def pdf_to_docx_bytes(pdf_bytes: bytes) -> bytes:
    """Convert PDF → DOCX in memory using pdf2docx."""
    from pdf2docx import Converter

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as pdf_tmp:
        pdf_tmp.write(pdf_bytes)
        pdf_path = pdf_tmp.name
    docx_path = pdf_path.replace(".pdf", ".docx")
    try:
        cv = Converter(pdf_path)
        cv.convert(docx_path, start=0, end=None)
        cv.close()
        with open(docx_path, "rb") as f:
            return f.read()
    finally:
        try:
            Path(pdf_path).unlink(missing_ok=True)
            Path(docx_path).unlink(missing_ok=True)
        except OSError:
            pass


def inspire_pdf(file_bytes: bytes, filename: str, *, provider: str, api_key: str, model: str) -> InspireResult:
    if not pdf_has_extractable_text(file_bytes):
        # Fall through to image route
        log.info("PDF has no extractable text — routing to image_route as fallback")
        from .image_route import inspire_image
        result = inspire_image(file_bytes, filename, provider=provider, api_key=api_key, model=model)
        result.warnings.insert(0,
            "🛈 这个 PDF 看上去是扫描件 / 纯图片 — 已自动切到 vision LLM 路（保真度较低）"
        )
        return result

    try:
        docx_bytes = pdf_to_docx_bytes(file_bytes)
    except Exception as exc:
        log.exception("pdf2docx conversion failed")
        return InspireResult(
            theme_kind="docx", theme_bytes=b"", preview_html="",
            route_used="pdf", fidelity_estimate=0.0, notes=[], warnings=[],
            error=f"PDF → DOCX 转换失败：{exc}",
        )

    if not docx_bytes:
        return InspireResult(
            theme_kind="docx", theme_bytes=b"", preview_html="",
            route_used="pdf", fidelity_estimate=0.0, notes=[], warnings=[],
            error="pdf2docx 输出为空（PDF 内容可能完全是图片）",
        )

    # Now run the DOCX route on the converted file
    from .docx_route import inspire_docx

    result = inspire_docx(docx_bytes, filename.replace(".pdf", ".docx"),
                          provider=provider, api_key=api_key, model=model)
    # Annotate that this came via PDF
    result.route_used = "pdf→docx"
    result.fidelity_estimate = min(result.fidelity_estimate, 0.92)
    result.warnings.insert(0,
        "🛈 经过 PDF→DOCX 转换 — 复杂排版（多列、浮动图）可能位置略有偏差。"
        "如果可能，建议直接上传原 .docx 文件。"
    )
    return result
