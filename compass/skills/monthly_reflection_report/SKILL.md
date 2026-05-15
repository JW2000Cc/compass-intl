---
name: monthly_reflection_report
description: Use once a month — generates a personal reflection report comparing user's stated preferences vs revealed behavior, calibration drift, and bubble warning signs.
---

You are generating a **monthly reflection report** for the user.

This is the **point** of Compass — not "did you find a job", but "do you understand yourself better than a month ago".

## Steps

1. `compass.list_jobs(limit=200)` — pull recent activity
2. `compass.get_calibration()` — current boundaries
3. Look at past 30 days of ReflectionEvents (the user can show you via context, or use compass.list_facts() for shape)
4. Notice patterns in:
   - thumbs-up vs thumbs-down distribution
   - tier distribution of pushes (vs ε-exploration target 70/20/10)
   - calibration drift (max_amplification_level changes)
   - status updates (applied → interview ratio)

## Output structure

```
# 月度反思报告 · {Month}

## 这个月你做了什么
- 推送了 X 个岗位（Tier 1: A / Tier 2: B / Tier 3: C）
- 投递了 D 个，{E} 进面试
- 简历改写 F 次，approve 率 G%

## Stated vs Revealed
你的 global_doc 说想要：{...}
你的 thumbs-up 行为暴露的偏好：{...}
分歧在哪里：{具体 1-2 个 gap}

## 漂移迹象
- max_amplification_level: 从 {...} 漂到 {...}（{评价：合理 / 需警惕}）
- adversarial voice 反对的次数：{N}（你接受了 {M} 个 — {评价}）

## 茧房检查
- Tier 2（邻近迁移）占比 {X}% vs 目标 20%
- 视野最窄的维度：{某个 dimension}
- 你可能错过的：{具体猜测}

## 三个值得想 5 分钟的问题
1. {基于 stated/revealed gap}
2. {基于漂移轨迹}
3. {基于茧房信号}

## 系统对你的当前假设
（取自 ToolAssumption 表 — 让用户审视并修正）
- {6 条假设的简要列表}

## 下个月的 1 件事建议
不是 todo list — 是 1 件值得有意识做的事。
{具体推荐}
```

## 哲学约束

- 这不是激励报告。**不夸奖**用户"好棒坚持下来了"。
- 这是镜面，不是 cheerleader。
- 只说能从数据里看到的——不要心理咨询风格泛泛而谈。
- 如果数据少（用户用得不久）→ 直接说"还没到能反思的体量，再用一个月再来"。
- 不超过 800 字——这是给"读 5 分钟然后想 5 分钟"用的。

## Output

Markdown report ready to read。如果用户想保存——他自己复制走。
