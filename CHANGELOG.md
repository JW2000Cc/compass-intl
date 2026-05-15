# Changelog

All notable changes to Compass will be documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### 2026-05-13 (后续): 简历多语言导出落地

**根因**: 多语言基础设施 (variant_translator + translated_json + build_json_resume target_language + langQ() UI) **早已实现**, 但用户 zhenghao 的 .docx 模板里 `<w:rFonts w:eastAsia="...">` 字段都指向 LMMathSymbols6 / URWPalladioL — 这两个**纯拉丁/符号字体**, 不含 CJK 字形. 渲染中文/日文/韩文时 Word 找不到字形, 回退到系统默认字体, 跨平台不一致.

UI dropdown 也漏列俄文 (后端 SUPPORTED_TARGETS 已含 'ru', UI 模板缺一行).

**修复**:

- **A. zhenghao_cv_high_fidelity.docx 加 CJK 字体 fallback**:
  - 把所有 32 个 `<w:rFonts>` 元素的 `w:eastAsia` 改成 "Microsoft YaHei":
    - Win Office: 原生支持
    - Mac Office: 通过 font substitution 表自动 fallback 到 PingFang SC
    - 中/日/韩 都能渲染一致
  - 拉丁文字符仍走 `w:ascii`/`w:hAnsi` (URWPalladioL/LMMathSymbols6), 系统找不到时 Word 自动 fallback

- **B. UI dropdown 加俄文**:
  - `compass/templates/resume/_review_panel.html`: `_export_languages` 数组加 `{'code': 'ru', 'name': 'Русский'}`. 后端早已支持.

- **C. 通用模板适配** (Indirect — 给未来 inspire 模板用):
  - template_inspire docx_route 在 B 方案落地时已自动产出 docxtpl for-loop. 未来用户上传别的 PDF 简历, 渲染中日韩文时也会用 docx 字体 substitution. 但**新模板**未来需要类似 CJK fallback 修法 (这是 future polish, 可加进 docx_route 一个新 PASS 4).

**E2E 验证**:
- 5 种目标语言用真 LLM 翻译 + docx 渲染 + python-docx 字符检测:
  - zh: 33 CJK chars ✓
  - ja: 18 CJK + 27 假名 ✓
  - it: Latin+ 重音字母 ✓
  - de: ä/ö/ü ✓
  - ru: 102 西里尔字符 ✓
- LLM 翻译质量抽样: zh "强烈的自主学习能力" / ja "強い探究心" / it "Dimostrato una forte curiosità" — 自然准确

**Sibling sync**:
- ✅ Compass_template (源码 _review_panel.html) 已 sync
- ❌ zhenghao 的 .docx 模板是 personal data, 不 sync (template zip 里没用户简历)

**已知 Gap (后续可做)**:
- 翻译只对 user-approved/edited variants 生效 — 用户当前 production data 里没 approved variant, 实际多语言需要先跑改写流水线 + approve
- 未来 inspire 出来的新模板需要 CJK fallback 自动注入 (加 PASS 4 to docx_route)

---

### 2026-05-13: template_inspire 补齐 outer-list for-loop + skills 段重做

**根因**: docx_route 模块 docstring 承诺"second pass for repeatable lists handled"
但代码未实现, 任何 PDF→DOCX inspire 产出的模板都是 `r.work[0]/[1]/[2]` 硬编码
索引, 渲染时 jinja `UndefinedError: list object has no element 1` 直接炸。
另: PDF→DOCX 转换的 skills 段常有视觉伪影 (keywords 跨 cell 错位), inspire
没绕过, placeholder 索引错位无救.

**修复**:

- **A. SYSTEM_PROMPT 升级** (`compass/services/template_inspire/docx_route.py`):
  - LLM 在同一次调用里多吐两类信息: `list_format_patterns` (homogeneous /
    heterogeneous + first_format/rest_format) 和 `skills_structure`
    (绕开视觉伪影的逻辑层级)
  - Token 增量 ~500 in / ~700 out (单次 inspire 多 ~$0.003, 开 prompt caching 后更低)

- **B. PASS 2 — wrap_outer_lists**:
  - 按 placeholder 索引 N 自动分组 element (`_scan_blocks_for_list` with sandwich
    inherit logic, 避免一个 list 的 block 贪婪吃下一个 section)
  - homogeneous: 单 `{%p for w in r.work %}{%p endfor %}` wrap, 删 [1:] block
  - heterogeneous: `{%p for %}{%p if loop.first %}[0]{%p else %}[1]{%p endif %}{%p endfor %}`
    保留"第一项 table 突出 + 其余 plain 简化"的视觉层次

- **C. PASS 3 — rebuild_skills_section**:
  - 不信任原 skills 段视觉, 直接删了重做成标准 table 双列 (`{%tr for s in r.skills %}`)
  - 渲染时数据来自 build_json_resume 的 r.skills, skills_structure 仅证明
    "用户原模板确实有 skills 段, 触发重做"

- **D. 3 处 polish**:
  - `_polish_date_fallback`: 单日期字段改 `{{ w.endDate or w.startDate }}` —
    简历惯例 endDate 优先 (毕业年/离职年)
  - `_polish_projects_highlights_fallback`: schema mismatch fallback,
    `(p.highlights or ([p.description] if p.description else []))`
  - `_try_inline_loop`: simple-string list (如 r.languages) 用 inline jinja
    for-loop, 产出 "English, Mandarin, Italian" 逗号 inline

**验证**:
- 回归测试: B 方案产出 vs 手工 transform_docx.py 产出, **逐行字面一致** (19/19 items)
- 模板适配测试: 0/1/3/10 项 work/edu/skills 数据量, 均能渲染不炸

**Sibling sync**:
- ✅ Compass_template (源码) 已 sync
- ❌ Sundial / Visa Sniper 无 template_inspire 模块, 不涉及

**Refs**:
- docs/postmortems/2026-05-13-template-inspire-second-pass.md
- 工作量: 调研 1.5h + 设计 1h + 实施 2h + 测试 1.5h = ~6h

---

### 2026-05-10 (修 bug): 一键抓取浮层用户感知不到 (P-006)

**Symptom**: 用户报"点了 sidebar 一键抓取，看不到进度浮层"

**Root cause**: 之前用浏览器**原生 form submit** (`<form action method=post>` + setTimeout(0) 启 polling). 浏览器 form submit 后立即进入 navigation 等待状态, 旧页面 JS 优先级降级, `setInterval` polling 被 throttle, `classList.add('is-active')` DOM 修改没及时 paint → 用户感知"什么都没发生".

**Fix** (`compass/templates/base.html` submit handler 改 AJAX):
- `e.preventDefault()` 阻止浏览器原生 navigation
- 立即同步调 `startPolling()` (DOM 修改在当前 event loop paint)
- `fetch(form.action, {redirect: 'follow'})` 异步 POST
- 响应回来 `window.location.href = r.url` 手动跳转, flash 走 session 自动带
- `inflight` flag 防双击
- `.catch()` 网络失败 alert + 收摊

**沉淀**:
- `docs/postmortems/2026-05-10-progress-overlay-not-visible.md`
- `docs/bug-patterns.md` 加 P-006: 浏览器原生 form submit + navigation 期间 JS 不可靠
- **教训**: 任何"提交 + UI 反馈"默认走 AJAX, 不要赌浏览器 navigation 期间的 JS 优先级
- smoke-checklist 应该真实点击不能只看 curl POST 端点 (后端 OK ≠ 用户看得到)

**验证**: 后端 endpoint /jobs/api/scrape/progress 1 秒就有数据 (profile_index=1/8, kw='Electrical Engineer', phase=keyword_running), 前端 JS 改成 AJAX 后浮层在当前 event loop 同步 paint, polling 期间页面完全 interactive.

---

### 2026-05-10 (再后续): 流水线静默回退 banner + 一键重试

**起因**: Sundial 加完离线 → GCal 自动补传后, 顺手反思 "Compass LLM 调用失败也是静默 return" 是不是同类问题. 调查发现 Compass 已有完整四层处理 (services/llm.py 3 次 retry + resume_writer 每步 catch + judge_report["pipeline_warnings"] 累积 + ReflectionEvent llm_silent_failure 事件流), **不需要 retry 队列**.

**但仍可改进的体感**: review 页之前只有 "composite 0/10" + flash 一句 "⚠ 2 个步骤静默回退" 一闪即逝, 用户错过 banner 后看不到具体哪步失败、也没法重试.

**做了**:

- `compass/templates/resume/_review_panel.html` KPI row 前加条件 render banner:
  - 显示 "流水线有 N 步静默回退"
  - <details> 折叠列具体 step + 错误信息
  - 解释可能原因 (限流 / 抖动 / 超时) + 重跑成本预期
  - "↻ 重跑失败步" 按钮 POST 到现有 `/resume/run/<job_id>` (复用整流水线重跑路径, 旧 attempt 保留为历史)
- 黄色 (#fffbeb / #fbbf24) 警示色, 跟全程 P-003 audit fake-key 那种用 danger 红区分
- 用 `data-slow-action="true"` + `data-loading-text="⏳ 重试中... (30-90 秒)"` 复用现有 loading 反馈机制

**没做** (考虑过但拒绝):
- 加 retry 队列 (Sundial 模式): 不适用. Sundial 的 op 是幂等"重推单条事件", Compass 流水线步骤之间有数据依赖, "单步 retry" 没语义意义
- 把 inline retry 从 3 提到 5: 用户已经等 7s, 提到 31s 会以为卡死
- 加新 endpoint /retry: 现有 `/resume/run/<job_id>` 已经能做整流水线重跑, 多一个端点是冗余

**测试** (mock attempt.judge_report["pipeline_warnings"]):
- HTTP 200, 无 template 错
- banner 显示 + step 列表展开 + form action 正确
- 无 warnings 时 banner 不显示 (条件 render 验证 OK)

---

### 2026-05-10 (后续): 发布门禁 + bug pattern 注册表（8 件套）

> **动机**: 5-10 那次连环修了 4 个 bug，意识到"修一处不够，要扫一类"。补一套
> 跨 release 的纪律：动手前量基线、bug 找到后扫 sibling、修完后程序化校验。

**新加文件**:

```
docs/
├── bug-patterns.md          # P-001..P-005 抽象 pattern 注册表（不是 instance）
├── change-template.md       # solo dev 自填的 PR description 模板
├── smoke-checklist.md       # 浏览器人眼 smoke 清单（程序化测不到的）
└── postmortems/
    ├── _template.md
    ├── 2026-05-10-jobspy-hang.md
    ├── 2026-05-10-llm-fake-key.md
    └── 2026-05-10-zip-cjk-mojibake.md

tools/
├── release_check.sh         # 发布门禁: --baseline / --diff / --quick / --full
├── sibling_scan.sh          # 跨 Compass/Sundial/Visa Sniper 的 pattern regex 扫
├── sync_template.sh         # rsync Compass/ → Compass_template/，diff 验证
└── package_zips.py          # NFC + UTF-8 flag 安全打 personal+template 双 zip
```

**`/healthz?detail=1` 增项**: `vendor_files`（alpine/htmx 大小+sha8）、`scrape_lock`
（`.scrape_in_flight` 存在性 + age）、`scrape_progress`（active/phase/updated_age）、
`last_scrape_at`（最近 `external_scrape` 事件时间）。

**release_check.sh 当前覆盖 12 项**:
1. Python 语法 (`compileall compass/`)
2. CDN 引用回归扫 (templates 不含 unpkg/cdnjs/jsdelivr)
3. 残余调试代码 (`console.log` / `print('debug...)`)
4. Audit 占位符 (P-003 防 `sk-fake-for-audit` 回归)
5. active ↔ template diff 在白名单内
6. 临时 7099 端口启动 boot smoke
7. 17 核心页面探活 (200/302)
8. `/healthz?detail=1` status=ok
9. LLM ping (.env key 仍活, 1-3s 内 pong)
10. `/jobs/api/scrape/progress` 端点 200
11. 4 个 scrape form 都有 `data-progress-source="scrape"` 标记
12. 文档 ↔ 代码漂移 (使用说明.md 含「进度浮层 / 离线指示器 / 90 秒超时 / vendor」)

**sibling_scan.sh 第一次跑就发现的 P-005 实例**:
- ✅ Compass: 0 处 unpkg/cdnjs/jsdelivr 命中
- ⚠ **Sundial: 15 处** (Alpine + htmx + Chart.js + FullCalendar + html2canvas + marked)
- ✅ Visa Sniper: 0 处 (因为它 inline 了所有 JS/CSS)

→ 已记录, 后续清理 Sundial 时复用 Compass 5-10 的本地化方案。

**完整发布流程现在是**:

```
[改之前] tools/release_check.sh --baseline           # 量基线
         填 docs/change-template.md                   # 逼自己想清楚
[过程中] 发现 bug → 走 docs/postmortems/_template.md
         → tools/sibling_scan.sh '<pattern>'         # 横扫三程序
         → 必要时更新 docs/bug-patterns.md
[完成后] tools/release_check.sh --diff                # 不引入新红灯
         走 docs/smoke-checklist.md                   # 人眼 smoke
         tools/sync_template.sh                       # 同步
         python3 tools/package_zips.py                # 双 zip
```

**借鉴**: GitHub PR template (change-template) / SRE postmortem doctrine / K8s
readiness probe (healthz extension) / "fix the class not the instance" (P-XXX
pattern registry) / "boil the lake" (release_check 12 项一次走完).

---

### 2026-05-10: 抓取可见性 + 离线韧性 + 静默 bug 修复

**用户能感知到的变化**：

1. **进度浮层（一键抓取）**
   - 点 ⚡ 一键抓取后立即弹居中浮层，每 2 秒刷新
   - 显示当前 profile（`3/8`）、当前 keyword（`5/9` + linkedin/indeed）、累计已抓 / 新增 / 已存在 / 规则挡 / 失败、已用时（mm:ss）
   - 单 keyword 跑超 60 秒，浮层标题改成"⚡ 一键抓取（当前 keyword 较慢，最长 90s）"做柔性提示
   - 表单 redirect 跳页时浮层随 DOM 销毁自动消失，flash 显示总结
   - 实现：`scraper.py` 加 `write_progress` / `read_progress` / `clear_progress` 原子文件读写；`run_scrape` 接受 `progress_cb`；新加 `GET /jobs/api/scrape/progress` 端点；`base.html` 加浮层 HTML/CSS/JS
   - 三个 scrape route（`scrape_direct` / `scrape_now` / `search_config_scrape_all`）合并到 helper `_run_profiles_with_progress`，避免 50 行重复

2. **离线指示器**
   - 浏览器 `navigator.onLine` 报告网络断开时，右上角冒红色「● 离线」pill
   - hover tooltip 说明哪些功能仍可用（本地浏览/编辑）vs 哪些会失败（LLM/抓取/Gmail/翻译）
   - 实现：`base.html` 注入纯 vanilla JS 监听 online/offline 事件；不依赖 Alpine

3. **抓取超时保护**
   - 单次 jobspy 调用 90s 强制超时，挂死的 keyword 自动跳过、错误信息进 errors 列表
   - 解决了 5-10 之前发现的 bug：LinkedIn drop TCP 不发 RST → tls-client (Go cgo) 无 read 超时 → goroutine 永久卡 `__psynch_cvwait` → 整个 scrape 函数 with `_scrape_lock` 不退出 → 后续抓取永远被 flash "scrape running" 拦回（用户表面症状："点抓取 0 个新增"）
   - 实现：`scraper.py:scrape_one` 包 daemon 线程 + `t.join(timeout=90)`；不用 ThreadPoolExecutor 因为 with-block 退出强制 wait=True 等 future 完成（正好踩进死等）

4. **本地资源（离线韧性）**
   - Alpine.js 3.13.10 + htmx 1.9.12 之前从 `unpkg.com` CDN 加载；现下载到 `compass/static/js/vendor/` 本地引用
   - 离线 / unpkg 抽风时 UI 仍能交互（之前所有 `x-data` / `hx-*` 属性会变哑巴）
   - 影响范围：base.html 共 2 处 `<script src>` 改本地引用

**Bug 修复**：

- **`data/llm_settings.json` 残留 `sk-fake-for-audit` 占位符** —— 5-08 安全审计时把活跃 overlay 文件的 api_key 替换成 fake，但没改回来。该文件作为 overlay 覆盖 `.env`（`apply_overlay_to`：非空 key 才覆盖），导致用户点「测试连接」返回 401 invalid x-api-key 即便 .env 有真 key。修：把 api_key 字段改成空串，让 .env 接管。
- **打包 zip 中文文件名乱码** —— macOS 自带 `zip` 命令对 NFD 中文文件名（如 `使用说明.md`）处理有 bug，没设 UTF-8 flag → 解压出来变 `使?? 说???.md`。改用 Python `zipfile` 重打，强制 NFC 规范化 + bit 11 (UTF-8 flag)。

**文档同步**：

- `使用说明.md` 三处行内更新（一键抓取 / 启停 / 常见问题）+ 底部加「5-10 改动速览」
- `Compass_template/` 同步全部源码
- 重打 `Compass_personal_20260510.zip` + `Compass_template_20260510.zip`

---

### 2026-05-07: R 方案重做 + 100 轮 audit（v3 → 当前）

**起因**：5-07 用户发现 multi-profile 实现是"伪 in-place"——namespace 共享 jobs.bp 但物理 fork（`routes/jobs_config.py` / `routes/jobs_llm.py` / `services/search_config.py` / `templates/jobs/config/` / `templates/jobs/llm/`）。用户引用 5-06 12:55 原话"为啥不直接在老 ui 那个接口那里修改"，要求**真 file in-place**重做。

**R 方案执行**（v3 节点 → 当前）：

- **R1 粗暴回退**：`Compass/` ← `Compass_v3_pre_v4_20260506/` 完整恢复
- **R3 cherry-pick ADR-0015 全套**：A-G+G2 简历改写流水线（services/claim_grounder L5a/L5b、adversarial_voice two-phase、native_ats_check、bullet_decomposer、variant_translator、user_facing_translator、review_rows、exporters/、_review_panel.html、_decomposition_panel.html）+ alembic baseline + decomposition migration
- **R3 cherry-pick 5-07 修复**：services/themes / routes/exports（PDF/MD/TXT 导出）/ templates/themes/inspire.html / 8 audit bug fix
- **R4 真 file in-place multi-profile**：
  - `data/last_scrape.json` schema 原地升级（保留文件名，自动从 v3 单一全局 → multi-profile，第一次 load 自动 save 落盘）
  - `routes/jobs_config.py` **物理合并**到 `routes/jobs.py`（`from .jobs import bp` 改为同文件，多 ~600 行进 jobs.py）
  - `services/search_config.py` 重命名 `services/scrape_config.py`（语义贴合 `last_scrape.json`，dataclass `JobSearchIntent` 名保留）
  - `services/dealbreaker_gate.py` 保留（按 ADR-0017 heuristic："新独立 capability"）
  - `templates/jobs/config/` 平铺到 `templates/jobs/scrape_config_*.html`
  - `services/conflict_detector.py` 单源化（删除"双源 fallback"逻辑，直接读 last_scrape.json）
  - `services/tier_classifier.py` profile injection 接通
- **R10 真 file in-place LLM Settings**：
  - `routes/jobs_llm.py` **物理合并**到 `routes/jobs.py`
  - `templates/jobs/llm/llm.html` 平铺到 `templates/jobs/llm.html`
  - 4 个 LLM endpoint 全注册到 jobs.bp
- **删除 ADR-0016 / ADR-0017**：fork 决策的副产物，重做后失效

**100 轮优化**：

- 备份目录加 `DO_NOT_RUN_FROM_HERE.txt`（`Compass_v3_pre_v4_*` / `Compass_v4_pre_C_rollback_*` / `Compass_with_5_07_work_*`）
- Unused imports 大批清理（22 → 1）
- 跨程序 FIELDNAMES 单源验证（Sundial event_writer / csv_sync / gcal_export 三处都 `from event_schema import`）
- TODO/FIXME 扫描（仅 1 处设计性 TODO 在 user_facing_translator.py:420）

**最终状态**：277 测试全绿；`routes/jobs.py` 1500+ 行（jobs basic + multi-profile 8 endpoint + LLM 4 endpoint，单文件 in-place）；`last_scrape.json` 文件名保留 + multi-profile schema；桌面 `Compass_personal_20260507.zip` (1.4M) + `Compass_template_20260507.zip` (1.3M)。

**待用户手动**：凭证轮换（@BotFather `/revoke`、Anthropic Console 删 key、Google OAuth 删 secret）。

---

### Added — 2026-05-03: Fact-level revision history + sediment workflow

**核心：把"沉淀到我的简历"从 silent mutation 改造为 append-only revision history。**

按 GitHub PR 心智模型重构：
- 每个 `RewriteAttempt` = 一个 PR；用户在 review 里 ✓/✎ 都在沙盒里
- 「📌 沉淀」 = merge to main → 写一行 `FactRevision` 记录（不再无声 mutate `ResumeFact.text`）
- 任意旧 revision 可点「↺ 设为当前」回滚 — 实质是 append 一条 `rolled_back` 类型的新 revision，旧记录永不删除
- 用户线程上验证过：删中间 revision 是 footgun（语义崩塌）→ 故意没有 delete API

**新数据模型** (`compass/models/core.py`):

新表 `FactRevision`:
```python
fact_id, text, kind, source_attempt_id, source_job_id,
source_variant_id, note, created_at
```

`kind ∈ {origin, promoted_from_attempt, rolled_back, manual_edit}`

每条 `ResumeFact.revisions` 关系自动反查时间倒序。`ResumeFact.text` 仍是 current text（=最新 revision 的 text）—— 沿用，但通过 `services/fact_history.write_revision()` 这唯一入口修改。

**新 service** (`compass/services/fact_history.py`):

- `ensure_origin(session, fact)` — 老 fact 第一次访问时自动 backfill 一条 `kind=origin` 的 revision
- `write_revision(session, *, fact, new_text, kind, source_*)` — 唯一 mutation 入口；同 text 是 no-op；自动调 ensure_origin
- `list_revisions(session, fact_id)` — 倒序列表
- `rollback_to(session, *, fact, target_revision_id)` — append 一条 `rolled_back`，目标行不删

**新路由 + 视图：**

- `POST /resume/attempt/<id>/promote` — 重写：每条 ✓/✎ 通过 `write_revision` 写一行；保留 `ReflectionEvent` 给反思 dashboard 双写
- `GET /identity/fact/<id>/history` — 单条 fact 的修订时间线，4 种 kind 各自图标 + 颜色；自由编辑入口（脱离 RewriteAttempt 直接改 fact）
- `POST /identity/fact/<id>/restore/<rev_id>` — 「↺ 设为当前」
- `GET /identity/sediments` — 所有「沉淀事件」按 attempt 分组的总览页（GitHub「PR merge log」风格）

**UI:**

- `_review_panel.html`: 「📌 沉淀到我的简历（N）」按钮换成 Alpine modal 二次确认（替换原生 confirm()），列出影响的 fact 数 + revision 写入说明
- `_fact_card.html`: 加「📜 演变」链接进 fact_history 页
- `overview.html`: 顶部加「📌 沉淀历史」入口，文案改为强调"main 分支"心智
- `base.html`: 侧栏 nav 加「📌 沉淀历史」一级条目，区分 active state（fact_history / sediments_overview / restore）
- 沉淀历史页 + 单 fact 演变页都加「为什么不能删某次修订」说明（防 footgun）

### Added — 2026-05-03: 简历 review UI Claude-in-Word 风格

`_review_panel.html` 重做（约 350 行）：

- **2 列布局**：左 iframe 实时 resume preview（应用了 ✓/✎ 的状态）+ 右 sticky suggestions rail
- **可拖拽分隔条**：grid columns 用 `var(--review-left/right)` driven by drag — 比例存 localStorage
- **每条建议变 compact card**：level badge + inline diff + ✓ ✎ ↻ ✗ 紧凑按钮（替代原 large card）
- **单个反馈 widget 在底部**（替代每个 variant 各一个）
- **去掉 HTMX 重定向 bug**：原 `hx-swap="outerHTML"` + 服务端 302 redirect 导致整页被塞进单卡片 div（"界面套界面"）。改为 native form POST + 服务端用 `next` 字段重定向回 anchor

加 next 字段处理：嵌入 `/jobs/<id>` 时 next 用 `#step-resume_rewrite`（Alpine activeStep 只识这种 hash），独立 `/resume/review/<id>` 用 `#sug-<variant>` 滚到那条卡片。

### Added — 2026-05-03: rerun + per-variant regenerate + negative prompt feedback

`run_pipeline` 改造为 PR-style inheritance：

- 已 ✓/✎ 的 fact 在 rerun 时**保留不动**（用户决定继承到新 attempt）
- 已 ✗ 的 fact 在 rerun 时把 rejection_reason 当 negative prompt 喂给 LLM，让重生有方向感
- 待决 (pending) 的 fact 走标准重生

新 helper：`_build_prior_decisions` + `_build_rejected_history`。

`rewrite_one` 加 `negative_examples: list[(text, reason)]` 参数；prompt 里 append "do NOT produce something similar to" 段。

新单条重生路由 `POST /resume/variant/<id>/regenerate` — 用户可对单个 variant 点 ↻ 让 LLM 换角度重写（不影响其他）。

UI:

- 再跑一次按钮换成 Alpine modal，列「✓ 保留 X / ⏳ 替换 Y / ✗ 重生 Z」详细破坏性说明
- 每个 pending variant 卡片加 ↻ 按钮调 `/resume/variant/<id>/regenerate`
- HTMX redirect 后 dedup：每个 fact 只显示最新 variant（旧的留 DB 当 audit trail）
- Loading-state helper：data-slow-action="true" 的 form 自动 disable + 显示「请稍候，LLM 处理中…」

### Added — 2026-05-03: GitHub-style polish

- **Sidebar 一键最小化**：base.html 加 « 按钮 + `[` 键盘快捷键，状态 localStorage 持久化；最小化后 nav 显示 icon + tooltip
- **LLM 调用统一加 timeout=60**：`services/llm.py:_chat_claude/_chat_openai` Anthropic + OpenAI client 都设 timeout，防止网络故障卡死整个请求
- **Loading state 全局**：data-slow-action="true" form 自动 disable + 替换文案
- **promote 成功 flash 引导用户去侧栏「📌 沉淀历史」** 看变更详情
- **review_panel custom open redirect 校验**：next URL 必须以 `/` 开头，防止 open redirect 攻击

### Bug 修复

- **「✓ 跳到下一界面」** 根因：嵌入页面 `next` 用了 `#sug-xxx` hash，但 Alpine activeStep 只识别 `#step-xxx` → fallback 到「下一步」(apply)。修：嵌入用 step hash，独立 review 用 sug hash
- **沉淀页 N+1 query** 改成 IN 一次拉所有 fact + job（先前每条 sediment item 各 session.get(ResumeFact)）
- **Sediment summary 内点链接误触 toggle**：`event.stopPropagation()` 替换 Alpine `.stop`（链接不在 Alpine scope 内）
- **Edit redirect 错回 overview**：`edit_fact` 加 `next` 字段，从 fact_history 进的编辑保留在那
- **stepper 4 步挤换行**：grid → flex `1 1 0`，labels `text-overflow: ellipsis`

### 加固（无具体 bug 但防御性改进）

- 所有触动 `ResumeFact.text` 的代码路径**全部走** `write_revision`（auditable）— 用 grep 确认无 bypass
- session_scope autoflush=False，所有写入路径加显式 `session.flush()`，防 attempt.id 取不到
- SQLite WAL + busy_timeout=30s（防止 dashboard / bot 同时写 db 时 "database is locked"）
- 所有 `from __future__ import annotations` 已加到所有触碰 PEP 604 的 .py 文件（防 3.9 兼容回归）
- Compass + Sundial 双向 sync 验证：所有 session 改过的文件都同步过 main↔template

---

### Added — Stage C (2026-05-02): 简历 diff + 工作流分阶段披露

2 件并行 + 1 个 bug 修复（约 450 行）：

1. **改造 — 简历 track-changes diff** (`compass/services/resume_diff.py` + `templates/resume/review.html`)。借鉴 Word + Claude side panel 的可视化（用户给的截图）：每条 variant 一个块，块顶部 L0-L5 语义标签（"L1 同义改写"/"L2 显式化"/"L3 重新定调"等）+ 块内是 inline diff —— 删除词红色 strikethrough、新增词蓝色下划线，**而不是原来两段文字并排**。`difflib.SequenceMatcher` 词级 diff，注册为 Jinja `diff_html` filter。Adversarial Voice 反对从总是显示改成 `<details>` 折叠（默认收起）。Accept/Reject/Edit 按钮整合到右栏。

2. **工作流 progressive disclosure** (`routes/jobs.py:detail` + `templates/jobs/detail.html` + `routes/contacts.py:panel` + `templates/contacts/_panel.html`)。job detail 页按 status 进入 5 个阶段：
   - **evaluate** (new): JD + 反馈 + warm 联系人，cold 搜索 + 简历改写都隐藏
   - **prepare** (reviewed): + 简历改写 + cover letter 入口；cold 仍隐藏（投前不冷联系）
   - **sent** (applied): + cold 联系人搜索（"我刚投了，能聊吗"）
   - **active** (interview/offer): + 面试准备 skill 提示
   - **archive** (rejected/passed): 只读复盘
   每个阶段顶部一个状态卡解释 "当前阶段" + "做什么" + 可展开"五阶段说明"。 disabled panels 改成灰显 "阶段未启用 — 切到 X 状态后开放" 而非完全隐藏，避免用户困惑"功能去哪了"。

3. **Bug 修复 — Sundial onboarding 一道题也看不到** (`templates/base.html`)。根因：onboarding 页大量用 Alpine.js 指令（x-data / x-show / x-init / x-cloak / @click），但 base.html 从未引入 Alpine 库——`x-cloak` 默认 display:none 永远不解除，全部题目被永久隐藏。修：base.html 加 Alpine 3.13 + HTMX 1.9 CDN + `[x-cloak]` CSS rule。**两个 sibling 同步**（Sundial / Sundial_template）。

4. **Bug 修复 — Sundial bot 自动停止不会重启** (`bot.py`)。原代码 run_polling 抛出任何异常都 re-raise → 进程死 → bot.pid 留死指针 → 用户必须手动启动。修：包成 while 循环，瞬时错误（NetworkError / 5xx / sleep-wake 断连）以 1→60s 指数 backoff 自动重试，**永久错误（409 Conflict / 401 Unauthorized）才退出**。

### Added — Stage B (2026-05-02): Effort 包 + PKM markdown + Kanban

3 件并行上线（约 850 行）：

1. **改造 3 — Effort 包**（`compass/services/effort_pack.py` + `compass/templates/jobs/_effort_pack.html`）。每个 job 详情页顶部一个 checklist：读 JD / 改简历 / 找内推 / 发 outreach / 投递（+ 面试准备如果到了）。**多数 step 自动从现有数据推**——发了 OutreachAttempt → "找内推" 自动 ✓；status 变 applied → "投递" 自动 ✓。新表 `EffortPack` 只存用户显式 override（手动标 done / skipped / n/a）。剩余预估分钟数实时计算。让用户**不会忘记找内推这步**——最高 ROI 的动作之前最容易漏。

2. **PKM markdown 导出**（`compass/services/pkm_export.py` + Settings.pkm_export_enabled）。可选开关。打开后 connections / companies / jobs 同步成 .md 文件到 `data/contacts/` `data/companies/` `data/jobs/`。**hand-edit-safe**: 系统块用 `<!-- COMPASS:SYSTEM:BEGIN/END -->` 标记，用户在标记外的手写笔记**重新同步不会被覆盖**。中文名 slug 保留 CJK 字符。详见 [ADR-0013](docs/decisions/0013-pkm-markdown-export.md)。

3. **Huntr Kanban 视图**（`compass/templates/jobs/kanban.html` + `_kanban_columns` 路由 helper）。`/jobs/?view=kanban` 切换。按 status 分 6 列（未读 / 已看 / 已投 / 面试中 / Offer / 被拒），每 job 一卡。Tier 5 和 status=passed 显式排除（pipeline 板上的噪音）。卡片显示 tier badge + 公司 + 状态变更日期。点击跳详情，**目前只读**——拖拽改状态 deferred 直到用户提需求。详见 [ADR-0014](docs/decisions/0014-kanban-as-readonly-funnel.md)。

### Added — Stage A (2026-05-02): warm + cold contact discovery (~900 行)

替代撤回的 SKILL.md 路径，把找内推做成 Compass core 功能：

- **改造 1 种子表** `OutreachAttempt`（18 字段，含闭环：response_received / response_kind / led_to_interview / led_to_offer）+ `Connection`（warm 层基础）。
- **`services/contact_finder.py`** —— port v1 `hr_finder.py` 多轮策略（HR → 部门同事 → 宽泛），用 `ddgs` 库替换手写多引擎 + 30 天 cache + 公司名归一化 + "在职"验证（`at <company>` 正则 + 防误匹配 lookahead）。
- **`services/contact_outreach.py`** —— port v1 `outreach_gen.py`，用 Compass 已有 LLM client（不依赖 anthropic SDK），按 medium 切换 280 / 300 / 150 字限制，按地区切语言（DE→德语 / IT→意语 / 其他→英语），warm/cold 不同 system prompt（warm 用真实关系作开场白）。
- **`services/connections.py`** —— LinkedIn `Connections.csv` 导入（容错 3 种日期格式 + LinkedIn 2024+ 4 行 preamble 跳过）+ 公司名 fuzzy 匹配（完全 / 子串 / token overlap 三层评分）。
- **路由**：`/connections` （上传 + 概览 + top companies）+ `/jobs/<id>/contacts`（HTMX panel）+ `/jobs/<id>/contacts/cold` (lazy 10-30s) + draft + sent。
- **UI**：jobs detail 页底部 contact panel 双层（warm 即时显示 + cold 按钮触发）+ 起草 modal（HTMX swap）+ 「我发了」记 OutreachAttempt + 闭环字段事后填。
- **依赖新增**：`ddgs>=9.14`、`beautifulsoup4>=4.12`。


撤回 Stage 1 加的两个 SKILL.md：

- `compass/skills/find_hiring_manager/`
- `compass/skills/outreach_email_gen/`

理由：warm outreach / 找内推被验证是**真高频核心功能**（朋友实证：回应率比 ATS 冷投 15-30 倍），不该锁在 Claude Code Skill 路径上——那对 Compass 真实目标用户不可达。

替代方案：connection-graph-native 路径，从用户 LinkedIn CSV / 校友 / GitHub network 出发，本地 SQLite 存储，jobs detail 页内嵌显示。**待开发**——跟用户讨论清楚 V1 范围后启动。

详见 [ADR-0011 amendment](docs/decisions/0011-claude-only-skill-ecosystem.md)。

### Added — Stage 1 of 5: post-lock quality-of-life improvements

User said quality first ("保证质量优先慢慢来不着急") — Stage 1 ships 6 carefully scoped items:

1. **Funnel multi-dimensional slicing** (`compass/services/job_funnel.py:compute_by_dimension` + `routes/funnel.py` + `templates/funnel/dashboard.html`): added 3 slice tables — by source / location / role — to the funnel dashboard. Each shows scraped→applied→interview→offer per bucket, plus 应聘率 / 面试率. Ported from Job Mate v2 verified pattern. Lets the user identify which channel actually produces interviews. Includes interpretation guide card.

2. ~~**HR-finder + outreach-email skills**~~ —— **2026-05-02 已撤回**。原本：两个 Claude-Code-Skill format YAML+markdown templates（anti-bulk by design）。撤回理由：warm outreach 是核心高频功能，不该锁在 Claude Code 路径——对 Compass 真实用户不可达。替代方案见 ADR-0011 amendment。

3. **Active-alert banner system** (`compass/services/reflection_engine.py:compute_active_alerts` + `compass/app.py` context_processor + `compass/templates/base.html`): aggregates 4 signals into a global banner that appears on every page — value-regression checkpoint due (>14 days), drift unstable (level shift ≥2), thumbs-down/up divergence, Tier-2 starvation. Dismiss-for-session via sessionStorage. Replaces the previous passive "only-on-/reflect/dashboard" warning. Also fixed a pre-existing tz bug in `build_drift_report` (SQLite strips tzinfo on round-trip).

4. **Adversarial Voice submit-time reflection** (`compass/services/pre_submit_reflection.py` + `compass/templates/jobs/pre_submit_reflection.html` + `routes/jobs.py:change_status`): when user clicks "applied" on a job for the first time AND there's something material to surface (recent objections / drift / tier outlier / overdue value review), interrupt with a one-step read-only reflection page. Includes a "六个月前的你说" line composed from the earliest drift snapshot. User confirms or backs out — never blocks, but the round-trip itself is logged as `applied_with_reflection_seen`. Silent pass-through when nothing material to say. This is the frozen, never-trained reverse channel surfaced at the moment of high commitment.

5. **Desktop screen-width responsiveness** (`compass/templates/base.html`): tiered max-width — 880 (default laptop) / 1040 (≥1280) / 1180 (≥1600) / 1280 (≥1920). Mobile/PWA explicitly excluded per user. Footer no longer fights the responsive container.

6. **Sundial monthly reflection report** (`时间反思工具/Sundial/analytics.py:monthly_digest_text` + `Sundial_template/`): existing monthly digest extended with reflection layer — this-month-vs-last-month deltas (≥20% changes), 3 data-driven reflection prompts that always reference concrete numbers (no generic "what did you learn?" fluff). Synced both Sundial siblings.

### Added — Blacklist 升级（自然语言输入 + 反思层效果分析）
解决 substring blacklist 的两个 hidden bug:

- **跨语言失效**: 用户写英文 "spam recruiter" 屏蔽不到意大利语 "agenzia spam"
- **词不达意**: 用户写"廉价职位"实际 JD 用 "entry level"

新增三件:

1. **`💬 自然语言描述`输入区** (`compass/services/blacklist.py:from_natural_language` + `routes/blacklist.py:from-description` endpoint): 用户自然语言说"不要 unpaid 不要 mlm 不要纯销售"，LLM 转成多语言关键词清单，用户审核加入。
2. **🪞 反思层效果分析** (`services/blacklist.py:effectiveness_audit`): `/blacklist` 页底部显示"被黑名单过滤了多少 + LLM 反复判 Tier 5 的公司清单"。一键加入屏蔽。让用户看到自己的屏蔽规则是否真有效——是 Compass 反思系统在黑名单领域的延伸。
3. UI 整合: 三种入口（自然语言 / 手动 textbox / Tier 5 推荐）共存，用户按习惯选。

### Added — Quality-of-life features (porting back proven patterns from Job Tracker / Job Mate)
v1.0 Compass focused on the reflection/calibration framework but skipped daily-use plumbing that v1/v2 had verified. Adding back what real users actually need:

- **Blacklist** (`compass/services/blacklist.py` + `/blacklist`): filter out specific companies (substring match) or keywords (in JD or company name) at scrape time. Stored in `data/blacklist.json` — flat file, deliberately NOT a DB table to avoid post-lock schema change. Borrowed from Job Tracker (v1).
- **`hours_old` per-group time filter** in scrape UI: 24h / 3d / 7d / 30d / 不限. Default 7d. jobspy passes through to LinkedIn/Indeed natively. Avoids burning LLM calls on stale postings.
- **Per-group enabled flag**: each scrape config row has a checkbox to temporarily disable that group without deleting it. Disabled groups are silently skipped at scrape time.

These are pure routes/services additions — no schema change, no core service touched.

### Added
- **Multi-group scrape config** (`/jobs/scrape`): users can now configure multiple `(locations, keywords)` pairs in one form — e.g. "Milan/Italian fintech" + "Berlin/English ML" each search independently. Supports up to 20 groups, with localStorage persistence (your config sticks across reloads). Backward compatible with the legacy single `locations`+`keywords` form schema.
  - Real-need driven (different regions = different role types = different salary expectations)
  - No schema change, no core service touched — pure routes + template work
  - Per-group failure isolation (one group's error doesn't kill others)
- **Multi-language keyword alias suggestions** (`compass/services/keyword_alias.py` + `/jobs/suggest-aliases` + UI button per group): LLM proposes 5-8 multi-language + same-lang variants for each canonical keyword. User checks boxes to opt in (adjacent role suggestions default unchecked). Borrowed unchanged from Job Mate's keyword_alias.py (proven design). Without this, users typing "数据分析师" searching Italy LinkedIn return ~0 results because Italian listings index by "analista dei dati". Solves the hard-base of the scrape pipeline.
  - Aliases broaden SEARCH only — the LLM matcher (tier_classifier) reads JD semantically and doesn't need expansion.
  - Never auto-add: user is always the gatekeeper (matches Compass Reflective Flywheel philosophy).
- **Module help blocks** on all 8 pages: `<details>` panel at top of each page explains "what / why / when to use" — addresses the "I clicked into Reflection and felt lost" UX gap. Pure template work, no logic change.

### Fixed
- **Critical**: First-run startup created empty database (no tables) due to `Base.metadata.create_all()` running before `models/core.py` was imported. Added explicit `from . import models` in `app.py` to trigger model class registration before table creation. Symptom was `OperationalError: no such table: ...` on every request.
- **Missing dependency**: `python-jobspy` was used in `services/scraper.py` but not in `requirements.txt` — would crash at scrape time. Also added `markupsafe` (used in error handler).

## [1.0.0] - 2026-05-01

First "core locked" release. From here on, core 不再扩 (see [docs/CORE_LOCKED.md](docs/CORE_LOCKED.md)).

### Added — Phase 0 反思层
- ReflectionEvent 事件流（事件溯源轻量版）
- ToolAssumption 系统假设暴露 + 用户可推翻
- 漂移仪表盘（calibration drift 时间序列）
- stated-vs-revealed 偏好对照

### Added — Phase 1 Tier Matcher
- 5-tier 分类（替代 v1 push/skip 二元）
- JobSpy 包装（多站抓取）
- 漏斗追踪（含 by_tier 维度）
- 70/20/10 ε-exploration 配额

### Added — Phase 2 简历多步流水线
- IdentityVersion + ResumeFact + FactEvidence + FactVariant 三层模型
- 6 层 claim grounding（L0–L5，L5 硬拦截）
- 访谈式发现（LLM 提问 + 合成 facts）
- Adversarial Voice（永远冻结的反对声音）
- 三角色 judge（ATS / HR / Hiring Manager）

### Added — Phase 3 Calibration + 反向调节
- UserCalibration 边界学习（slow drift ±1）
- 价值观回归 checkpoint
- 漂移检测 + 警报

### Added — 工程基础
- 启动脚本（Mac .command + Win .bat）
- Win 兼容代码层（asyncio policy + UTF-8 stdout + chcp 65001）
- 时区可配置（COMPASS_TZ + Jinja `local_dt` filter）
- 文件上传安全（10MB cap + 扩展名白名单 + secure_filename + UUID 后缀）
- 错误页 XSS 转义（markupsafe.escape）
- LLM 端口检测 + empty choices guard
- v1 → v2 数据迁移脚本

### Added — Skill 生态
- MCP server（暴露 6 个 compass.* tools 给 Claude Code Skill）
- 4 个 reference skill 模板：
  - `ats_keyword_audit` — ATS 关键词覆盖审计
  - `cover_letter_gen` — Cover letter 生成
  - `company_brief` — 面试前公司简报
  - `monthly_reflection_report` — 月度反思报告

### Added — 集成
- Gmail 集成（应用状态自动检测）
- JSON Resume 导出
- HTML preview（浏览器打 PDF 的标准简历视图）

### Added — 文档
- 11 个 ADR（决策记录）
- 6 个 Discussion（高层 essay）
- DESIGN_PHILOSOPHY.md（总览）
- IMPLEMENTATION_NOTES.md（实施细节）
- CORE_LOCKED.md（v1.0 锁定声明）
- before-adding-feature.md（自我克制清单）
- SKILLS.md（Claude Code Skill 集成教程）
- PORTABILITY.md（程序可移植约定）

### Removed
- 内置 skill 引擎（`compass/services/skill_engine.py` / `compass/routes/skills.py` / `compass/templates/skills/`）—— 路径精简到 Claude-only，见 [ADR-0011](docs/decisions/0011-claude-only-skill-ecosystem.md)

### Tests
- `tests/test_tier_classifier.py` — 5-tier 分类核心 case
- `tests/test_claim_grounder.py` — L5 deterministic 拦截 case
- `tests/test_calibration_learner.py` — slow drift case

---

## [0.1.0] - 2026-05-01 (initial implementation)

Phase 0–3 一次性实现的开发版本——见上面 1.0.0 的初版状态。

---

*Convention: 每次改动**都**记一行到 `[Unreleased]`，发版时移到新版本号下。*
