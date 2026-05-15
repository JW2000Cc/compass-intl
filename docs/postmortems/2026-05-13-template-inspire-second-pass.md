# 2026-05-13: template_inspire 缺失的 second pass — 硬编码 [N] 索引模板永远渲染失败

> 用户报"导出 Word 没按钮"。深挖发现: docx_route.py 注释承认"second pass for
> repeatable lists handled" 但实际没实现。所有 PDF→DOCX inspire 产出的模板都
> 是 r.work[0]/[1]/[2] 硬编码索引, 任何"work entries 数量比模板假设少"的简历
> 渲染时 jinja UndefinedError 直接炸。

---

**Symptom**（用户看到的）:
- 进入 Compass `/resume/workbench` → 选 attempt → review 页 → 看不到 .docx 导出按钮
- 实际 user 的 .docx 模板被错登记为 `kind="html"` (templates.json), 所以 docx 引擎下的按钮 (`x-show="engine === 'docx'"`) 不显示
- 即使强制注册成 `kind="docx"`, 渲染立刻 raise: `jinja2.UndefinedError: list object has no element 1` (模板有 `r.work[2]` 但当前简历只有 1 个 work entry)

**Detection**（怎么发现的）:
- 用户主动反馈"Compass 简历不支持导出 Word/PDF, 字号字体小问题"
- 调研发现: docx 导出按钮**有条件**显示 (engine=='docx'), 用户模板 kind 是 html
- 改 templates.json kind 后渲染失败, 暴露真正问题: 模板硬编码 [N] 索引
- 读 docx_route.py 模块 docstring 发现"second pass for table-shaped sections" 写在注释里但代码未实现

**Root cause**（实际技术原因）:
template_inspire docx_route.py 处理 PDF→DOCX 转换的 inspire 流程时:
- Step 1 (collect_text_runs) → 走通
- Step 2 (llm_identify) → 走通, 给每个 text run 注 placeholder
- Step 3 (inject_placeholders) → 只做 inner-list for-loop wrap (`r.work[0].highlights`), **outer list (`r.work[0]/[1]/[2]` 这种 sibling 关系) 不 wrap**

设计文档 (module docstring) 说"Lists with sample bullets need to be detected
as repeatable; LLM identifies. Tables: each row's content gets routed normally;
row-level loops handled in a second pass for table-shaped sections" — 但代码
里没有这个 second pass.

另外, skills 段在 PDF→DOCX 转换时常有视觉伪影 — pdf2docx 把"Languages: Python,
MATLAB"这种视觉行 1:1 复刻到 DOCX, 丢失"Languages 是 skill 类别, Python 是 keyword"
的逻辑层级, 导致 placeholder 跨 cell 错位 (`r.skills[0].keywords[0]` 和
`r.skills[1].keywords[0]` 同一 cell). 这是 docx_route 完全没处理的另一坑.

**Pattern class**: 新 pattern — P-XXX 候选 "AI 服务的承诺与现实"
(LLM/ML 服务在 docstring 里承诺的 second-pass 行为, 实际代码里没实现 —
跟 P-008 "wizard 实现完整不代表用户走得到" 同类: design 完整不代表实施完整).

**Fix**:
- `compass/services/template_inspire/docx_route.py:114-208`: 升级 SYSTEM_PROMPT 让 LLM 在同一次调用里多吐两类信息:
  - `list_format_patterns`: 每个 list_var (r.work / r.education / r.projects) 的 pattern (homogeneous = 所有 entry 同 format / heterogeneous = 第一项 table 突出 + 其余 plain 简化)
  - `skills_structure`: skills 段的逻辑层级 ({category_name, keywords[]}), 绕过 PDF→DOCX 视觉伪影
- `compass/services/template_inspire/docx_route.py:222-260`: `llm_identify` 返回扩展 dict `{mapping, list_format_patterns, skills_structure}` 而非裸 mapping
- `compass/services/template_inspire/docx_route.py:430+`: 加 PASS 2 `wrap_outer_lists()` — 用 `_scan_blocks_for_list` 按 placeholder 索引 N 分组 element, 对 homogeneous 单 `{%p for %}` wrap, 对 heterogeneous 加 `{%p for w %}{%p if loop.first %}[0 block]{%p else %}[1 block]{%p endif %}{%p endfor %}`
- `compass/services/template_inspire/docx_route.py:570+`: 加 PASS 3 `rebuild_skills_section()` — 删除原 skills 段所有 elements (视觉伪影污染), 插入标准 table 双列模板 `{%tr for s in r.skills %}{{ s.name }} | {{ s.keywords | join(", ") }}{%tr endfor %}`
- `compass/services/template_inspire/docx_route.py:540+`: 加 3 处 polish:
  - `_polish_date_fallback`: 单日期字段 `{{ w.startDate }}` 改成 `{{ w.endDate or w.startDate }}` — endDate 优先 (简历惯例)
  - `_polish_projects_highlights_fallback`: schema 不一致 fallback, 把 `for h in p.highlights` 改成 `for h in (p.highlights or ([p.description] if p.description else []))`
  - `_try_inline_loop`: simple-string list (如 r.languages 只 1 个 placeholder) 用 inline jinja for-loop 而非 paragraph-level, 产出 "English, Mandarin, Italian" 逗号 inline 而非 3 段

**Sibling propagation scan**（横向）:
- ✅ Compass: 本 fix 仅触及 Compass.template_inspire, 已 sync 到 Compass_template/
- ❓ Sundial: 没 template_inspire 服务, 无此 pattern
- ❓ Visa Sniper: 没 PDF→DOCX 流程, 无此 pattern
- 通用 pattern (注释承诺 vs 代码现实): 全程序应在 release_check 里加 grep `"TODO\|second pass\|未实现\|placeholder"` 在 docstring 里, 命中即警告

**Prevent recurrence**（防再犯）:
- ✅ 实施了完整 PASS 1 + 2 + 3 流水线
- ✅ docstring 更新, 不再有"承诺但未实现"的注释
- ⏳ 单元测试 (T28 deferred) — 现在用 zhenghao 模板回归测试 100% 一致, 等用户传别的简历再补 case-based test
- ✅ docs/decisions/0019-... ADR 留档说明设计选择 (B vs C, LLM-driven vs rule-driven)
- ⏳ release_check.sh 应加 grep 检测"docstring 承诺 vs 代码现实"反 pattern

**Time spent**: 调研 (current state + 业界) 1.5h / 设计 prompt + algorithm 1h / 实施 (PASS 2 + 3 + polishes) 2h / 测试 + 调试 (scan inherit logic 等 edge case) 1.5h = 总 ~6 小时

**Lesson**（一句话）:
**docstring 承诺 "second pass handled" 但代码里 grep 不到对应实现就是反 pattern**.
未来 design doc / 注释要么写"实现了什么", 要么写"TODO: 还没做", 不要写"已处理"
但实际 grep 不到代码 — 这是给未来维护者埋的炸弹.
