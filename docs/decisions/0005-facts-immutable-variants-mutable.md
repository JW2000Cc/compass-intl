# ADR-0005 · 事实层不可变 + 变换层可变（Git 模型搬到简历）

Status: Accepted
Date:   2026-05-01

## 情境

简历改写工具的常见架构：用户上传一份简历，每个职位 fork 一份 tailored 版，每份独立。

但 Reactive Resume / 各种 SaaS 有一个共同问题：**多个 tailored 版本之间会互相矛盾**。
- 在 v1 简历里写 "5 years of experience"
- 在 v2 简历里因为某个 JD 要求强一点，改成 "6+ years"
- 在 LinkedIn 上写 "5 years"
- 半年后用户自己都搞不清哪个是真的

这不是用户故意撒谎——是**没有 single source of truth** 导致的渐进漂移。

## 选项

### A. 单份简历（v1 模式）
- ✓ 一致性强（只有一份事实）
- ✗ 没法 tailor——所有 JD 用同一份
- ✗ 改一次就回不去了

### B. 多份独立 tailored 简历（Reactive Resume / Teal 模式）
- ✓ 可 tailor
- ✗ 长期不一致（前面那个问题）

### C. Git 模型 — Facts 是 main，Variants 是 branches
- ✓ 事实层只能"创建新事实"或"停用旧事实"——永远不可变
- ✓ 每份 tailored 简历是事实的不同**呈现**（顺序、措辞、强调），不是事实的不同**版本**
- ✓ 加新经历 = 加新事实，自动 propagate 到所有 variants
- ✓ Claim Grounding 的 source-of-truth 一直是事实层
- ✗ 数据模型复杂（需要 IdentityVersion + ResumeFact + FactVariant 三层）
- ✗ UI 需要清楚区分"改事实"和"改 variant"

## 决定

**选 C**——三层数据模型：

```
IdentityVersion ─┬─ ResumeFact (immutable atomic facts) ─┬─ FactVariant (per job)
                 │                                        ├─ FactVariant (per job)
                 └─ FactEvidence (provenance)             └─ ...
```

规则：
1. ResumeFact 永远不会被 UPDATE——要"修改" = `active=False` + 创建新 fact 在新 IdentityVersion
2. FactVariant 是"呈现层"——可以多次改写、用户接受/拒绝/编辑
3. Variant 不存在脱离 fact——总是绑定到一个 source fact_id
4. Claim Grounder 在创建 variant 前总是对照原 fact 检查 L5 规则

## 代价 / 后果

- 数据模型从 v1 的"扁平 jobs + artifacts" 变成三层——schema 复杂度上升
- 用户需要理解"改事实"和"改 variant"的区别——通过 UI 文案传达
- 每次"上传新简历"会创建一个全新的 IdentityVersion——不是 update 现有的——这是有意的
- 历史 IdentityVersion 永远不删——为漂移分析保留

什么时候会后悔：
- 如果用户感觉"事实不可变太严格" → 提供"修正事实"快捷按钮（实际上是 deactivate + create）
- 如果数据膨胀（每次上传都新增一份完整 IdentityVersion）→ 加冷数据归档机制

## 备注

- 实现：`compass/models/core.py` 的 IdentityVersion / ResumeFact / FactEvidence / FactVariant
- 灵感来源：Git 的 commit/branch 模型 + Datomic 的 immutable database
- 与之绑定：`ADR-0003 6-level-grounding` 依赖事实层作为不可变基准
- 与之绑定：`ADR-0006 adversarial-voice-frozen` 也读事实层做对照
