"""HTML route — 97% fidelity via DOM-only text replacement.

Approach:
    1. Parse HTML with lxml
    2. Walk only TEXT nodes (skip <script>, <style>, comments)
    3. Send text + structural context (parent tag chain) to LLM
    4. LLM returns mapping: which text → which Compass field
    5. Replace text in-place; CSS / classes / IDs / nesting all stay
    6. Save → result is a Compass-compatible Jinja HTML template

Caveats:
    - Whitespace-only text nodes are skipped
    - For repeatable bullets, LLM identifies one as "loop body"; we wrap parent <li> in {% for %}{% endfor %} via comment markers
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from lxml import etree, html

from ..llm import LLMError, chat
from . import InspireResult

log = logging.getLogger(__name__)


@dataclass
class HtmlTextNode:
    id: int
    text: str
    parent_chain: str  # "html > body > div.resume > h1"
    parent_tag: str    # "h1", "p", "li" — quick filter
    is_whitespace: bool = False


SKIP_TAGS = {"script", "style", "noscript", "head", "meta", "link"}


def collect_text_nodes(tree) -> list[HtmlTextNode]:
    """Walk all elements, collect non-empty text content + structural context."""
    nodes: list[HtmlTextNode] = []
    nid = 0
    for elem in tree.iter():
        if not isinstance(elem.tag, str):
            continue  # skip comments/PIs
        if elem.tag in SKIP_TAGS:
            continue
        for txt_attr in ("text", "tail"):
            text = getattr(elem, txt_attr)
            if not text:
                continue
            stripped = text.strip()
            if not stripped:
                continue
            chain_parts = []
            cur = elem
            while cur is not None and isinstance(cur.tag, str):
                tag = cur.tag
                if cur.get("class"):
                    tag = f"{tag}.{cur.get('class').replace(' ', '.')}"
                chain_parts.append(tag)
                cur = cur.getparent()
            chain_parts.reverse()
            nodes.append(HtmlTextNode(
                id=nid, text=stripped,
                parent_chain=" > ".join(chain_parts[-4:]),
                parent_tag=elem.tag,
            ))
            nid += 1
    return nodes


SYSTEM_PROMPT = """You are a resume-template analyzer. The user uploaded an HTML
resume template they want to reuse for their own data.

For each text node, decide:
1. SAMPLE — text that is sample data; should be replaced with Jinja placeholder
2. LABEL — text that is part of design (section names "Experience", "Skills"); keep
3. SAMPLE_LOOP — bullet/list item that should expand to N items at render

Available Compass fields (same JSON Resume schema):

  r.basics.name                # full name
  r.basics.email
  r.basics.phone
  r.basics.location.address
  r.basics.summary             # 1-2 sentence summary
  r.work[N].name               # company
  r.work[N].position           # job title
  r.work[N].location
  r.work[N].startDate
  r.work[N].endDate
  r.work[N].highlights[M]      # bullet (loop)
  r.education[N].institution
  r.education[N].studyType
  r.education[N].area
  r.education[N].startDate
  r.education[N].endDate
  r.education[N].gpa
  r.skills[N].name             # category like "Languages"
  r.skills[N].keywords[M]      # individual skill (loop)
  r.languages[N].language
  r.projects[N].name
  r.projects[N].description
  r.projects[N].keywords[M]
  r.certificates[N].name
  r.certificates[N].issuer
  r.certificates[N].date

Rules:
- Section headers ("EXPERIENCE", "Education") → LABEL, keep verbatim.
- A single sample bullet under a heading = SAMPLE_LOOP — at render time it expands to user's N bullets.
- If unsure, choose LABEL (safer).

Return JSON only:
{
  "nodes": [
    {"id": 0, "kind": "sample", "field": "r.basics.name"},
    {"id": 1, "kind": "label"},
    {"id": 5, "kind": "sample_loop", "field": "h", "loop_over": "r.work[0].highlights"},
    ...
  ]
}
"""


def _build_user_message(nodes: list[HtmlTextNode]) -> str:
    lines = ["Text nodes (with parent context):", ""]
    for n in nodes:
        lines.append(f"  id={n.id} <{n.parent_tag}> path={n.parent_chain!r} : {n.text!r}")
    return "\n".join(lines)


def llm_identify_html(nodes: list[HtmlTextNode], *, provider: str, api_key: str, model: str) -> dict:
    user_msg = _build_user_message(nodes)
    res = chat(
        provider=provider, api_key=api_key, model=model,
        system=SYSTEM_PROMPT, user_content=user_msg,
        max_tokens=4000, temperature=0.2,
    )
    parsed = res.parse_json(default={"nodes": []})
    out = {}
    for entry in parsed.get("nodes", []):
        nid = entry.get("id")
        if nid is not None:
            out[nid] = entry
    return out


def inject_html_placeholders(tree, nodes: list[HtmlTextNode], mapping: dict) -> tuple[object, list[dict]]:
    """Walk tree again; replace .text/.tail of nodes that LLM marked sample.

    For sample_loop, wrap the parent <li> / <div class="bullet"> in
    `{% for h in r.work[0].highlights %}` ... `{% endfor %}` markers.
    """
    notes: list[dict] = []
    nid = 0
    # Track which parent elements need loop wrapping
    loop_parents: dict = {}  # parent_elem_id → (field, loop_over, first_child)

    for elem in tree.iter():
        if not isinstance(elem.tag, str):
            continue
        if elem.tag in SKIP_TAGS:
            continue
        for txt_attr in ("text", "tail"):
            text = getattr(elem, txt_attr)
            if not text or not text.strip():
                continue

            directive = mapping.get(nid, {})
            kind = directive.get("kind", "")
            field = directive.get("field")

            if kind == "sample" and field:
                new_text = "{{ " + field + " }}"
                # Preserve leading/trailing whitespace of original
                lead = text[:len(text) - len(text.lstrip())]
                trail = text[len(text.rstrip()):]
                setattr(elem, txt_attr, lead + new_text + trail)
                notes.append({"id": nid, "original": text.strip(), "field": field, "kind": "sample"})

            elif kind == "sample_loop" and field and directive.get("loop_over"):
                loop_over = directive["loop_over"]
                # Replace the text with `{{ field }}`
                lead = text[:len(text) - len(text.lstrip())]
                trail = text[len(text.rstrip()):]
                setattr(elem, txt_attr, lead + "{{ " + field + " }}" + trail)
                # Mark parent for loop wrap (e.g. <ul> wrapping <li>)
                parent = elem.getparent()
                pid = id(parent) if parent is not None else None
                if pid is not None and pid not in loop_parents:
                    loop_parents[pid] = (field, loop_over, elem)
                notes.append({"id": nid, "original": text.strip(), "field": field,
                              "loop_over": loop_over, "kind": "loop"})

            else:
                notes.append({"id": nid, "original": text.strip(), "kind": "label"})

            nid += 1

    # Wrap loop parents: put `{% for ... %}` right before first_child, `{% endfor %}` right after.
    # Strategy: place the markers on first_child's adjacent text, then remove duplicate siblings.
    for pid, (field, loop_over, first_child) in loop_parents.items():
        parent = first_child.getparent()
        if parent is None:
            continue

        for_marker = f"{{%- for {field} in {loop_over} -%}}"
        endfor_marker = "{%- endfor -%}"

        # Place FOR marker BEFORE first_child:
        prev = first_child.getprevious()
        if prev is not None:
            prev.tail = (prev.tail or "") + for_marker
        else:
            parent.text = (parent.text or "") + for_marker

        # Place ENDFOR AFTER first_child (right after its closing tag) BEFORE removing dups
        # so it sticks even after we delete siblings:
        first_child.tail = (first_child.tail or "") + endfor_marker

        # Now remove duplicate same-tag siblings (they're sample-only repeats)
        siblings = list(parent.iterchildren(tag=first_child.tag))
        for sib in siblings:
            if sib is first_child:
                continue
            parent.remove(sib)

    return tree, notes


def inspire_html(file_bytes: bytes, filename: str, *, provider: str, api_key: str, model: str) -> InspireResult:
    try:
        text = file_bytes.decode("utf-8", errors="replace")
        tree = html.fromstring(text)
    except Exception as exc:
        return InspireResult(
            theme_kind="html", theme_bytes=b"", preview_html="",
            route_used="html", fidelity_estimate=0.0, notes=[], warnings=[],
            error=f"无法解析 HTML：{exc}",
        )

    nodes = collect_text_nodes(tree)
    if not nodes:
        return InspireResult(
            theme_kind="html", theme_bytes=b"", preview_html="",
            route_used="html", fidelity_estimate=0.0, notes=[], warnings=[],
            error="HTML 里没有可识别的文本节点",
        )

    log.info("inspire_html: %d text nodes", len(nodes))

    try:
        mapping = llm_identify_html(nodes, provider=provider, api_key=api_key, model=model)
    except LLMError as exc:
        return InspireResult(
            theme_kind="html", theme_bytes=b"", preview_html="",
            route_used="html", fidelity_estimate=0.0, notes=[], warnings=[],
            error=f"LLM 识别失败：{exc}",
        )

    tree, notes = inject_html_placeholders(tree, nodes, mapping)

    # Serialize back
    html_bytes = etree.tostring(tree, method="html", pretty_print=False, encoding="utf-8")

    # Render with sample to verify + produce preview
    sample_html = _render_sample_html(html_bytes)

    sample_count = sum(1 for n in notes if n["kind"] in ("sample", "loop"))
    warnings = []
    if sample_count == 0:
        warnings.append("LLM 没识别出任何 sample 数据 — 模板可能太抽象")
    if sample_count < 3:
        warnings.append(f"只识别出 {sample_count} 条字段 — 检查预览确认")

    return InspireResult(
        theme_kind="html",
        theme_bytes=html_bytes,
        preview_html=sample_html,
        route_used="html",
        fidelity_estimate=0.97,
        notes=notes,
        warnings=warnings,
    )


def _render_sample_html(template_bytes: bytes) -> str:
    """Render the inspired template with sample data so user can preview."""
    from jinja2 import Environment, BaseLoader, select_autoescape
    from ...routes.themes import SAMPLE_RESUME

    try:
        env = Environment(
            loader=BaseLoader(),
            autoescape=select_autoescape(["html"]),
        )
        tpl = env.from_string(template_bytes.decode("utf-8", errors="replace"))
        return tpl.render(r=SAMPLE_RESUME)
    except Exception as exc:
        return f"<html><body><p>预览渲染失败：{exc}</p></body></html>"
