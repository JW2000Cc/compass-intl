# CORE_LOCKED · v1.0 锁定声明

Date: 2026-05-01
Status: Active (with 2026-05-01 PM correction — see bottom)

> 这份文档锁住 Compass v1.0 的核心边界。
> 标记 🔒 的部分**不再扩**——只修 bug、跟依赖。
> 新功能必须走 plugin / skill / 独立项目，不能污染 core。

> ⚠️ **2026-05-01 修正**: lock 是为了**对抗过度建造**，不是机械保守。
> v1/v2 验证过的实用 gap **不应该被 lock 卡住**——见底部修正条款。

---

## 为什么要锁

Compass 在过去几次会话演化中从"求职过滤器"成长成了 ~5000 行的本地反思系统。
继续无边界添加新功能会：

1. **稀释设计哲学** — 每加一层 abstract 都可能违反某个 ADR（5-tier / 6-level / Reflective Flywheel / Facts immutable）
2. **增加维护负担** — 每加一个 service 都要配套 sibling sync / portability / 时区 / Win 兼容 / ADR
3. **拖累跑通节奏** — 4300 行代码至今没真跑过，再加只会越加越没法验证
4. **遗忘真正的 metric** — Compass 的成功不是功能数量，是"用户对自己的清晰度"

锁 v1.0 = 仪式性切换 = 强制把节奏从"加功能"切到"维护 + 跑通 + 用着"。

---

## v1.0 Core（🔒 不再扩）

### 数据模型（14 张表）
- IdentityVersion / ResumeFact / FactEvidence
- FactVariant / RewriteAttempt
- InterviewSession / InterviewQA
- ReflectionEvent / ToolAssumption
- UserCalibration / CalibrationDriftPoint
- Job / JobMatch
- DeletedRecord

任何 schema 变更都必须有 alembic migration + 走 ADR。

### 核心 service（不再加新功能）
- `tier_classifier.py` — 5-tier prompt
- `claim_grounder.py` — 6-level 光谱 + L5 deterministic 拦截
- `adversarial_voice.py` — 永远冻结的反对声音
- `calibration_learner.py` — slow drift 边界学习
- `interview_engine.py` — 访谈式发现
- `resume_writer.py` — 多步流水线
- `reflection_engine.py` — 漂移 + 工具假设
- `identity_engine.py` — 简历解析 + 事实抽取
- `scraper.py` — jobspy 包装
- `job_funnel.py` — 漏斗分析
- `llm.py` — provider 抽象（Claude/OpenAI 已支持，不再加）

### 核心 prompt（必须冻结）
- `tier_classifier.SYSTEM_PROMPT` — 5-tier 判断规则
- `claim_grounder.GROUNDING_SYSTEM` — 6-level 评估
- `adversarial_voice.ADVERSARIAL_SYSTEM` — **绝对冻结**（ADR-0006）

修改任何 frozen prompt 必须走 ADR。

---

## v1.0 已完成的 reference skill（最小生态）

✅ `compass/skills/ats_keyword_audit/` — ATS 关键词覆盖审计
✅ `compass/skills/cover_letter_gen/` — Cover letter 生成
✅ `compass/skills/company_brief/` — 面试前公司简报
✅ `compass/skills/monthly_reflection_report/` — 月度反思报告

剩下的（salary / transition / 其它垂直）走 plugin 路径——不进 core。

---

## 走 plugin / skill / 独立项目的（✅ 可以加）

以下任何想法**不要**进 core：

- 任何具体行业的 hard rule（ATS 评分细节 / 某 site 的 keyword 库）
- 任何"自动化"色彩的功能（自动投递 / 自动 follow-up）
- 任何 UI polish（漂亮主题 / 动画 / 移动端响应式）
- 任何一次性工具（数据导出脚本 / 配置迁移）
- 任何用户特化（vivi 的特殊偏好 / 某行业的术语库）

→ 全部走 `compass/skills/` 或独立 GitHub 项目。

---

## 明确不做（除非满足触发条件）

| 想做的事 | 触发条件（满足才考虑） |
|---|---|
| 浏览器扩展 | 找到第二个真用户 + 愿意做反例设计（每字段单审 / 不批量 / 反思摘要） |
| Tauri / 真程序打包 | 要分发给非技术用户 |
| 跨用户匿名集体智慧（路径二） | 有 ≥10 个真实用户 |
| Ollama 本地 LLM | 你真的想跑本地（M 系列 16GB+） |
| DOCX carry-over | HTML preview + 浏览器打 PDF 不够用 |
| 自动投递 | **永远不做**（ADR-0004） |
| 自己造 skill 引擎 | Anthropic API 涨价 5x 以上（ADR-0011） |

---

## v1.0 维护范围

下一阶段（3 个月内）只做：
- 跑通 + 修 bug（最高优先级）
- 跟依赖更新（每月看一次）
- 写测试（修 bug 时顺手补 case）
- 读 reflection events（用工具自我反思）
- 文档同步（代码改了文档要跟）
- **port back v1/v2 验证过的实用 gap**（见修正条款）

**不做**：投机型新功能 / 重构 / 美化 UI / 上 React / 做营销。

---

## 解锁条件

什么情况下应该考虑解锁、做 v2：

1. **使用半年以上** + 用户记忆里有"反复缺这个功能"的实证
2. **第二个真用户的反馈** + 多个使用场景共识
3. **某个 ADR 被实证证伪** —— 我们当时的判断错了，需要重做

不是"想到一个新主意"——是"用了 6 个月还是回到这条路"。

---

## ⚠️ 2026-05-01 修正：lock 不是机械保守

锁定 v1.0 后第一天就发现一个判断错误——把"用 0 天，等用一周再决定"应用到了 **v1/v2 验证过的真实痛点**（多语言 keyword expansion / blacklist / hours_old 时效过滤等）。结果：理论框架完整，实际用户用着不舒服，"没有实用价值"。

### 修正后的 lock 标准

| 想加的功能 | 是否被 lock 卡住 |
|---|---|
| 想得不够清楚的新点子 | ✅ 卡住——`before-adding-feature` 4 问会拦下 |
| 直觉驱动 / "看到别人做了我也想做" | ✅ 卡住 |
| **v1/v2 验证过的实用 gap**（用户已经验证有真痛点）| ❌ **不卡住——立刻补** |
| **实际用户反馈的实用问题**（有具体使用场景）| ❌ **不卡住** |
| 投机型架构升级（Tauri / React / 微服务化）| ✅ 卡住 |

### 这条修正引出的具体补回（已做完）

2026-05-01 PM 一次性 port back：

1. **多语言 keyword aliases** (`compass/services/keyword_alias.py`)——v2 验证过的功能，没它中文用户在德国搜不到工作
2. **多组抓取配置**——v1 的 per-location 关键词配置的简化版
3. **黑名单** (`compass/services/blacklist.py`)——v1 验证过，用一周必然需要
4. **hours_old 时效过滤**——v1 已经支持，不加就抓回 6 个月前过期岗位
5. **每组 enabled flag**——临时禁用一组而不删配置
6. **每页 module help blocks**——降低使用门槛

这些**不算违反 lock**——它们是修复 v1.0 设计时漏掉的实用 gap，不是投机型新功能。

### 给将来的我自己

> **Lock is meant to defeat over-building, NOT to block fixing real pains validated by previous iterations.**

下次想加功能时先问：
- 这个想法是 v1/v2 验证过的吗？是 → 通常该补
- 这个想法是真用户反馈的吗？是 → 通常该补
- 这个想法是我自己直觉觉得"不错"？是 → **lock 卡住**，写到 IDEAS.md 等 24 小时

---

## 给未来的我自己

如果 6 个月后你打开这份文档想加新功能：

> 先问自己 4 个问题（见 [docs/before-adding-feature.md](before-adding-feature.md)）。
>
> 通不过就别加。这不是技术问题，是承诺问题。

---

*"工具的最高荣誉，是被它的下一代取代——但下一代必须真的更好，不是只是更新。"*
