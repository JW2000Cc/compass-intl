"""
Fill .po files with translations for all 6 supported languages.

Run with: .venv/bin/python tools/fill_translations.py
Then compile: .venv/bin/pybabel compile -d translations
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

from babel.messages.pofile import read_po, write_po
from translations_data import TRANSLATIONS as T

TRANS_DIR = ROOT / "translations"


def fill_po(lang: str) -> tuple[int, list[str]]:
    po_path = TRANS_DIR / lang / "LC_MESSAGES" / "messages.po"
    with open(po_path, "rb") as f:
        catalog = read_po(f)
    import babel
    catalog.locale = babel.Locale.parse(lang)
    filled = 0
    missing = []
    for msg in catalog:
        if not msg.id:
            continue
        msgid = msg.id if isinstance(msg.id, str) else msg.id[0]
        if msgid in T and lang in T[msgid]:
            msg.string = T[msgid][lang]
            # Clear fuzzy/auto-translated flags so compile picks the msgstr
            if "fuzzy" in msg.flags:
                msg.flags.discard("fuzzy")
            filled += 1
        else:
            missing.append(msgid)
    with open(po_path, "wb") as f:
        write_po(f, catalog, width=80)
    return filled, missing


if __name__ == "__main__":
    print(f"Translation dict has {len(T)} msgids")
    for lang in ["en", "de", "fr", "it", "es", "tr"]:
        filled, missing = fill_po(lang)
        print(f"  [{lang}] filled {filled} strings; {len(missing)} missing")
        for m in missing[:5]:
            print(f"    MISSING: {m!r}")
