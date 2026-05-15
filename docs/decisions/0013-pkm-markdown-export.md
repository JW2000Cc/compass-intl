# ADR-0013 · PKM-style markdown export alongside SQLite

Status: Accepted
Date:   2026-05-02

## 情境

Compass 现在所有用户数据存在 `data/compass.sqlite` —— 单文件、可移植、查询快。但有几个隐性代价：

- **黑盒**：用户用 Compass 时看不到自己的数据"长什么样"。每条 Connection、每个 Outreach 草稿都关在 SQLite 里
- **迁移困难**：Compass 哪天死了 / 用户想换工具——SQL schema 不能直接喂给别的系统
- **审计不友好**：想 git 版本化、想用 Obsidian 浏览、想用 grep——SQLite 都不友好
- **不像"用户拥有数据"**：单一文件存所有，对非技术用户感觉像 SaaS（即使本地运行）

PKM（个人知识管理）社区有现成的范式：**每个 entity 一个 markdown 文件**，frontmatter 存结构化元数据，body 存自由文本。Obsidian / Logseq / 各 PKM 工具的核心模式。

## 选项

### A. 全部走 SQLite（现状）
- ✓ 查询快、JOIN 自然、schema 演化清晰
- ✗ 上述所有缺点

### B. 全部走 markdown（替换 SQLite）
- ✓ 用户可读可改可 git
- ✗ 查询慢（全文扫）、无 JOIN、并发写易冲突
- ✗ 反思引擎 / tier classifier 等需要快查询的功能崩塌

### C. 混合：SQLite 是 source of truth，markdown 是用户可读副本
- ✓ 查询性能保留
- ✓ 用户能直接读 / 用 Obsidian 看 / git 版本化
- ✓ 哪天 Compass 死了，用户的笔记还活着
- ✗ 双向同步实现成本（如果做 markdown→SQL 也要 reconcile）
- 折衷：**只做 DB → markdown 单向**，markdown 作为只读副本 + 用户手写笔记区

## 决定

**选 C，单向 DB → markdown 同步**。具体形态：

- **opt-in**：默认关闭。`COMPASS_PKM_EXPORT=1` 或 settings 开关启用。原因：避免给不想要 .md 文件的用户惊喜
- **每 entity 一个 .md**：
  - `data/contacts/<slug>.md` — Connection
  - `data/companies/<slug>.md` — 有 ≥1 connection 的公司
  - `data/jobs/<slug>.md` — Job（轻量摘要，完整数据仍在 DB）
- **hand-edit-safe**：每个 .md 分两段
  - SYSTEM 块（`<!-- COMPASS:SYSTEM:BEGIN/END -->` 标记内）：每次同步覆盖
  - USER 块（标记外）：永不覆盖，给用户写自己的笔记
- **触发**：`/connections` 页面一个「↻ 同步全部」按钮 + 未来可加 import 时自动同步
- **slug 处理**：保留 CJK 字符（中文名直接用）+ 去标点 + 拼 6 位 ID 防重名

## 代价 / 后果

- **同步是单向的**：用户在 markdown 里改了 frontmatter（比如改 company）—— Compass 不会感知。下次同步会用 DB 数据覆盖系统块。**这是有意的**：双向同步需要 conflict resolution，复杂度暴涨
- **文件数量**：典型用户 LinkedIn 几百到几千连接 → 几百到几千 .md 文件。git 操作可能慢，但 ls / Obsidian 都能正常处理
- **路径含 CJK**：在 macOS / Linux 都 OK，Windows 也行（NTFS unicode 支持），但有些老脚本可能不爽——保留 CJK 是用户友好优先

## 什么时候应该重审

- 用户开始抱怨"我在 Obsidian 改的 company 字段被同步覆盖了"——那时考虑 markdown 是否可读回
- 文件数 > 5000 影响 ls / 同步性能——考虑分层目录
- 出现真正能读 markdown 的反向同步需求（用户用 Obsidian 加 tag → 希望反映在 Compass UI）

## 备注

- 实现：`compass/services/pkm_export.py`（~280 行）
- 与之绑定：[ADR-0010](0010-data-sovereignty.md)（如有）—— 数据主权原则的具体落地
- 教训：**不要 fight PKM 社区**——Obsidian 等工具已经验证了这个模式有用，借鉴比重新发明便宜
