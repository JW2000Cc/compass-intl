# ADR-0015 · Reflective UI: Structured Decomposition + User-Facing Language Layer

Status: Proposed
Date:   2026-05-06

## 情境

ADR-0010 把简历改写做成 6 步多步流水线（extract_keywords → match → rewrite → ground → adversarial → judge），每步 LLM 接力。
半年实战暴露 4 个结构性问题，**不是单步 prompt 调优能解决的**：

### 问题 1 · LLM 评估端跟生成端共谋
Grounder / Adversarial / Judge 三个验收 LLM 跟 Generator LLM 同源训练分布——它们对"听起来专业的措辞"有共谋偏好。
润色越重（"architected" / "leveraging" / "cutting-edge"），三个 LLM 都倾向给好评。
**多 LLM 不等于多视角**——加更多 LLM 层是递归扩张同源偏差，不是引入独立性。

### 问题 2 · 跨语言场景 grounding 失效
当用户投意大利岗，JD 是意大利语，LLM 默认会把简历输出成意大利语——但用户 fact 是英文，绝大多数翻译相关判断（数字校验、专有名词、grounding ladder 阈值）在英文域调好了，跨语言时 ladder 整体上移一档（无 L0/L1 通道——任何翻译都是视角重构）。
跨语言 ATS 字面命中跟语义 grounding 是两个不同维度，但当前流水线把它们混在一个 judge LLM 里评。

### 问题 3 · 数字 / 实体的 deterministic 校验粒度
当前 L5 = 编造，硬拦截。但实践中 LLM 抽样飘成"22% 替代 23%"——这是渲染漂移（rendering drift），不是用户编造。
现状把渲染漂移和实质编造塞进同一标签，UX 像审判用户。
应拆成 L5a 渲染漂移（数字微差/拼写偏移，自动 snap 回 fact 值）vs L5b 实质编造（凭空出现的实体，硬拦截）。

### 问题 4 · "对用户的判断"藏在 LLM 黑箱里
Grounder 判 L3 时，用户看到一个分数，背后凭什么不是 L2 是个黑箱。
这跟 ADR-0004 (Reflective Flywheel) 的哲学层根本矛盾——Reflective 要求"系统对用户的判断必须可见可推翻"。
**当前 6 步流水线的 LLM 配比是 Engagement 形态（多 LLM 帮你过关），不是 Reflective 形态。**

### 问题 5 · "判断归用户"的隐藏前提
即使我们把判断从 LLM 拽回用户，UI 里如果用 L0-L5 / amplification_level / verb / metric 这些**开发者术语**呈现，
用户面对一堆陌生标签盲选 = 还是黑箱，只是黑箱从 LLM 移到了"我点了 ✓ 但不知道为啥"。
真正的"判断归用户"要求**呈现给用户的语言他能直接理解**——这是 UX 写作维度。

## 选项

### A. 修正主义补丁
- 各步 prompt 里加 "OUTPUT LANGUAGE: English" / 加 fuzzy threshold / 加用户标黄
- ✓ 1 周完成
- ✗ 治标不治本——LLM 共谋偏差仍在，黑箱判断仍在

### B. 结构归属重划（本 ADR）
- 重新划分 LLM / 代码 / 用户三种角色的判断范围
- LLM 只做"语言皮"（动词候选 / 串句 / connective tissue）
- 代码做"事实骨"（数字 / 实体 / 命中 / 字面 ATS）
- 用户做"评价"（采纳 / 拒绝 / 触发访谈）
- 配套：fact 升 element schema、bullet 拆五维菜单、用户语言转换层

### C. 推迟
- 现状能用，等大模型升级后看是否自然解决
- ✗ 大模型升级解决不了"判断归属"问题——这是产品哲学不是模型能力

## 决定

**选 B。** 这次升级的核心**不是加新功能**，是**重新划分 LLM 在产品里的边界**。

### B 的具体形态

#### 1. Fact 升 element schema（沿 ADR-0005 Facts immutable）
ResumeFact.structured_json 内承载 element 拆分：
```python
{
  "verb":     {"canonical": "built", "en": "Built", "it": "Sviluppato"},
  "metric":   {"value": 23, "unit": "%", "display": {"en": "23%", "it": "del 23%"}},
  "object":   {"en": "ML pipeline", "it": "pipeline ML"},
  "org_canonical": "Siemens",        # locale-invariant，永不翻译
}
```
- 不加列（complement structured_json，向后兼容）
- 老 fact 渐进迁移（首次访问时 fact_decomposer 自动填充）
- `metric.value` / `org_canonical` 不进 LLM
- 缺 locale **不静默 fallback**——标 `requires_interview: True` 触发访谈

#### 2. Bullet 五维结构化菜单
原 polished bullet 仍渲染（用户仍能看到完整句），但同时生成 `BulletDecomposition`：
- 动作词（verb）：候选 + grounding 标签
- 数字成果（metric）：fact 字面校验
- JD 想要的能力（jd_keyword）：字面比对，缺则触发访谈
- 长度（length）：deterministic 规则
- 语气（tone）：候选 + grounding

每条带 grounding level + 来源（代码 / LLM）+ 三选一动作（用 / 保留原样 / 重写）。

#### 3. Grounder L5a/L5b 拆分
deterministic numeric/NER 校验独立成模块（移出 LLM 路径）：
- L5a：数字 Levenshtein > 0.9 / 字面接近 → auto-snap 建议
- L5b：fact 池里完全不存在的实体 → hard-block

#### 4. Adversarial Voice 改吃 fact + JD
ADR-0006 frozen 原则保留（prompt 内容不被反馈训练），但**输入结构改**：
- Phase 1：拿 fact + JD 生成 `expected_objections` 列表（独立事实立足点，不看润色文本）
- Phase 2：才把 polished bullet 喂进，问"上述 objections 是否被 variant 规避了？规避手段是事实还是话术？"

#### 5. Variant 翻译层（按 element 翻 + 语法串句）
不"翻译整段 bullet"——按 element 翻：
- `metric.value` / `org_canonical` 直接保留
- `verb / object` 取 locale 版本
- LLM 调用只做"用 connective tissue 把 elements 串成自然句"

#### 6. 原语言 ATS 字面扫描（deterministic）
翻译后跑 native_language_ats_check：
- 抽 JD 原语言关键 token
- 在目标语言简历里字面 + fuzzy 命中
- 输出 coverage 数 + missing list（不调 LLM）

#### 7. 用户语言转换层
所有用户 UI 文字必须经过 user_facing_translator：
- L0-L5 → "原文照搬" / "换说法意思一样" / "原文暗示明说出来" / "换角度讲" / "添了 fact 没说的细节" / "fact 里完全没有"
- amplification_level / verb / metric → 不暴露给用户
- "Accept / Reject / Dismiss" → "用这条" / "保留原样" / "AI 别管这点"
- 默认普通话模式，专家模式 toggle 才显示原始数据

## 代价 / 后果

- 工作量：~10 个新 .py 文件、~5 个修改 .py 文件、~3 个新 template、修订 ADR-0006、新增 1 ADR
- LLM 调用结构改变：从"6 次大调用"变成"~10 次细粒度调用"，但每次更短
- UI 复杂度上升（结构化菜单 panel + 用户语言层）——但 panel 默认折叠/sticky rail，主预览仍是 polished bullet
- 数据库：FactVariant 加 `decomposition_json` 列、`translated_json` 列；启用 alembic（baseline + 1 revision）
- ADR-0010 **不撤回**——多步流水线方向正确，本 ADR 是它的细化（哪些步归 LLM、哪些归代码、哪些归用户）
- ADR-0006 **不撤回**——frozen 原则保留，但发布 amendment 写清楚 input 改造的边界
- ADR-0005 **加固**——fact 不可变 + element-per-locale = field-level localization（Sanity 风格）

## 这条决策的延伸价值

- 这是 **"LLM 边界划分"** 的一次具体形态——其他 LLM-heavy 产品也面临同样问题：哪些事 LLM 做、哪些事代码做、哪些事用户做
- 通用原则：**LLM 适合无标准答案的事（措辞、视角整合）；有标准答案的事（数字、字面命中、规则判断）必须代码做；评价类判断必须用户做**
- 业界共识：**多 LLM 不等于多视角，独立性来自输入异构**
- 教训传给：未来任何 LLM-评估-LLM-输出 的设计——评估端必须有 generator 看不到的独立锚点

## 备注

- 实施在 Compass_v4（fork from Compass）
- 原 Compass 保留作 30 秒回退安全网
- 不做 shadow 双路径模式（单用户 + 已备份 = 真实使用反馈优于 shadow 数据）
- 配套修订：ADR-0006 amendment（adversarial input 改造）
- 学术参考：参见 v4/docs/discussions/07-llm-boundary-recalibration.md（待写）
- 跟 ADR-0009 (ReflectionEvent stream) 配合：BulletDecomposition 每个维度的用户决策都要写 ReflectionEvent
