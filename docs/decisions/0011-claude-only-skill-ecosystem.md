# ADR-0011 · 放弃内置 skill 引擎，聚焦 Claude Code Skill 生态

Status: Accepted
Date:   2026-05-01

## 情境

之前规划过双路径 skill 系统：
- 路径 A：Claude 用户走 MCP server + Claude Code Skill
- 路径 B：其它 provider 走 Compass 内置 skill 引擎（自己实现 SKILL.md 加载、dispatch、prompt 渲染）

实施完路径 B 的 MVP（skill_engine.py + routes/skills.py + 一个示例 skill）后，回头评估发现路径 B 是**复刻一个二流的元认知层**——做不到 Claude Code Skill 的"LLM 自己决定何时激活"。

## 选项

### A. 保留双路径（之前的决定）
- ✓ 所有 provider 都能用 skill
- ✗ 路径 B 永远比路径 A 差一档（被动 vs 主动激活）
- ✗ 双倍维护负担（两套 skill 加载机制）
- ✗ 产品定位模糊（用户不知道哪条路是"主推"）

### B. 只保留 Claude 路径
- ✓ 集中投入做透一条路
- ✓ 产品定位清晰：用 Compass 推荐用 Claude
- ✓ Skill 定义文件（SKILL.md）仍然中性——格式遵循 Claude Code Skill 标准
- ✗ 非 Claude 用户失去 skill 那一层（但 Compass 核心：matcher / claim grounder / 简历改写仍可用）

### C. 完全不做 skill
- ✓ 最简单
- ✗ 失去"开放生态"路径，长期没有杠杆

## 决定

**选 B**。删除路径 B 的代码（skill_engine.py / routes/skills.py / templates/skills/），保留：

1. `tools/mcp_server.py` —— MCP server 暴露 6 个 compass.* tool
2. `compass/skills/<name>/SKILL.md` —— 作为 reference skill 模板（用户复制到 ~/.claude/skills/ 用）
3. `docs/SKILLS.md` —— 简化为 Claude-only 教程

非 Claude 用户体验：
- 启动时 base.html 顶部友好横幅提示"core 能用，skill 生态需要切到 Claude"
- `.env.example` 顶部说明推荐 Claude 的原因
- **不强制**——核心功能仍可在所有 provider 上跑

## 代价 / 后果

- 失去"我们支持所有 provider"的话语权——产品定位变成"Claude-first"
- 非 Claude 用户感觉被"区别对待"——但 Compass 的 metric 不是市场份额，是质量
- 如果将来 Claude 出问题（API 涨价 / 关停 / 政策变化）——skill 生态绑定 Claude 是单点风险

什么时候应该重审：
- Claude API 涨价 5x 以上 → 也许该重做内置引擎
- 出现真正能复刻"LLM 元认知层"的开源标准 → 转向那个标准
- 第二个用户用非 Claude → 那时再评估值不值得做路径 B

## 备注

- 实现：删除 `compass/services/skill_engine.py` / `compass/routes/skills.py` / `compass/templates/skills/`；保留 `tools/mcp_server.py` + `compass/skills/`
- 与之绑定：[ADR-0004](0004-reflective-vs-engagement-flywheel.md) Reflective Flywheel——MCP 让 LLM 自己决定何时激活，本身就是元认知能力的体现
- 教训：**不要复刻一个二流的元认知层**——某些事情 Anthropic 生态确实做得更好，认这个差异比硬要"中立"价值高

---

## Amendment · 2026-05-02

撤回 `find_hiring_manager` + `outreach_email_gen` 这两个 skill。理由如下，**这是对原 ADR 适用边界的更精细判断，不是推翻**：

### 为什么撤回

经验证，**找内推 / warm outreach 是真高频核心需求**（朋友实证：回应率比 ATS 冷投高 15-30 倍）。这意味着：

- 它**不是** Compass 边缘的 nice-to-have
- 它是 Compass 应该作为**核心功能**直接提供的能力——和 tier 分类、简历改写一个量级
- 把它放在 Skill 路径上 = 锁给装了 Claude Code 的开发者，**对 Compass 真实目标用户不可用**（朋友这种使用群没人去装 npm CLI）

### 适用边界判断

ADR-0011 原文里讲的"skill 生态" 适用于：
- **元认知 / 偶发需求**：ATS 关键词审计、面试前公司简报、月度反思生成、薪资谈判演练
- 这些**不是日常**，每周 0-1 次，对装 Claude Code 的用户来说完全合理

但 warm outreach **不属于这一类**：
- **每个值得投递的 job 都该问一遍"我有没有人能联系"**
- 频率 = 跟 thumbs / 状态变更同级，不该有任何门槛
- 必须**核心内嵌**，与 Compass UI 一体

### 替代方案

ADR-0013（待写）将定义 connection-graph-native 路径——从用户自己的 LinkedIn export / 校友 / GitHub network 出发，本地 SQLite 存储，jobs detail 页直接显示"你认识的人在这家公司"。无 Claude Code 依赖，所有 LLM provider 用户都能用 Compass 已有的 LLM client 起草邮件。

### 当下状态

- `compass/skills/find_hiring_manager/` —— **已删**（2026-05-02）
- `compass/skills/outreach_email_gen/` —— **已删**（2026-05-02）
- 替代实现：**待开发**，跟用户讨论清楚 V1 范围后再启动

### 这次撤回的元教训

**Skill 路径是好工具，但不该用它装载核心高频功能**——只装"边缘 + 偶发 + 元认知"那一类。一旦发现某个 skill 是日常核心，应立即把它从 skill 路径**移出**进核心。这是和原 ADR 互补的边界规则。
