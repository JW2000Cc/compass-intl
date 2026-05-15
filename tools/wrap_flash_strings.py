"""
Bulk-wrap `flash("中文...")` and `flash(f"中文 {var}")` patterns in
compass/routes/*.py with `_(...)` / `_("...", var=var)` form.

Adds `from flask_babel import gettext as _` import if missing.

Reports all wrapped strings so they can be batch-added to translations_data.

Run: python tools/wrap_flash_strings.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ROUTES_DIR = ROOT / "compass" / "routes"
CHINESE = re.compile(r"[一-鿿]")

# Patterns (we operate line-by-line for safety)
# Match: flash("中文 ..."[, "level"])  → flash(_("..."), "level")
# Match: flash(f"中文 {var}"[, "level"])  → flash(_("... %(var)s", var=var), "level")
# Avoid: already-wrapped flash(_(...))

PLAIN_RE = re.compile(
    r'flash\(\s*(["\'])((?:(?!\1).)*?[一-鿿](?:(?!\1).)*?)\1\s*(,\s*["\'][^"\']*["\'])?\s*\)'
)
FSTRING_RE = re.compile(
    r'flash\(\s*f(["\'])((?:(?!\1).)*?[一-鿿](?:(?!\1).)*?)\1\s*(,\s*["\'][^"\']*["\'])?\s*\)'
)

# {var}, {var.attr}, {obj.attr.name} extraction — be conservative
FSTRING_VAR_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_.]*?)\}")


def convert_fstring_to_named(fstring_body: str) -> tuple[str, dict[str, str]]:
    """
    Convert `中文 {a} 文 {b.attr}` →
        ("中文 %(a)s 文 %(b_attr)s", {"a": "a", "b_attr": "b.attr"}).
    """
    bindings: dict[str, str] = {}
    used: set[str] = set()

    def repl(m: re.Match) -> str:
        expr = m.group(1)
        # Make a valid identifier
        key = re.sub(r"[^a-zA-Z0-9_]", "_", expr)
        # Disambiguate
        original_key = key
        i = 1
        while key in used and bindings.get(key) != expr:
            i += 1
            key = f"{original_key}_{i}"
        used.add(key)
        bindings[key] = expr
        return f"%({key})s"

    new_text = FSTRING_VAR_RE.sub(repl, fstring_body)
    return new_text, bindings


def wrap_plain(match: re.Match) -> str:
    quote, body, level = match.group(1), match.group(2), match.group(3) or ""
    # Escape inner quote not needed because we use same quote inside _()
    return f"flash(_({quote}{body}{quote}){level})"


def wrap_fstring(match: re.Match) -> str:
    quote, body, level = match.group(1), match.group(2), match.group(3) or ""
    new_text, bindings = convert_fstring_to_named(body)
    args = ", ".join(f"{k}={v}" for k, v in bindings.items())
    if args:
        return f"flash(_({quote}{new_text}{quote}, {args}){level})"
    else:
        return f"flash(_({quote}{new_text}{quote}){level})"


def ensure_import(content: str) -> str:
    if re.search(r"from flask_babel import.*gettext", content):
        return content
    # Find first `from flask import` line
    m = re.search(r"^from flask import .*$", content, re.MULTILINE)
    if m:
        insert_pos = m.end()
        return content[:insert_pos] + "\nfrom flask_babel import gettext as _" + content[insert_pos:]
    # Fallback: insert after the last `import` near top
    lines = content.splitlines(keepends=True)
    last_import_idx = 0
    for i, line in enumerate(lines[:50]):
        if line.startswith("import ") or line.startswith("from "):
            last_import_idx = i
    lines.insert(last_import_idx + 1, "from flask_babel import gettext as _\n")
    return "".join(lines)


def process(path: Path) -> tuple[int, list[str]]:
    """Returns (count_wrapped, list_of_new_msgids)."""
    content = path.read_text(encoding="utf-8")
    original = content
    new_msgids: list[str] = []
    count = 0

    # Walk line-by-line so we don't accidentally span multi-line statements
    out_lines = []
    for line in content.splitlines(keepends=True):
        # Skip if line is already wrapped (flash(_(...)))
        if "flash(_(" in line:
            out_lines.append(line)
            continue
        # Skip if no Chinese in line
        if not CHINESE.search(line):
            out_lines.append(line)
            continue

        new_line = line

        # Try plain string flash first
        for m in PLAIN_RE.finditer(line):
            body = m.group(2)
            if CHINESE.search(body):
                new_msgids.append(body)
                count += 1
        new_line = PLAIN_RE.sub(wrap_plain, new_line)

        # Then f-string flash
        for m in FSTRING_RE.finditer(new_line):
            body = m.group(2)
            if CHINESE.search(body):
                # Convert {var} → %(name)s for the msgid
                converted, _bindings = convert_fstring_to_named(body)
                new_msgids.append(converted)
                count += 1
        new_line = FSTRING_RE.sub(wrap_fstring, new_line)

        out_lines.append(new_line)

    new_content = "".join(out_lines)
    if count > 0:
        new_content = ensure_import(new_content)

    if new_content != original:
        path.write_text(new_content, encoding="utf-8")

    return count, new_msgids


def main() -> None:
    total = 0
    all_msgids: list[str] = []
    for f in sorted(ROUTES_DIR.glob("*.py")):
        if f.name == "__init__.py":
            continue
        count, msgids = process(f)
        if count:
            print(f"  {f.name}: wrapped {count}")
        total += count
        all_msgids.extend(msgids)
    print(f"\nTotal wrapped: {total}")
    print(f"New msgids: {len(all_msgids)} ({len(set(all_msgids))} unique)")
    # Dump unique msgids for translation
    print("\n─── unique new msgids ───")
    for m in sorted(set(all_msgids)):
        # Show first 100 chars
        print(f"  {m[:100]}")


if __name__ == "__main__":
    main()
