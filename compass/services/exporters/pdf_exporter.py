"""PDF exporter — DOCX bytes → PDF bytes via LibreOffice headless.

Borrowed from Resume Generator/exporters/pdf_exporter.py with adjustments:
  · returns bytes (not output_path) for streaming via Flask Response
  · raises LibreOffice NotFoundError so callers can show install hint
  · works cross-platform (mac / linux / win)

Why LibreOffice and not pure-Python:
  · `python-docx` doesn't render to PDF — DOCX is XML, PDF needs layout engine
  · LibreOffice (`soffice --headless --convert-to pdf`) is the de-facto open
    tool for high-fidelity DOCX→PDF; pixel-equivalent to opening in Word
  · weasyprint route would re-render from Markdown / HTML, losing all the
    DOCX format the user just retained. That defeats ADR-0018.

If LibreOffice is missing we raise — caller decides UX (show install hint
to the user, fall back to "download .docx, convert in Word manually", etc).
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)


class LibreOfficeNotFoundError(RuntimeError):
    """Raised when soffice/libreoffice can't be located on disk."""


def detect_dominant_fonts(docx_bytes: bytes) -> list[str]:
    """Pull the most-used font names from a DOCX. Best-effort, no exception
    on parse failure — return empty list to skip the warning gracefully.

    We only need a small set of names so we sample the first ~200 paragraph
    runs (rPr/rFonts) — full traversal isn't worth it for a UI warning.
    """
    try:
        import io
        from docx import Document  # noqa: F401 (lazy)
        from docx.oxml.ns import qn

        doc = Document(io.BytesIO(docx_bytes))
        seen: dict[str, int] = {}
        sampled = 0
        for para in doc.paragraphs:
            for run in para.runs:
                rPr = run._r.find(qn("w:rPr"))
                if rPr is None:
                    continue
                rFonts = rPr.find(qn("w:rFonts"))
                if rFonts is None:
                    continue
                for attr in ("ascii", "hAnsi", "eastAsia", "cs"):
                    val = rFonts.get(qn(f"w:{attr}"))
                    if val:
                        seen[val] = seen.get(val, 0) + 1
                sampled += 1
                if sampled > 200:
                    break
            if sampled > 200:
                break
        # Top 3 by count
        return [name for name, _ in sorted(seen.items(), key=lambda kv: -kv[1])[:3]]
    except Exception:  # noqa: BLE001
        return []


def fonts_likely_to_fall_back(font_names: list[str], target_lang: str | None) -> list[str]:
    """Given font names + a target language, return any that are likely to
    fall back to a system substitute (visible visual change in PDF).

    Heuristic only — we don't actually probe LibreOffice's font cache. The
    UX value is "give the user a heads-up", not "guarantee accuracy".
    """
    cjk_fonts = {
        "SimSun", "SimHei", "宋体", "黑体", "Microsoft YaHei", "PingFang SC",
        "PingFang TC", "Hiragino Sans", "Yu Gothic", "MS Gothic", "MS Mincho",
        "Malgun Gothic", "Noto Sans CJK", "Source Han Sans",
    }
    western_target = (target_lang or "").lower() in {
        "en", "it", "fr", "de", "es", "pt", "nl", "sv", "no", "da", "fi", "pl",
    }
    if not western_target:
        return []
    return [n for n in font_names if any(cjk in n for cjk in cjk_fonts)]


def find_libreoffice() -> str:
    """Return path to soffice / libreoffice executable, or empty string if absent."""
    for name in ("soffice", "libreoffice"):
        found = shutil.which(name)
        if found:
            return found
    if sys.platform == "darwin":
        mac_path = "/Applications/LibreOffice.app/Contents/MacOS/soffice"
        if Path(mac_path).exists():
            return mac_path
    if sys.platform == "win32":
        import glob

        candidates = [
            r"C:\Program Files\LibreOffice\program\soffice.exe",
            r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
        ]
        candidates += glob.glob(r"C:\Program Files\LibreOffice*\program\soffice.exe")
        candidates += glob.glob(r"C:\Program Files (x86)\LibreOffice*\program\soffice.exe")
        for path in candidates:
            if Path(path).exists():
                return path
    return ""


def docx_to_pdf(docx_bytes: bytes, *, timeout: int = 60) -> bytes:
    """Convert DOCX bytes → PDF bytes via LibreOffice headless.

    Raises:
        LibreOfficeNotFoundError: if soffice can't be found
        RuntimeError: if soffice command fails (corrupt docx, etc)
    """
    exe = find_libreoffice()
    if not exe:
        raise LibreOfficeNotFoundError(
            "LibreOffice not found on this system. Install from "
            "https://www.libreoffice.org/ or use the bundled installer."
        )

    with tempfile.TemporaryDirectory(prefix="compass_pdf_") as tmpdir:
        tmpdir_p = Path(tmpdir)
        in_docx = tmpdir_p / "in.docx"
        in_docx.write_bytes(docx_bytes)

        kwargs: dict = {"capture_output": True, "text": True, "timeout": timeout}
        if sys.platform == "win32":
            # Suppress console window flash on Windows
            kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW

        result = subprocess.run(
            [exe, "--headless", "--convert-to", "pdf",
             "--outdir", str(tmpdir_p), str(in_docx)],
            **kwargs,
        )

        if result.returncode != 0:
            raise RuntimeError(
                f"LibreOffice exited with code {result.returncode}: "
                f"{(result.stderr or result.stdout or '').strip()}"
            )

        out_pdf = tmpdir_p / "in.pdf"
        if not out_pdf.exists():
            raise RuntimeError(
                f"LibreOffice didn't produce expected output: {out_pdf}"
            )
        return out_pdf.read_bytes()
