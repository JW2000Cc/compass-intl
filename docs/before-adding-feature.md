# 加新功能前的自我克制清单

Date: 2026-05-01
Status: 永久——每次想加功能时强制读

---

## 想加新功能？先回答这 4 个问题

### 问题 1：我用 Compass 多久了？

如果 < 1 个月：
> **STOP**。Compass v1.0 还没经过真实使用考验。再加功能 = 给一个未验证的产品堆未验证的功能。
> 至少用一个月，让 reflection events 自己告诉你哪里痛。

如果 ≥ 1 个月：继续 →

---

### 问题 2：这个想法是从哪儿来的？

填一个：
- [ ] **真实痛点** —— 我反复在 Compass 里做某件事，做着做着烦了
- [ ] **数据驱动** —— funnel.py / drift dashboard / reflection events 显示某个 gap
- [ ] **第二个用户反馈** —— 不是我自己想的，是别人说的
- [ ] **新东西的诱惑** —— 看到 Hacker News / GitHub 上有个酷东西，想加进 Compass
- [ ] **不写新代码就不舒服** —— 周末没事干，想 hack 点啥

如果选了后两个：
> **STOP**。这是好奇心驱动 ≠ 真实需求驱动。
> 写到 backlog/IDEAS.md，3 个月后回头看还想做不。

如果选前三个：继续 →

---

### 问题 3：能走 plugin/skill 吗？

如果新功能能写成：
- 一个 SKILL.md（让 Claude Code 通过 MCP 调）
- 一个独立 GitHub 项目（消费 Compass JSON Resume 输出）
- 一个第三方工具（用户自己装，Compass 不管）

→ **走 plugin / skill / 独立项目**。

如果非要进 core 才能做（影响 14 张表 schema / 改核心 prompt / 修反思层逻辑）：

→ 继续问题 4 →

---

### 问题 4：它破坏哪些 ADR？

逐条对照：

- [ ] [ADR-0001](decisions/0001-fork-not-inplace.md)：fork v2 而不在 v1 上原地改
  → 这个改动是不是大到该 fork v2 而不是改 v1？
- [ ] [ADR-0002](decisions/0002-five-tier-not-binary.md)：5-tier 替代 push/skip
  → 这个改动会让推送变回二元判断吗？
- [ ] [ADR-0003](decisions/0003-six-level-grounding.md)：6 层变换光谱
  → 这个改动会让 LLM 跨过 L5 硬地板吗？
- [ ] [ADR-0004](decisions/0004-reflective-vs-engagement-flywheel.md)：Reflective vs Engagement
  → 这个改动会引入"自动化便利稀释判断"的滑坡吗？
  → 这个改动会增加 retention 但减少 reflection 吗？
- [ ] [ADR-0005](decisions/0005-facts-immutable-variants-mutable.md)：Facts immutable
  → 这个改动会让 facts 变成可改吗？
- [ ] [ADR-0006](decisions/0006-adversarial-voice-frozen.md)：Adversarial Voice 冻结
  → 这个改动会让 adversarial 学习用户偏好吗？
- [ ] [ADR-0007](decisions/0007-interview-driven-discovery.md)：访谈式发现
  → 这个改动会跳过访谈直接编造事实吗？
- [ ] [ADR-0008](decisions/0008-tool-assumptions-overridable.md)：用户可推翻假设
  → 这个改动会让画像变黑盒吗？
- [ ] [ADR-0009](decisions/0009-reflection-event-stream.md)：事件流
  → 这个改动会绕过 ReflectionEvent 写决策吗？
- [ ] [ADR-0010](decisions/0010-multi-step-resume-pipeline.md)：多步流水线
  → 这个改动会让简历改写退回单步生成吗？
- [ ] [ADR-0011](decisions/0011-claude-only-skill-ecosystem.md)：Claude-only skill
  → 这个改动会重新引入内置 skill 引擎吗？

任意一条破坏：
> **STOP**。要么不做，要么先写一份新 ADR 解释为什么这次该破坏既有决策。
> 写新 ADR 强迫你想清楚 trade-off——大多数时候写到一半就发现"算了别做了"。

没破坏任何 ADR：

→ 检查 [docs/CORE_LOCKED.md](CORE_LOCKED.md) 的"明确不做"清单，看是否在里面。

不在 → 可以做。

---

## 如果通过了 4 个问题

恭喜——你的想法通过了反思滤网。

但还有最后一步：**写到 CHANGELOG.md** 之前先写到 `docs/IDEAS.md`，等 24 小时再开工。

24 小时后还想做 → 真做。
24 小时后忘了 → 当时只是冲动。

---

## 这份清单的 metric

如果你**通过了所有问题**——你应该感到：
> "我必须做这件事，不做我难受，不是因为它酷"

如果你只是觉得"嗯，看起来 OK 可以做"——
> 你大概率没真的过这套问题。回去再读一遍。

---

*Compass 的护城河不在功能数量，在每次"不做"的克制。*
