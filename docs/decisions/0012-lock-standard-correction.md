# ADR-0012 · v1.0 lock 标准修正：不阻挡 v1/v2 验证过的实用 gap

Status: Accepted
Date:   2026-05-01 (PM, same day as 0001-0011 + lock)

## 情境

ADR-0001..0011 + CORE_LOCKED.md + before-adding-feature.md 一起把 v1.0 锁定，立刻在同一天暴露问题：

用户跑通后立刻反馈："**初代没这些东西，真的太理论了，就一个理论框架，用户用的不舒服不说，还没有实用价值**"。

具体缺的功能：
- 多语言 keyword aliases（v2 keyword_alias.py 验证过）
- 黑名单（v1 验证过，用一周必然需要）
- hours_old 时效过滤（v1 验证过）
- 每组 enabled flag、多组抓取配置等

我（claude）面对这些"加新功能"的请求时，机械应用 lock 标准 + before-adding-feature 4 问的"问题 1：用 0 天"——卡住每个改进。**结果：用户被迫用一个明确不能用的工具，但 lock 文件高高挂着。**

这是工程美学战胜了实用主义的典型反模式。

## 选项

### A. 维持原 lock 标准（机械保守）
- ✓ 一致性
- ✗ 用户用不下去——v1.0 永远只是个文档艺术品
- ✗ 违背 lock 的本意（"防过度建造"，不是"防修复痛点"）

### B. 完全放开 lock
- ✗ 失去对抗 over-building 的护栏
- ✗ 回到"加层加层"的滑坡

### C. 修正 lock 标准——区分两种"加新功能"
- ✓ 保留 lock 对抗投机型新功能的力量
- ✓ 不阻挡 v1/v2 验证过的真实痛点修复
- ✓ 用户能真用上 + 哲学护栏仍在

## 决定

**选 C**。lock 标准重写为：

| 想加的功能 | lock 是否阻挡 |
|---|---|
| 想得不够清楚的新点子 | ✅ 阻挡 |
| 直觉驱动 / "看到别人有也想做" | ✅ 阻挡 |
| 投机型架构升级（Tauri / React 重写 / 微服务化） | ✅ 阻挡 |
| **v1/v2 验证过的实用 gap**（用户已经验证有真痛点） | ❌ **不阻挡——立刻补** |
| **实际用户反馈的具体使用场景痛点** | ❌ **不阻挡** |
| 修 bug | ❌ 永远不阻挡 |

before-adding-feature.md 4 问保留，但**问题 1（"用了多久"）的权重降低**——它不再是 hard gate。当问题 2（"想法从哪来"）有强答案（v1/v2 验证 / 真用户反馈），问题 1 不通过也可以做。

## 代价 / 后果

- 区分"投机型 vs 验证型"加新功能，需要持续判断力——不能机械应用
- 可能会被滥用（任何"加功能"都说成"v1 验证过"）—— 由 ADR + CHANGELOG 公开记录每次决策来约束

什么时候这条修正会被破坏：
- 如果 v1/v2 中的功能都被陆续 port 完，无可借鉴时——回到原 lock 标准
- 如果"实用 gap"被滥用（每个新功能都说是 gap）——重新评估

## 修正条款生效后的具体补回（已完成）

2026-05-01 PM 一次性补回 6 项 v1/v2 验证过的功能：

| # | 功能 | 来源 | 投入 |
|---|---|---|---|
| 1 | 多组抓取配置 | v1 per-location config 简化版 | ~150 行 |
| 2 | LLM keyword aliases | v2 `keyword_alias.py` 直接 port | ~200 行 |
| 3 | 每页 module help | 新加（onboarding UX） | ~200 行 |
| 4 | 黑名单 | v1 验证过 | ~100 行 |
| 5 | hours_old 时效过滤 | v1 验证过；jobspy 原生支持 | ~30 行 |
| 6 | 每组 enabled flag | v1 验证过 | ~30 行 |

加起来 ~700 行，跨多次 commit。**不动 14 张表 schema，不动 11 条核心 ADR**。

## 给将来的我自己

> **Lock is meant to defeat over-building, NOT to block fixing real pains validated by previous iterations.**

下次想加功能时先问：
- 这个想法是 v1/v2 验证过的吗？是 → 通常该补
- 这个想法是真用户反馈的吗？是 → 通常该补
- 这个想法是我自己直觉觉得"不错"？是 → **lock 卡住**，写到 IDEAS.md 等 24 小时

## 备注

- 触发场景：用户原话"咱们这个初代没这些东西，真的太理论了，就一个理论框架"——清醒提醒
- 与之绑定：[CORE_LOCKED.md 修正条款](../CORE_LOCKED.md#%EF%B8%8F-2026-05-01-修正lock-不是机械保守)
- 与之绑定：[before-adding-feature.md](../before-adding-feature.md)
- 这条 ADR 是**对自己工程美学的警醒** —— 工具不是给哲学家用的，是给真要找工作的人用的
