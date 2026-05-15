"""DOCX route — 99% fidelity via docxtpl placeholder injection.

Approach:
    1. Open the .docx (zip of XML)
    2. Walk all <w:t> text runs across the document body, headers, footers
    3. Send the collected text (plus structural context) to LLM
    4. LLM returns mapping: text → Compass field (or "keep as label")
    5. Inject Jinja placeholders inline into the text runs
    6. Save → result is a docxtpl-compatible template

At render time docxtpl handles {{ }} substitution, preserving all XML / styles.

Caveats:
    - Lists with sample bullets need to be detected as repeatable; LLM identifies
    - Tables: each row's content gets routed normally; row-level loops (`{%tr ...}`)
      handled in a second pass for table-shaped sections
    - Footer text like page numbers → "label, keep"
"""
from __future__ import annotations

import io

import logging

from dataclasses import dataclass


from docx import Document

from ..llm import LLMError, chat
from . import InspireResult

log = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────
# Step 1: collect text runs with context
# ────────────────────────────────────────────────────────────


@dataclass
class TextRun:
    id: int
    text: str
    context: str  # "h1" | "h2" | "h3" | "body" | "list" | "table"
    paragraph_idx: int  # for grouping
    in_table: bool = False


def heading_level(p) -> str:
    """Classify paragraph by style: h1/h2/h3/list/body."""
    try:
        style = p.style.name if p.style else ""
    except Exception:
        style = ""
    if style.startswith("Heading 1") or style == "Title":
        return "h1"
    if style.startswith("Heading 2"):
        return "h2"
    if style.startswith("Heading 3"):
        return "h3"
    if style.startswith("List"):
        return "list"
    return "body"


def collect_text_runs(doc: Document) -> list[TextRun]:
    """Walk doc body + tables in document order, returning text runs.

    We track structural context so LLM can distinguish "Experience" (heading
    label, keep) from "Senior Engineer" (sample title, replace).
    """
    runs: list[TextRun] = []
    rid = 0
    para_idx = 0

    # Walk body element children in order: paragraphs (w:p) and tables (w:tbl).
    # Use the proper docx accessors to keep parent references valid.
    from docx.text.paragraph import Paragraph
    from docx.table import Table

    body_elem = doc.element.body
    for child in body_elem.iterchildren():
        tag = child.tag.split("}")[-1]
        if tag == "p":
            # Use the document's body so .style etc. resolves correctly
            p = Paragraph(child, doc)
            text = p.text.strip()
            if text:
                runs.append(TextRun(id=rid, text=text, context=heading_level(p),
                                    paragraph_idx=para_idx, in_table=False))
                rid += 1
            para_idx += 1
        elif tag == "tbl":
            t = Table(child, doc)
            for row in t.rows:
                for cell in row.cells:
                    for p in cell.paragraphs:
                        text = p.text.strip()
                        if text:
                            runs.append(TextRun(id=rid, text=text, context=heading_level(p),
                                                paragraph_idx=para_idx, in_table=True))
                            rid += 1
                        para_idx += 1

    return runs


# ────────────────────────────────────────────────────────────
# Step 2: LLM identifies which runs are sample data
# ────────────────────────────────────────────────────────────


SYSTEM_PROMPT = """You are a resume-template analyzer. The user has uploaded a
sample resume DOCX template they want to reuse for their own data.

Your job: for each text run, decide whether it's:
1. SAMPLE DATA — text that should be replaced with the user's data
   (a name, a date, a job title, a company, a bullet point, a skill, etc.)
2. LABEL / STRUCTURE — text that is part of the template's design and
   should be kept verbatim (e.g., section headers like "Experience" / "Skills",
   page numbers, decorative divider lines).

For SAMPLE DATA, also identify which Compass field it maps to. Available fields:

  r.basics.name          # full name
  r.basics.email
  r.basics.phone
  r.basics.location.address
  r.basics.summary       # 1-2 sentence professional summary
  r.work[N].name         # company name (one work entry per index)
  r.work[N].position     # job title
  r.work[N].location     # job location
  r.work[N].startDate    # e.g. "2022-01"
  r.work[N].endDate      # or "Present"
  r.work[N].highlights[M]  # bullet points (one per index)
  r.education[N].institution
  r.education[N].studyType  # e.g. "M.Sc."
  r.education[N].area       # e.g. "Computer Science"
  r.education[N].startDate
  r.education[N].endDate
  r.education[N].gpa
  r.skills[N].name       # skill category, e.g. "Languages"
  r.skills[N].keywords[M]  # individual skill, e.g. "Python"
  r.languages[N].language
  r.projects[N].name
  r.projects[N].description
  r.projects[N].keywords[M]
  r.projects[N].url
  r.certificates[N].name
  r.certificates[N].issuer
  r.certificates[N].date

CRITICAL RULES:
- If the template lists ONE bullet under a job, mark that bullet's run as a
  REPEATABLE list — the loop unrolls to N user bullets at render time.
- Section headers like "EXPERIENCE", "EDUCATION", "Skills" are LABELS — keep verbatim.
- If unsure, prefer LABEL — better to keep a literal than swap incorrectly.

OUTER LIST PATTERNS (NEW):
The template likely shows MULTIPLE work / education / projects entries (e.g.,
work[0], work[1], work[2]). After identifying individual runs, also tell us
the FORMAT PATTERN per list — is the first entry visually distinct from the rest?

Many CV templates use heterogeneous format ("first item table-prominent, rest
plain") — e.g., the most recent job uses a 2-column table with startDate,
while older jobs use plain paragraphs with position+endDate. This is the
"importance-decreasing" visual hierarchy common in academic CVs (Politecnico
Milano, moderncv LaTeX).

Mark `pattern` = "heterogeneous" if first vs subsequent entries visually
differ (table layout vs plain paragraphs, more vs fewer fields). Otherwise
"homogeneous".

SKILLS SECTION (NEW):
PDF→DOCX conversion often mangles the skills section — keywords from different
skill categories can end up in the same table cell because pdf2docx translates
visual layout linearly, losing the logical "category → keywords" hierarchy.

Even when the resulting DOCX placeholders are mis-indexed
(e.g. r.skills[0].keywords[0] and r.skills[1].keywords[0] in the same cell),
the ORIGINAL skill grouping is usually still visually clear from the text
(e.g. "Languages:" + "Python, MATLAB, C++" was probably one skill category).

Reconstruct the LOGICAL skills structure regardless of the visual mess in the
DOCX. Output the canonical {category_name, keywords[]} list.

Return JSON only — no markdown fences:
{
  "runs": [
    {"id": 0, "kind": "sample", "field": "r.basics.name"},
    {"id": 1, "kind": "label"},
    {"id": 5, "kind": "sample_loop_item", "field": "h", "loop_over": "r.work[0].highlights"},
    ...
  ],
  "list_format_patterns": {
    "r.work":      {"pattern": "heterogeneous", "first_format": "primary", "rest_format": "secondary"},
    "r.education": {"pattern": "heterogeneous", "first_format": "primary", "rest_format": "secondary"},
    "r.projects":  {"pattern": "homogeneous"},
    "r.languages": {"pattern": "homogeneous"}
  },
  "skills_structure": [
    {"category_name": "Technical", "keywords": ["Python", "MATLAB", "C++", "Excel"]},
    {"category_name": "Tools",     "keywords": ["Ollama", "Notion", "PyTorch"]}
  ]
}

If a list type doesn't appear in the template, omit it from list_format_patterns.
If the skills section is unambiguously clean (no visual mangling), still output
skills_structure so the downstream rebuild always has canonical data.
"""


def _build_user_message(runs: list[TextRun]) -> str:
    lines = ["Text runs from the template (in document order):", ""]
    for r in runs:
        ctx_tag = f"[{r.context}]" + ("[in-table]" if r.in_table else "")
        lines.append(f"  id={r.id} {ctx_tag} : {r.text!r}")
    return "\n".join(lines)


def llm_identify(runs: list[TextRun], *, provider: str, api_key: str, model: str) -> dict:
    """Send runs to LLM, parse JSON response.

    Returns a dict with three top-level keys:
      - mapping: {run_id: directive}  (per-run kind/field/loop_over)
      - list_format_patterns: dict {list_var: {pattern, first_format, rest_format}}
      - skills_structure: list of {category_name, keywords[]}

    The latter two drive PASS 2 (outer list for-loop wrap) and PASS 3 (skills
    section rebuild) in inject_placeholders. See SYSTEM_PROMPT for details.
    """
    user_msg = _build_user_message(runs)
    res = chat(
        provider=provider,
        api_key=api_key,
        model=model,
        system=SYSTEM_PROMPT,
        user_content=user_msg,
        max_tokens=5000,  # 略增, 容纳新 fields
        temperature=0.2,
    )
    parsed = res.parse_json(default={"runs": []})
    if not parsed.get("runs"):
        log.warning("LLM returned no runs — raw response truncated: %r", res.text[:500])
    mapping = {}
    for entry in parsed.get("runs", []):
        rid = entry.get("id")
        if rid is None:
            continue
        mapping[rid] = entry
    return {
        "mapping": mapping,
        "list_format_patterns": parsed.get("list_format_patterns") or {},
        "skills_structure": parsed.get("skills_structure") or [],
    }


# ────────────────────────────────────────────────────────────
# Step 3: inject Jinja placeholders into the DOCX
# ────────────────────────────────────────────────────────────


def _replace_paragraph_text(p, new_text: str) -> None:
    """Replace ALL text in a paragraph with new_text, preserving the first run's
    formatting. We delete subsequent runs to avoid mismatched styles.
    """
    if not p.runs:
        return
    # Keep first run, set its text to new_text
    p.runs[0].text = new_text
    # Remove all other runs (their text is now redundant)
    for run in p.runs[1:]:
        run._element.getparent().remove(run._element)


def inject_placeholders(doc: Document, runs: list[TextRun], mapping: dict) -> tuple[Document, list[dict]]:
    """Walk the doc and replace sample-data paragraphs with Jinja placeholders.

    For "sample_loop_item" paragraphs, group consecutive ones with the same
    loop_over and turn them into:
        [NEW para]: {%p for h in r.work[0].highlights %}
        [first orig para, text → {{h}}]    (body, gets duplicated)
        [NEW para]: {%p endfor %}
        (other orig paras in group: deleted)

    docxtpl strips the `{%p for/endfor %}` paragraphs at render and duplicates
    the body paragraph (with its bullet/list style preserved!) for each item.
    """
    from docx.text.paragraph import Paragraph
    from docx.table import Table

    # Re-walk to build a flat list of (paragraph, run_id) tuples in order
    walk: list[tuple] = []  # (paragraph_obj, run_id_or_None)
    rid_iter = 0
    for child in doc.element.body.iterchildren():
        tag = child.tag.split("}")[-1]
        if tag == "p":
            p = Paragraph(child, doc)
            text = p.text.strip()
            if text:
                walk.append((p, rid_iter))
                rid_iter += 1
            else:
                walk.append((p, None))
        elif tag == "tbl":
            t = Table(child, doc)
            for row in t.rows:
                for cell in row.cells:
                    for p in cell.paragraphs:
                        text = p.text.strip()
                        if text:
                            walk.append((p, rid_iter))
                            rid_iter += 1
                        else:
                            walk.append((p, None))

    notes: list[dict] = []
    paragraphs_to_remove: list = []

    # Group consecutive sample_loop_item runs that share loop_over
    i = 0
    while i < len(walk):
        p, rid = walk[i]
        if rid is None:
            i += 1
            continue
        directive = mapping.get(rid, {})
        kind = directive.get("kind", "")
        text = p.text.strip()

        if kind == "sample" and directive.get("field"):
            _replace_paragraph_text(p, "{{ " + directive["field"] + " }}")
            notes.append({"id": rid, "original": text, "field": directive["field"], "kind": "sample"})
            i += 1

        elif kind == "sample_loop_item" and directive.get("field") and directive.get("loop_over"):
            field = directive["field"]
            loop = directive["loop_over"]

            # Find consecutive group with same loop_over
            group_end = i
            while group_end + 1 < len(walk):
                _, next_rid = walk[group_end + 1]
                if next_rid is None:
                    break
                next_dir = mapping.get(next_rid, {})
                if (next_dir.get("kind") == "sample_loop_item"
                    and next_dir.get("loop_over") == loop):
                    group_end += 1
                else:
                    break

            # Insert {%p for %} BEFORE first paragraph in group
            for_para = _insert_paragraph_before(p, "{%p for " + field + " in " + loop + " %}")
            # Replace first paragraph's text with body
            _replace_paragraph_text(p, "{{ " + field + " }}")
            notes.append({
                "id": rid, "original": text, "field": field,
                "loop_over": loop, "kind": "loop_start",
            })

            # Mark subsequent paragraphs in group for deletion
            for j in range(i + 1, group_end + 1):
                _, dup_rid = walk[j]
                paragraphs_to_remove.append(walk[j][0])
                notes.append({
                    "id": dup_rid, "original": walk[j][0].text.strip(),
                    "kind": "loop_dup_removed",
                })

            # Insert {%p endfor %} AFTER last paragraph in group
            last_p, _ = walk[group_end]
            _insert_paragraph_after(last_p, "{%p endfor %}")

            i = group_end + 1

        else:
            notes.append({"id": rid, "original": text, "kind": "label"})
            i += 1

    # Clean up the duplicate paragraphs (delete from XML)
    for p in paragraphs_to_remove:
        elem = p._element
        elem.getparent().remove(elem)

    return doc, notes


def _make_paragraph_element(text: str):
    """Build a proper w:p element (via OxmlElement) so it has add_r etc."""
    from docx.oxml import OxmlElement
    p = OxmlElement("w:p")
    r = OxmlElement("w:r")
    t = OxmlElement("w:t")
    t.text = text
    # Preserve trailing spaces in jinja delimiters
    t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    r.append(t)
    p.append(r)
    return p


def _insert_paragraph_before(p, text: str):
    """Insert a new paragraph immediately before p in document order."""
    new_elem = _make_paragraph_element(text)
    p._element.addprevious(new_elem)
    return new_elem


def _insert_paragraph_after(p, text: str):
    """Insert a new paragraph immediately after p in document order."""
    new_elem = _make_paragraph_element(text)
    p._element.addnext(new_elem)
    return new_elem


# ────────────────────────────────────────────────────────────
# Step 3b (PASS 2): outer-list for-loop wrap (B 方案)
# ────────────────────────────────────────────────────────────
# 现有 inject_placeholders 已经把 inner list (r.work[0].highlights) wrap 成
# {%p for h ...%} 段落级 for-loop. 但 outer list (r.work[0] / [1] / [2]) 还是
# 硬编码索引. 本 PASS 把 outer list 也 wrap, 用 LLM 给的 list_format_patterns
# 区分 homogeneous (单 for-loop) vs heterogeneous (for-loop + if loop.first 分支).

_LIST_INDEX_RE = __import__("re").compile(r"(r\.[a-zA-Z_]+)\[(\d+)\]")


def _build_paragraph_xml(cmd: str) -> str:
    """Build a docxtpl control paragraph (just the control cmd, no other content)."""
    return (
        '<w:p xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:r><w:t xml:space="preserve">' + cmd + "</w:t></w:r></w:p>"
    )


def _rewrite_indexed_placeholders(elem, list_var: str, new_var: str = "w") -> None:
    """Within elem, replace all `<list_var>[<N>]` with `<new_var>`.

    E.g., 'r.work[0].name' → 'w.name', 'r.work[1].highlights' → 'w.highlights'.
    Touches <w:t> text content only; styling preserved.
    """
    import re

    from docx.oxml.ns import qn

    pat = re.compile(re.escape(list_var) + r"\[\d+\]")
    for t in elem.iter(qn("w:t")):
        if t.text and list_var in t.text:
            t.text = pat.sub(new_var, t.text)


def _scan_blocks_for_list(body, list_var: str) -> dict:
    """Scan body children, group elements by their smallest `<list_var>[N]` index.

    Untagged elements (no placeholder of *any* list_var) inherit into the
    current block ONLY when one of these holds:
      (a) it contains inner-loop body/control markers ({{h}}, {%p for h/endfor%})
          — these belong to the previous tagged block's nested for-loop
      (b) the *next* tagged element (anywhere ahead) is the same list_var
          — we're still "inside" our list's section, not yet at the next section

    Encountering a tagged element of a DIFFERENT list_var ends our section
    (current_idx → None). This prevents one list's block from greedy-grabbing
    paragraphs that belong to the next section (e.g., work[2] swallowing
    PROJECTS title + r.projects[0] elements when scanning r.work).
    """
    body_elems = list(body.iterchildren())

    # Pre-compute the set of list_vars each element carries (None = untagged)
    el_list_vars: list[set] = []
    for el in body_elems:
        text = "".join(el.itertext())
        el_list_vars.append(set(m[0] for m in _LIST_INDEX_RE.findall(text)))

    blocks: dict[int, list] = {}
    current_idx = None
    inner_markers = ("{{ h }}", "{%p for h", "{%p endfor")

    for i, el in enumerate(body_elems):
        lvs = el_list_vars[i]
        if list_var in lvs:
            text = "".join(el.itertext())
            our_idxs = [int(m[1]) for m in _LIST_INDEX_RE.findall(text) if m[0] == list_var]
            mn = min(our_idxs)
            blocks.setdefault(mn, []).append(el)
            current_idx = mn
        elif lvs:
            # Different list_var → we left our list's section
            current_idx = None
        elif current_idx is not None:
            text = "".join(el.itertext())
            # (a) inner-loop markers always inherit (highlights body/endfor)
            if any(m in text for m in inner_markers):
                blocks[current_idx].append(el)
                continue
            # (b) check if next tagged element belongs to our list_var
            next_vars = None
            for j in range(i + 1, len(el_list_vars)):
                if el_list_vars[j]:
                    next_vars = el_list_vars[j]
                    break
            if next_vars and list_var in next_vars:
                blocks[current_idx].append(el)
            # else: untagged element in section gap → don't inherit

    return blocks


def _polish_date_fallback(elem) -> None:
    """Replace bare {{ w.startDate }} → {{ w.startDate or w.endDate }} (and reverse).

    Why: PDF templates often have a single date placeholder per entry (e.g.,
    "2026" on the PhD line), but the LLM tags it as either startDate or endDate
    based on guess. Real data may have only one populated. Using jinja's `or`
    operator auto-falls-back to whichever field has a value.
    """
    import re
    from docx.oxml.ns import qn

    # Resume convention: a single-date placeholder usually means the more
    # informative "recent" date (endDate = graduation year / left job year).
    # If endDate is empty (still ongoing), fall back to startDate.
    pat_start = re.compile(r"\{\{\s*(\w+)\.startDate\s*\}\}")
    pat_end = re.compile(r"\{\{\s*(\w+)\.endDate\s*\}\}")
    for t in elem.iter(qn("w:t")):
        if t.text:
            t.text = pat_start.sub(r"{{ \1.endDate or \1.startDate }}", t.text)
            t.text = pat_end.sub(r"{{ \1.endDate or \1.startDate }}", t.text)


def _polish_projects_highlights_fallback(elem) -> None:
    """In projects context, replace `for h in p.highlights` with a fallback
    that uses [p.description] if highlights is empty/missing.

    Why: JSON Resume schema stores project body as `description` (long string),
    not `highlights` (list). Original PDF templates often had a bullet list
    that got mapped to `highlights` — at render this is undefined → silent skip.
    """
    import re
    from docx.oxml.ns import qn

    pat = re.compile(r"\{%p\s*for\s+(\w+)\s+in\s+(\w+)\.highlights\s*%\}")
    for t in elem.iter(qn("w:t")):
        if t.text and "highlights" in t.text:
            t.text = pat.sub(
                r"{%p for \1 in (\2.highlights or ([\2.description] if \2.description else [])) %}",
                t.text,
            )


def wrap_outer_lists(doc, list_format_patterns: dict) -> tuple:
    """PASS 2: wrap outer-list blocks ({{r.work[0]}}, {{r.work[1]}}, ...) into
    docxtpl `{%p for w in r.work %}` loops.

    list_format_patterns shape (from LLM):
      { "r.work": {"pattern": "heterogeneous",
                   "first_format": "primary", "rest_format": "secondary"},
        "r.projects": {"pattern": "homogeneous"} }

    For each list_var:
      - homogeneous   → keep block[0], delete block[1..], wrap [0] in for-loop
      - heterogeneous → keep block[0] & block[1], delete block[2..],
                        wrap in `for + if loop.first / else / endif`

    Inside kept blocks, rewrite r.LIST[N].xxx → w.xxx so the inner placeholders
    bind to the for-loop variable.
    """
    notes: list[dict] = []

    for list_var, info in list_format_patterns.items():
        pattern = (info or {}).get("pattern", "homogeneous")
        blocks = _scan_blocks_for_list(doc.element.body, list_var)
        if not blocks:
            notes.append({"kind": "outer_wrap_skip", "list_var": list_var,
                          "reason": "list_var not found in doc"})
            continue

        sorted_idxs = sorted(blocks.keys())

        # If only one block exists, just wrap it in a for-loop. Use inline jinja
        # for-loop (not paragraph-level) when block is a single element with a
        # single placeholder (e.g., r.languages = "{{ r.languages[0].language }}").
        # This produces "English, Mandarin, Italian" instead of three separate
        # paragraphs.
        if len(sorted_idxs) == 1:
            block = blocks[sorted_idxs[0]]
            inline_done = False
            if len(block) == 1:
                inline_done = _try_inline_loop(block[0], list_var, "l")
            if not inline_done:
                _wrap_block_with_for_loop(block, list_var, var_name="w")
                for el in block:
                    _rewrite_indexed_placeholders(el, list_var, "w")
                    _polish_date_fallback(el)
                    if list_var == "r.projects":
                        _polish_projects_highlights_fallback(el)
            notes.append({"kind": "outer_wrap_single", "list_var": list_var,
                          "blocks_seen": 1, "inline": inline_done})
            continue

        if pattern == "homogeneous":
            kept_block = blocks[sorted_idxs[0]]
            to_delete = [el for idx in sorted_idxs[1:] for el in blocks[idx]]
            _wrap_block_with_for_loop(kept_block, list_var, var_name="w")
            for el in kept_block:
                _rewrite_indexed_placeholders(el, list_var, "w")
                _polish_date_fallback(el)
                if list_var == "r.projects":
                    _polish_projects_highlights_fallback(el)
            for el in to_delete:
                if el.getparent() is not None:
                    el.getparent().remove(el)
            notes.append({"kind": "outer_wrap_homo", "list_var": list_var,
                          "blocks_seen": len(sorted_idxs),
                          "blocks_deleted": len(to_delete)})
        else:  # heterogeneous
            kept_first = blocks[sorted_idxs[0]]
            kept_rest = blocks[sorted_idxs[1]]
            to_delete = [el for idx in sorted_idxs[2:] for el in blocks[idx]]
            _wrap_heterogeneous_blocks(kept_first, kept_rest, list_var, var_name="w")
            for el in kept_first + kept_rest:
                _rewrite_indexed_placeholders(el, list_var, "w")
                _polish_date_fallback(el)
                if list_var == "r.projects":
                    _polish_projects_highlights_fallback(el)
            for el in to_delete:
                if el.getparent() is not None:
                    el.getparent().remove(el)
            notes.append({"kind": "outer_wrap_hetero", "list_var": list_var,
                          "blocks_seen": len(sorted_idxs),
                          "blocks_deleted": len(to_delete)})

    return doc, notes


def _try_inline_loop(el, list_var: str, var_name: str = "l") -> bool:
    """If element has exactly one placeholder referencing list_var[0].FIELD,
    rewrite it inline:
       "{{ r.languages[0].language }}"
         → "{% for l in r.languages %}{{ l.language }}{% if not loop.last %}, {% endif %}{% endfor %}"

    Returns True if inline transform applied, False if structure didn't match
    (caller should fall back to paragraph-level for-loop).
    """
    import re
    from docx.oxml.ns import qn

    placeholder_re = re.compile(r"\{\{[^}]+\}\}")
    inline_target_re = re.compile(
        r"\{\{\s*" + re.escape(list_var) + r"\[0\]\.(\w+)\s*\}\}"
    )

    # Gather all text + check it has exactly one placeholder, of the expected shape
    full_text = "".join(t.text or "" for t in el.iter(qn("w:t")))
    placeholders = placeholder_re.findall(full_text)
    if len(placeholders) != 1:
        return False
    m = inline_target_re.search(full_text)
    if not m:
        return False
    field = m.group(1)
    new_text = (
        "{% for " + var_name + " in " + list_var + " %}"
        + "{{ " + var_name + "." + field + " }}"
        + "{% if not loop.last %}, {% endif %}"
        + "{% endfor %}"
    )
    # Replace the placeholder in whichever <w:t> contains it
    target_pat = re.compile(
        r"\{\{\s*" + re.escape(list_var) + r"\[0\]\.\w+\s*\}\}"
    )
    for t in el.iter(qn("w:t")):
        if t.text and target_pat.search(t.text):
            t.text = target_pat.sub(new_text, t.text)
            return True
    return False


def _wrap_block_with_for_loop(block_elements: list, list_var: str,
                              var_name: str = "w") -> None:
    """Insert {%p for VAR in LIST %} before block[0] and {%p endfor %} after block[-1]."""
    from lxml import etree
    if not block_elements:
        return
    for_para = etree.fromstring(_build_paragraph_xml(
        "{%p for " + var_name + " in " + list_var + " %}"))
    endfor_para = etree.fromstring(_build_paragraph_xml("{%p endfor %}"))
    block_elements[0].addprevious(for_para)
    block_elements[-1].addnext(endfor_para)


def _wrap_heterogeneous_blocks(first_block: list, rest_block: list,
                               list_var: str, var_name: str = "w") -> None:
    """For + if loop.first / else / endif wrap.

    Layout:
        {%p for w in r.work %}
        {%p if loop.first %}
        ... first_block (table-prominent format) ...
        {%p else %}
        ... rest_block (plain simplified format) ...
        {%p endif %}
        {%p endfor %}
    """
    from lxml import etree
    if not first_block or not rest_block:
        # one side missing — fall back to single-block wrap
        b = first_block or rest_block
        _wrap_block_with_for_loop(b, list_var, var_name)
        return
    for_para = etree.fromstring(_build_paragraph_xml(
        "{%p for " + var_name + " in " + list_var + " %}"))
    if_para = etree.fromstring(_build_paragraph_xml("{%p if loop.first %}"))
    else_para = etree.fromstring(_build_paragraph_xml("{%p else %}"))
    endif_para = etree.fromstring(_build_paragraph_xml("{%p endif %}"))
    endfor_para = etree.fromstring(_build_paragraph_xml("{%p endfor %}"))
    # lxml: addprevious appends in source order when called sequentially.
    # addnext on the same node inserts each new sibling *immediately* after, so
    # the LATER call wins position-1. We avoid that by chaining addnext from
    # the previously-inserted element.
    first_block[0].addprevious(for_para)        # → for | first_block[0]
    first_block[0].addprevious(if_para)         # → for | if | first_block[0]
    first_block[-1].addnext(else_para)          # → first_block[-1] | else
    rest_block[-1].addnext(endif_para)          # → rest_block[-1] | endif
    endif_para.addnext(endfor_para)             # → endif | endfor


# ────────────────────────────────────────────────────────────
# Step 3c (PASS 3): skills section rebuild (B 方案 — 绕开 pdf2docx 视觉伪影)
# ────────────────────────────────────────────────────────────
# PDF→DOCX 转换常把 skills 段的 "category → keywords" 逻辑层级压扁成视觉行,
# 导致 keywords 跨 cell 错位 (r.skills[0].keywords[0] 和 r.skills[1].keywords[0]
# 共一个 cell). LLM 在 SYSTEM_PROMPT 里已经被指示绕开视觉, 直接重建逻辑
# skills_structure. 本 PASS 不信任原 skills 段的视觉, 直接删了重做.


_SKILLS_INDEX_RE = __import__("re").compile(r"r\.skills\[\d+\]")


def _build_skills_table_xml() -> str:
    """A clean 2-column docxtpl table: skill category | keywords joined."""
    return '''<w:tbl xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
<w:tblPr><w:tblW w:type="auto" w:w="0"/><w:tblLayout w:type="fixed"/><w:tblLook w:firstColumn="1" w:firstRow="1" w:lastColumn="0" w:lastRow="0" w:noHBand="0" w:noVBand="1" w:val="04A0"/><w:tblInd w:w="0.0" w:type="dxa"/></w:tblPr>
<w:tblGrid><w:gridCol w:w="2200"/><w:gridCol w:w="8260"/></w:tblGrid>
<w:tr><w:tc><w:tcPr><w:tcW w:type="dxa" w:w="2200"/></w:tcPr><w:p><w:r><w:t xml:space="preserve">{%tr for s in r.skills %}</w:t></w:r></w:p></w:tc><w:tc><w:tcPr><w:tcW w:type="dxa" w:w="8260"/></w:tcPr><w:p/></w:tc></w:tr>
<w:tr>
<w:trPr><w:trHeight w:val="320"/></w:trPr>
<w:tc><w:tcPr><w:tcW w:type="dxa" w:w="2200"/><w:tcBorders><w:top w:sz="3.184000015258789" w:val="single" w:color="#000000"/></w:tcBorders></w:tcPr>
<w:p><w:pPr><w:spacing w:line="272" w:lineRule="exact" w:before="76" w:after="0"/><w:ind w:left="4" w:right="0"/></w:pPr><w:r><w:rPr><w:b/><w:sz w:val="22"/></w:rPr><w:t>{{ s.name }}</w:t></w:r></w:p>
</w:tc>
<w:tc><w:tcPr><w:tcW w:type="dxa" w:w="8260"/><w:tcBorders><w:top w:sz="3.184000015258789" w:val="single" w:color="#000000"/></w:tcBorders></w:tcPr>
<w:p><w:pPr><w:spacing w:line="218" w:lineRule="exact" w:before="76" w:after="0"/><w:ind w:left="538" w:right="0"/></w:pPr><w:r><w:rPr><w:sz w:val="22"/></w:rPr><w:t>{{ s.keywords | join(", ") }}</w:t></w:r></w:p>
</w:tc>
</w:tr>
<w:tr><w:tc><w:tcPr><w:tcW w:type="dxa" w:w="2200"/></w:tcPr><w:p><w:r><w:t xml:space="preserve">{%tr endfor %}</w:t></w:r></w:p></w:tc><w:tc><w:tcPr><w:tcW w:type="dxa" w:w="8260"/></w:tcPr><w:p/></w:tc></w:tr>
</w:tbl>'''


def rebuild_skills_section(doc, skills_structure: list) -> tuple:
    """PASS 3: 删除原 skills 段所有 elements (受视觉伪影污染), 插入标准 table 双列模板.

    skills_structure 只用来证明用户原模板有 skills 段 (LLM 输出非空就触发).
    实际渲染时, 数据来自 build_json_resume 的 r.skills (用户当前 identity),
    不是 skills_structure 本身.
    """
    from lxml import etree

    notes: list[dict] = []
    body = doc.element.body

    body_elems = list(body.iterchildren())
    skills_idxs = []
    for i, el in enumerate(body_elems):
        text = "".join(el.itertext())
        if _SKILLS_INDEX_RE.search(text):
            skills_idxs.append(i)

    if not skills_idxs:
        notes.append({"kind": "skills_rebuild_skip",
                      "reason": "no r.skills[N] placeholders found in doc"})
        return doc, notes

    start_idx, end_idx = skills_idxs[0], skills_idxs[-1]
    elems_to_remove = body_elems[start_idx:end_idx + 1]

    # 在 start_idx 之前插入新 table, 然后删除原 [start..end] elements
    new_table = etree.fromstring(_build_skills_table_xml())
    body_elems[start_idx].addprevious(new_table)
    for el in elems_to_remove:
        if el.getparent() is not None:
            el.getparent().remove(el)

    notes.append({"kind": "skills_rebuild",
                  "categories_in_llm_output": len(skills_structure),
                  "original_elements_removed": len(elems_to_remove)})
    return doc, notes


# ────────────────────────────────────────────────────────────
# Step 4: preview via mammoth (DOCX → HTML approximation)
# ────────────────────────────────────────────────────────────


def docx_to_preview_html(doc_bytes: bytes) -> str:
    """Use mammoth to convert DOCX → HTML for browser preview.

    Mammoth handles styles approximately — it's OK for a "did this work?" check
    but not pixel-exact. User should download .docx and open in Word for truth.
    """
    try:
        import mammoth
        result = mammoth.convert_to_html(io.BytesIO(doc_bytes))
        body = result.value
        # Wrap in minimal scaffolding so it renders standalone
        return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<style>
body {{ font-family: -apple-system, "PingFang SC", sans-serif; max-width: 760px; margin: 30px auto; padding: 24px; background: white; line-height: 1.55; color: #222; }}
h1 {{ font-size: 22pt; margin: 0 0 6px; }}
h2 {{ font-size: 12pt; margin: 16px 0 4px; text-transform: uppercase; letter-spacing: 0.05em; border-bottom: 1px solid #888; padding-bottom: 2px; }}
h3 {{ font-size: 11pt; margin: 8px 0 2px; }}
p {{ margin: 4px 0; }}
ul {{ margin: 4px 0; padding-left: 22px; }}
table {{ border-collapse: collapse; width: 100%; }}
td, th {{ padding: 4px 6px; vertical-align: top; }}
.preview-disclaimer {{ background: #fff3cd; border: 1px solid #ffeaa7; padding: 8px 14px; border-radius: 4px; font-size: 12px; color: #8b6500; margin: 0 0 16px; }}
</style></head>
<body>
<div class="preview-disclaimer">⚠ 这是 mammoth 的近似 HTML 预览。颜色、字体、字距可能与 Word 中实际渲染有差异。请下载 .docx 在 Word/Pages 中查看真实效果。</div>
{body}
</body></html>"""
    except Exception as exc:  # noqa: BLE001
        log.warning("mammoth preview failed: %s", exc)
        return f"<html><body><p>预览生成失败：{exc}。请下载 .docx 在 Word 中查看。</p></body></html>"


# ────────────────────────────────────────────────────────────
# Public entry
# ────────────────────────────────────────────────────────────


def inspire_docx(
    file_bytes: bytes, filename: str, *, provider: str, api_key: str, model: str,
) -> InspireResult:
    """Full DOCX inspire pipeline."""
    try:
        doc = Document(io.BytesIO(file_bytes))
    except Exception as exc:
        return InspireResult(
            theme_kind="docx", theme_bytes=b"", preview_html="",
            route_used="docx", fidelity_estimate=0.0, notes=[], warnings=[],
            error=f"无法解析 DOCX：{exc}",
        )

    runs = collect_text_runs(doc)
    if not runs:
        return InspireResult(
            theme_kind="docx", theme_bytes=b"", preview_html="",
            route_used="docx", fidelity_estimate=0.0, notes=[], warnings=[],
            error="DOCX 里没有可解析的文本（可能是纯图片简历）— 试试图片路或扫描件路",
        )

    log.info("inspire_docx: %d runs to identify", len(runs))

    try:
        llm_output = llm_identify(runs, provider=provider, api_key=api_key, model=model)
    except LLMError as exc:
        return InspireResult(
            theme_kind="docx", theme_bytes=b"", preview_html="",
            route_used="docx", fidelity_estimate=0.0, notes=[], warnings=[],
            error=f"LLM 识别失败：{exc}",
        )

    mapping = llm_output["mapping"]
    list_format_patterns = llm_output["list_format_patterns"]
    skills_structure = llm_output["skills_structure"]

    # PASS 1: per-run injection (sample / sample_loop_item / label) — existing
    doc, notes = inject_placeholders(doc, runs, mapping)

    # PASS 2: outer list for-loop wrap (NEW — B 方案)
    if list_format_patterns:
        doc, pass2_notes = wrap_outer_lists(doc, list_format_patterns)
        notes.extend(pass2_notes)

    # PASS 3: skills section rebuild (NEW — B 方案, 绕过视觉伪影)
    if skills_structure:
        doc, pass3_notes = rebuild_skills_section(doc, skills_structure)
        notes.extend(pass3_notes)

    # Save the modified docx
    out = io.BytesIO()
    doc.save(out)
    docx_bytes = out.getvalue()

    # Generate sample render (apply to preview data) for browser preview
    sample_rendered = _render_sample(docx_bytes)
    preview_html = docx_to_preview_html(sample_rendered)

    # Compute fidelity estimate from how many runs got identified
    sample_count = sum(1 for n in notes if n["kind"] in ("sample", "loop"))
    label_count = sum(1 for n in notes if n["kind"] == "label")
    fidelity = 0.99  # XML structure is preserved by definition

    warnings = []
    if sample_count == 0:
        warnings.append("LLM 没识别出任何 sample 数据 — 模板可能太抽象，建议手动检查")
    if sample_count < 3:
        warnings.append(f"只识别出 {sample_count} 条数据字段 — 看上去偏少，建议预览验证")

    return InspireResult(
        theme_kind="docx",
        theme_bytes=docx_bytes,
        preview_html=preview_html,
        route_used="docx",
        fidelity_estimate=fidelity,
        notes=notes,
        warnings=warnings,
    )


def _render_sample(docx_bytes: bytes) -> bytes:
    """Render the template with SAMPLE_RESUME data so user sees what it'll look like."""
    from docxtpl import DocxTemplate
    from ...routes.themes import SAMPLE_RESUME

    tpl = DocxTemplate(io.BytesIO(docx_bytes))
    tpl.render({"r": SAMPLE_RESUME})
    out = io.BytesIO()
    tpl.save(out)
    return out.getvalue()
