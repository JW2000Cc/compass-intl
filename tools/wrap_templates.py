"""
Auto-wrap Chinese strings in all Sundial templates with {{ _('...') }}.

Strategy:
1. For each template, find Chinese-containing text (between > and < tags)
2. Find Chinese in attribute values (placeholder, title, alt, label, data-*)
3. Skip text inside <script>, <style>, comments
4. Skip text already inside {{ _( ... ) }}
5. Output wrapped templates

Run with: .venv/bin/python tools/wrap_templates.py
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TPL_DIR = ROOT / "compass" / "templates"


def find_protected_ranges(html: str) -> list[tuple[int, int]]:
    """Char offsets of <script>, <style>, <!-- --> blocks."""
    ranges = []
    for m in re.finditer(r"<(script|style)\b[^>]*>(.*?)</\1>", html, re.DOTALL | re.IGNORECASE):
        ranges.append((m.start(), m.end()))
    for m in re.finditer(r"<!--.*?-->", html, re.DOTALL):
        ranges.append((m.start(), m.end()))
    return ranges


def in_protected(pos: int, ranges) -> bool:
    return any(a <= pos < b for a, b in ranges)


CHINESE_RE = re.compile(r"[一-鿿]")
ALREADY_WRAPPED = re.compile(r"\{\{\s*_\(")


def has_chinese(s: str) -> bool:
    return bool(CHINESE_RE.search(s))


def escape_jinja_string(s: str) -> str:
    """Make a Python string safe for embedding inside {{ _('...') }}."""
    return s.replace("\\", "\\\\").replace("'", "\\'")


def wrap_text_between_tags(html: str) -> tuple[str, int]:
    """Wrap Chinese-containing text between > and < with {{ _('...') }}."""
    ranges = find_protected_ranges(html)
    out = []
    last = 0
    count = 0

    # Find every >...< pair (text node candidates)
    pattern = re.compile(r">([^<>{}\n]*[一-鿿][^<>{}]*?)<", re.MULTILINE)
    for m in pattern.finditer(html):
        if in_protected(m.start(), ranges):
            continue
        text = m.group(1)
        # Skip if already wrapped
        if ALREADY_WRAPPED.search(text):
            continue
        # Skip if it contains Jinja syntax inside
        if "{{" in text or "{%" in text:
            continue
        stripped = text.strip()
        if len(stripped) < 1:
            continue
        # Preserve leading/trailing whitespace
        lead = text[: len(text) - len(text.lstrip())]
        trail = text[len(text.rstrip()):]
        out.append(html[last:m.start() + 1])  # include '>'
        out.append(f"{lead}{{{{ _('{escape_jinja_string(stripped)}') }}}}{trail}")
        last = m.end() - 1  # don't include '<'
        count += 1
    out.append(html[last:])
    return "".join(out), count


def wrap_attribute(html: str, attr: str) -> tuple[str, int]:
    """Wrap Chinese-containing attribute value with {{ _('...') }}."""
    ranges = find_protected_ranges(html)
    out = []
    last = 0
    count = 0
    pattern = re.compile(rf'\b{attr}="([^"]*[一-鿿][^"]*)"')
    for m in pattern.finditer(html):
        if in_protected(m.start(), ranges):
            continue
        val = m.group(1)
        if ALREADY_WRAPPED.search(val):
            continue
        if "{{" in val or "{%" in val:
            continue
        out.append(html[last:m.start()])
        out.append(f'{attr}="{{{{ _(\'{escape_jinja_string(val)}\') }}}}"')
        last = m.end()
        count += 1
    out.append(html[last:])
    return "".join(out), count


def process_template(path: Path) -> tuple[int, int]:
    """Returns (text_wrapped, attr_wrapped) counts."""
    html = path.read_text(encoding="utf-8")
    original = html
    html, n_text = wrap_text_between_tags(html)
    a_total = 0
    for attr in ("placeholder", "title", "alt", "aria-label", "data-tooltip"):
        html, n = wrap_attribute(html, attr)
        a_total += n
    if html != original:
        path.write_text(html, encoding="utf-8")
    return n_text, a_total


def main() -> None:
    total_text = 0
    total_attr = 0
    for tpl in sorted(TPL_DIR.rglob("*.html")):
        if tpl.name == "_language_switcher.html":
            continue
        if not has_chinese(tpl.read_text(encoding="utf-8")):
            continue
        t, a = process_template(tpl)
        print(f"  {tpl.name}: {t} text wraps, {a} attr wraps")
        total_text += t
        total_attr += a
    print(f"\nTotal: {total_text} text + {total_attr} attr = {total_text + total_attr} wraps applied.")


if __name__ == "__main__":
    main()
