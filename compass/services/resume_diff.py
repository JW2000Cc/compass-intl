"""Word-level diff between original fact and rewritten variant.

Output is inline HTML with <del>/<ins> tags styled like Word's track-changes:
deleted words shown in red strikethrough, added words in accent color underlined.
This is the visualization the user asked for in 2026-05-02 — replaces the
"two boxes side-by-side" review UX with a single track-changes paragraph.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from typing import Optional

from markupsafe import Markup, escape


# Tokenize on word boundaries while preserving spaces/punctuation. Keeps the
# diff readable when one version has different punctuation than the other.
_TOKEN_RE = re.compile(r"\w+|[^\w\s]+|\s+", re.UNICODE)


def _tokenize(s: str) -> list[str]:
    return _TOKEN_RE.findall(s or "")


def diff_html(original: str, new: str) -> Markup:
    """Render an inline diff of original→new as Markup-safe HTML.

    Whitespace tokens are not styled (avoids visual stutter). Empty input
    returns an empty Markup — caller decides what to render in that case.
    """
    if not original and not new:
        return Markup("")
    if not original:
        return Markup(f'<ins class="diff-ins">{escape(new)}</ins>')
    if not new:
        return Markup(f'<del class="diff-del">{escape(original)}</del>')

    o_tokens = _tokenize(original)
    n_tokens = _tokenize(new)

    matcher = difflib.SequenceMatcher(a=o_tokens, b=n_tokens, autojunk=False)
    out_parts: list[str] = []

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            chunk = "".join(o_tokens[i1:i2])
            out_parts.append(escape(chunk))
        elif tag == "delete":
            chunk = "".join(o_tokens[i1:i2])
            if chunk.strip():
                out_parts.append(f'<del class="diff-del">{escape(chunk)}</del>')
            else:
                out_parts.append(escape(chunk))
        elif tag == "insert":
            chunk = "".join(n_tokens[j1:j2])
            if chunk.strip():
                out_parts.append(f'<ins class="diff-ins">{escape(chunk)}</ins>')
            else:
                out_parts.append(escape(chunk))
        elif tag == "replace":
            del_chunk = "".join(o_tokens[i1:i2])
            ins_chunk = "".join(n_tokens[j1:j2])
            if del_chunk.strip():
                out_parts.append(f'<del class="diff-del">{escape(del_chunk)}</del>')
            if ins_chunk.strip():
                # If we just emitted a non-empty del, add a space for legibility
                if del_chunk.strip() and not (out_parts and out_parts[-1].endswith(" ")):
                    out_parts.append(" ")
                out_parts.append(f'<ins class="diff-ins">{escape(ins_chunk)}</ins>')

    return Markup("".join(out_parts))


# ─────────────────────────────────────────────────────────
# Amplification level — semantic label for each L0-L5 variant
# ─────────────────────────────────────────────────────────


_LEVEL_LABELS = {
    0: ("L0", "直接复制 — 无改动", "var(--muted)"),
    1: ("L1", "同义改写 — 换更主动的动词", "var(--accent)"),
    2: ("L2", "显式化 — 把隐含信息说出来", "var(--accent)"),
    3: ("L3", "重新定调 — 同事实换角度，需访谈支撑", "var(--warn)"),
    4: ("L4", "推断量化 — 添了数字 / 比例", "var(--warn)"),
    5: ("L5", "捏造 — 不该到这里", "var(--danger)"),
}


@dataclass
class LevelMeta:
    code: str
    label: str
    color: str


def level_meta(level: Optional[int]) -> LevelMeta:
    code, label, color = _LEVEL_LABELS.get(level, ("L?", "未知层级", "var(--muted)"))
    return LevelMeta(code=code, label=label, color=color)
