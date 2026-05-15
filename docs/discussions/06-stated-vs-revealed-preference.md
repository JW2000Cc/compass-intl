# Discussion 06 · 用户声称的偏好和真偏好不同

Date: 2026-05-01

## 引子

Compass 一开始假设用户的 `global_doc`（自由文本描述"我想找什么工作"）是**真偏好**。
这是个错觉。学术上有大量证据：**stated preference ≠ revealed preference**。

## 证据

[Frontiers 2025 综述](https://www.frontiersin.org/journals/artificial-intelligence/articles/10.3389/frai.2025.1660548/full)
和 [arxiv 2407.00082](https://arxiv.org/html/2407.00082v1) 都讲：用户**陈述的偏好**（stated）和**行为暴露的偏好**（revealed）经常矛盾，并且会随求职过程**自动漂移**——

> "an individual initially seeking a data engineer position adjusted their preferences to data analyst positions after encountering setbacks, and then refined their resume to better match those adjusted preferences."

用户碰壁后悄悄调整目标，然后改简历适应新目标——他自己甚至意识不到偏好变了。

## 三种典型分歧

| 用户声称 | 行为暴露 | 真相 |
|---|---|---|
| "想要 data analyst" | thumbs-up 全是 ML engineer 岗位 | 实际想做 ML 但不敢说 |
| "only Italy" | thumbs-up 全是 remote 岗位 | 实际是 location 不重要 |
| "5+ years 硬要求" | applied 接受了 3 年 offer | 真实门槛是 3 年 |

这些分歧用户**不会主动告诉系统**——因为他自己也没意识到。

## Compass 的核心反应

把"偏好"从 input 翻转为 output：

**旧设计**：global_doc 是 input → 系统按它过滤
**新设计**：global_doc 是历史 output → 系统对照行为找分歧 → 反过来教育用户

具体动作（部分实现）：
- 反思层每月生成"系统对你的 6 条假设"
- 每条假设带"证据"（来自 facts 或 reflection events）
- 用户审核——这一步本质上是**"声称 vs 行为"对照**
- 用户可以推翻假设——这条覆盖会进新的"声称偏好"

## 设计后果

### 有意识的"假装无知"

Compass 不能太信用户填的 global_doc。但也不能完全无视——那让 cold start 不可能。
正确姿态是：**初始相信，但持续质疑**。

### 给用户的反馈不是"你说错了"，是"我观察到这个"

错误：
> "你说想要 data analyst，但你 thumbs-up 全是 ML engineer——你说错了"

正确：
> "我注意到你最近 thumbs-up 的 8 个工作里有 7 个是 ML engineer，
> 0 个是 data analyst。你的 global_doc 还说想要 data analyst——
> 要不要更新一下？"

差别：第一种是审判；第二种是镜子。

### "矛盾"本身就是信号

如果用户声称 ≠ 行为，**那个 gap 就是反思素材**。Compass 故意暴露这种 gap，
不替用户解决它——让用户自己面对。

## 这条 discussion 落地到的具体设计

- `services/reflection_engine.py:stated_vs_revealed()` —— 显式计算这个 gap
- `services/reflection_engine.py:regenerate_assumptions()` —— LLM 从 facts + actions 出发，**不是从 global_doc 出发**
- ToolAssumption 的 user_override 字段——让用户能看到系统怎么理解他、并能反驳
- 反思层主页（`/reflect/dashboard`）显示 stated_vs_revealed 的简单计数

## 给后来人

如果你做任何"个性化"产品，记住：

> **用户填的偏好不是真偏好。用户的行为更接近真偏好。
> 但用户的行为也会被系统的偏置塑造——
> 所以连"行为"也不能照单全收。**

唯一稳定的 anchor 是**让用户自己看到声称 vs 行为的 gap，并自己决定怎么处理**。
这是 Reflective Flywheel 哲学的具体落地。
