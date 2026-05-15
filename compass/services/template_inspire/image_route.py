"""Image route — vision LLM recreates HTML+CSS Jinja from image.

Fidelity: 70-80% — LLM "looks" at image and re-generates CSS from scratch.
Loses: exact font metrics, custom backgrounds, fancy SVG/decorations.
Use case: scanned PDFs, screenshots, image-only resume samples.
"""
from __future__ import annotations

import base64
import logging
from pathlib import Path

from . import InspireResult

log = logging.getLogger(__name__)


SYSTEM_PROMPT = """You are a resume-template designer. The user shows you an
image of a resume design they want to reuse. Generate a valid HTML+CSS
template using Jinja2 placeholders so the user can render it with their data.

Available data variables (Compass JSON Resume schema):

  r.basics.name, r.basics.email, r.basics.phone, r.basics.location.address
  r.basics.summary
  r.work[N].name, r.work[N].position, r.work[N].location
  r.work[N].startDate, r.work[N].endDate, r.work[N].highlights[M]
  r.education[N].institution, r.education[N].studyType, r.education[N].area
  r.education[N].startDate, r.education[N].endDate, r.education[N].gpa
  r.skills[N].name, r.skills[N].keywords[M]
  r.languages[N].language
  r.projects[N].name, r.projects[N].description, r.projects[N].keywords[M]
  r.certificates[N].name, r.certificates[N].issuer, r.certificates[N].date

REQUIREMENTS:
1. Output ONE COMPLETE HTML document with embedded <style> — no external CSS, no external fonts.
2. Match the image's layout: column count, sidebar position, color palette, font weights, spacing.
3. Use Jinja2 {% for %}{% endfor %} loops over r.work, r.education, r.skills etc.
4. Use semantic HTML (h1, h2, ul/li, sections) — no positioned divs.
5. Add @page A4 + @media print rules so it prints cleanly.
6. NO commentary, NO markdown fences — output raw HTML only, starting with <!DOCTYPE html>.

If the image shows things you can't reproduce (custom illustrations, photos),
gracefully approximate (use <div class="photo-placeholder">) without breaking layout.
"""


def _detect_image_mime(file_bytes: bytes, filename: str) -> str:
    if file_bytes.startswith(b"\x89PNG"):
        return "image/png"
    if file_bytes.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if file_bytes.startswith(b"GIF8"):
        return "image/gif"
    if file_bytes.startswith(b"RIFF") and b"WEBP" in file_bytes[:16]:
        return "image/webp"
    if file_bytes.startswith(b"%PDF"):
        return "application/pdf"
    ext = Path(filename).suffix.lower()
    return {
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".gif": "image/gif", ".webp": "image/webp",
    }.get(ext, "image/png")


def _vision_chat_claude(*, api_key: str, model: str, system: str,
                        image_bytes: bytes, mime_type: str) -> str:
    """Make a vision-enabled Claude call. Returns raw text response."""
    import anthropic

    client = anthropic.Anthropic(api_key=api_key)
    img_b64 = base64.b64encode(image_bytes).decode("ascii")

    # Anthropic Messages API expects: messages=[{role:user, content:[{type:image,...}, {type:text,...}]}]
    msg = client.messages.create(
        model=model,
        max_tokens=8000,
        temperature=0.3,
        system=system,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": mime_type, "data": img_b64},
                },
                {
                    "type": "text",
                    "text": "Generate the HTML+CSS+Jinja template for this resume image.",
                },
            ],
        }],
    )
    text = "".join(getattr(b, "text", "") for b in msg.content)
    return text


def _vision_chat_openai(*, api_key: str, model: str, system: str,
                       image_bytes: bytes, mime_type: str) -> str:
    """Make a vision-enabled OpenAI call (gpt-4o-class). Returns raw text."""
    from openai import OpenAI

    client = OpenAI(api_key=api_key)
    img_b64 = base64.b64encode(image_bytes).decode("ascii")

    resp = client.chat.completions.create(
        model=model,
        max_tokens=8000,
        temperature=0.3,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": [
                {"type": "text", "text": "Generate the HTML+CSS+Jinja template."},
                {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{img_b64}"}},
            ]},
        ],
    )
    return resp.choices[0].message.content


def _strip_html_fences(text: str) -> str:
    """LLM sometimes wraps in ```html ... ``` despite instructions."""
    text = text.strip()
    if text.startswith("```"):
        # Drop first line (```html or ```)
        first_nl = text.find("\n")
        if first_nl > 0:
            text = text[first_nl + 1:]
    if text.endswith("```"):
        text = text.rsplit("```", 1)[0]
    return text.strip()


def _convert_pdf_first_page_to_image(pdf_bytes: bytes) -> tuple[bytes, str]:
    """For scanned PDFs, render the first page as an image so vision LLM can see it."""
    try:
        import pypdf
        # pypdf doesn't render to image directly. Use PyMuPDF (fitz) which is
        # already a transitive dep of pdf2docx.
        import fitz  # PyMuPDF
        # try/finally guards against doc leak if get_pixmap/tobytes throws —
        # fitz docs hold C-level memory.
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        try:
            page = doc[0]
            pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))  # 2x scale for clarity
            img_bytes = pix.tobytes("png")
        finally:
            doc.close()
        return img_bytes, "image/png"
    except Exception as exc:
        log.warning("PDF → image rendering failed: %s", exc)
        raise


def inspire_image(file_bytes: bytes, filename: str, *, provider: str, api_key: str, model: str) -> InspireResult:
    """Vision-LLM-based recreation. Works on PNG/JPG/PDF inputs."""
    mime = _detect_image_mime(file_bytes, filename)

    # If PDF, convert first page to PNG (vision LLMs prefer raster)
    if mime == "application/pdf":
        try:
            file_bytes, mime = _convert_pdf_first_page_to_image(file_bytes)
        except Exception as exc:
            return InspireResult(
                theme_kind="html", theme_bytes=b"", preview_html="",
                route_used="image", fidelity_estimate=0.0, notes=[], warnings=[],
                error=f"PDF → 图片渲染失败（无法走视觉路）：{exc}",
            )

    log.info("inspire_image: mime=%s size=%dKB provider=%s", mime, len(file_bytes) // 1024, provider)

    # Bound image size (vision API ~5MB cap)
    if len(file_bytes) > 4_500_000:
        return InspireResult(
            theme_kind="html", theme_bytes=b"", preview_html="",
            route_used="image", fidelity_estimate=0.0, notes=[], warnings=[],
            error="图片过大（>4.5MB）— 请压缩后重试",
        )

    try:
        if provider == "claude":
            html_raw = _vision_chat_claude(
                api_key=api_key, model=model, system=SYSTEM_PROMPT,
                image_bytes=file_bytes, mime_type=mime,
            )
        elif provider == "openai":
            html_raw = _vision_chat_openai(
                api_key=api_key, model=model, system=SYSTEM_PROMPT,
                image_bytes=file_bytes, mime_type=mime,
            )
        else:
            return InspireResult(
                theme_kind="html", theme_bytes=b"", preview_html="",
                route_used="image", fidelity_estimate=0.0, notes=[], warnings=[],
                error=f"vision LLM 不支持的 provider: {provider}",
            )
    except Exception as exc:
        return InspireResult(
            theme_kind="html", theme_bytes=b"", preview_html="",
            route_used="image", fidelity_estimate=0.0, notes=[], warnings=[],
            error=f"Vision LLM 调用失败：{exc}",
        )

    html_template = _strip_html_fences(html_raw)
    if "<html" not in html_template.lower():
        return InspireResult(
            theme_kind="html", theme_bytes=b"", preview_html="",
            route_used="image", fidelity_estimate=0.0, notes=[], warnings=[],
            error="LLM 输出不像 HTML — 重试或换张图片",
        )

    # Render with sample to verify + produce preview
    sample_preview = _render_sample(html_template)

    return InspireResult(
        theme_kind="html",
        theme_bytes=html_template.encode("utf-8"),
        preview_html=sample_preview,
        route_used="image",
        fidelity_estimate=0.75,
        notes=[],
        warnings=[
            "🎨 图片路靠 vision LLM 重画，保真度 70-80%",
            "字体细节、阴影、复杂背景可能丢失 — 预览检查后再保存",
        ],
    )


def _render_sample(template_html: str) -> str:
    from jinja2 import Environment, BaseLoader, select_autoescape
    from ...routes.themes import SAMPLE_RESUME

    try:
        env = Environment(loader=BaseLoader(), autoescape=select_autoescape(["html"]))
        tpl = env.from_string(template_html)
        return tpl.render(r=SAMPLE_RESUME)
    except Exception as exc:
        return f"<html><body><p>预览渲染失败 — Jinja 报错：{exc}</p><pre>{template_html[:2000]}</pre></body></html>"
