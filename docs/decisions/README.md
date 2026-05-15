# Architecture Decision Records · ADR

> 每个 ADR 是一份**短文档**，记录 Compass 一个重大设计决策的"为什么"。
> 不是教程、不是 README、不是文档——是**思考轨迹的快照**。

## 为什么有这个目录

代码告诉你"是怎么做的"。注释告诉你"这一行做什么"。
**ADR 告诉你"为什么是这个做法，不是别的做法"。**

软件演化时，代码会变、注释会过期、README 会重写。**但"当时为什么这样选"的决策上下文如果没记录，就永远丢了**。后来人（包括未来的你自己）只能猜。

## 格式

每个 ADR 用同一个模板：

```
# ADR-XXXX · <一句标题>

Status: <Accepted | Superseded by XXXX | Deprecated>
Date:   YYYY-MM-DD

## 情境
<这个决策出现时的 pain point 是什么>

## 选项
<考虑过哪几个方案，各自的吸引力 / 局限>

## 决定
<最终选了哪个，一句话说清>

## 代价 / 后果
<接受了什么代价、未来可能怎么后悔、什么时候应该重审>

## 备注 (可选)
<引用、对照、相关 ADR>
```

## 现有 ADR

| # | 标题 | 状态 |
|---|---|---|
| [0001](0001-fork-not-inplace.md) | Fork v2 而不在 v1 上原地改 | Accepted |
| [0002](0002-five-tier-not-binary.md) | 用 5-tier 替代 push/skip | Accepted |
| [0003](0003-six-level-grounding.md) | 简历改写用 6 层变换光谱 | Accepted |
| [0004](0004-reflective-vs-engagement-flywheel.md) | Reflective Flywheel 而非 Engagement Flywheel | Accepted |
| [0005](0005-facts-immutable-variants-mutable.md) | 事实层不可变，变换层可变 | Accepted |
| [0006](0006-adversarial-voice-frozen.md) | Adversarial Voice 永远不学习用户偏好 | Accepted |
| [0007](0007-interview-driven-discovery.md) | 访谈式发现而非 LLM 单方面改写 | Accepted |
| [0008](0008-tool-assumptions-overridable.md) | 工具假设暴露 + 用户可推翻 | Accepted |
| [0009](0009-reflection-event-stream.md) | ReflectionEvent 事件流（轻量 Event Sourcing） | Accepted |
| [0010](0010-multi-step-resume-pipeline.md) | 简历改写多步流水线而非单步生成 | Accepted |
| [0011](0011-claude-only-skill-ecosystem.md) | 放弃内置 skill 引擎，聚焦 Claude Code Skill | Accepted |
| [0012](0012-lock-standard-correction.md) | v1.0 lock 标准修正：不阻挡 v1/v2 验证过的实用 gap | Accepted |

ADR 之外，**贯穿多个决策的高层 essay** 在 [`../discussions/`](../discussions/README.md)：
- 飞轮分类、反馈周期决定论、用户毕业哲学、路径二/三、反茧房四层、stated vs revealed

## 怎么加新 ADR

写新决策时，复制 `_template.md` 改即可。不强求完美——**未完成的思考也比不写好**。

## 不要做的事

- ❌ 不要把 ADR 写成长篇论文（>200 行就该拆）
- ❌ 不要在代码改了之后改 ADR——ADR 是**当时的快照**，不是 living doc
- ❌ 不要写"显然的"决策（"我们用 Python"不需要 ADR）
- ❌ 不要描述实现细节（那是代码的活）—— ADR 只回答 "why"
