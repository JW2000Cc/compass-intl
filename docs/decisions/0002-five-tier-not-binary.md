# ADR-0002 · 用 5-tier 替代 push/skip

Status: Accepted
Date:   2026-05-01

## 情境

Job Mate v1 的 matcher 输出 `{push: bool, reason: str}` —— 二元判断。
跑了几个月后发现两个根本问题：

1. **二元抛弃了 90% 信息**——一个边缘但可能的岗位和一个完美匹配，在 push 这一边没有任何区别。前端按 `created_at desc` 排，相当于按抓取顺序而不是相关度。
2. **"为什么是这个 push 决定"无法验证**——LLM 给出 `push=true` + 一句 reason，用户没法判断"它是真的觉得我能干，还是判断不准"。

我们要找一个**保留信息密度 + 可解释 + 不茧房**的替代结构。

## 选项

### A. 加 score（仍是二元 push/skip + 0–10 score）
- ✓ 实现最简单——在现有 prompt 加一行
- ✗ score 还是单一维度——一个 score=7 的"邻近迁移"和一个 score=7 的"核心专业"都在同一个排序桶里
- ✗ 不解决"反推荐反到艺术生岗位"的茧房问题——score 不区分"客观可达 vs 不可达"

### B. 多维度 score（如：技术匹配 / 角色匹配 / 地点 / 公司 / 待遇 各 0–10）
- ✓ 信息密度高
- ✗ LLM 输出多维 score 不稳定——常见看 LLM 给 "技术 8 / 角色 8 / 待遇 7" 但其实它就是把整体感觉拆几份
- ✗ UI 复杂度大——用户看 5 个数字反而难做决定
- ✗ 仍然不解决"客观可达圈"问题

### C. 5-tier 分类 + tier 内 score
- ✓ tier 是**类型判断**，比单一 score 更稳定（LLM 能稳定区分 "这是邻近迁移" vs "这完全无关"）
- ✓ score 退化为 tier 内细分，作用清晰
- ✓ Tier 5 = 客观不可达的 hard 标签，正面对应"工程师 ≠ 调酒师"教训
- ✓ Tier 2 = 邻近迁移 = 反茧房甜点区——把"反在点子上"做成显式的 70/20/10 配额
- ✓ Tier 3 自然带 learning_gap 字段——"差什么 + 多久能补"
- ✗ 5 个 tier 的边界判断需要明确的 system prompt
- ✗ LLM 偶尔会把 Tier 1 和 Tier 2 搞混（这是可接受的）

## 决定

**选 C**——5-tier 分类 + tier 内 score。

Tier 定义：
- Tier 1: 核心专业圈（直接对口）
- Tier 2: 邻近迁移圈（反茧房甜点）
- Tier 3: 可学习扩展（带 learning_gap）
- Tier 4: 远距迁移（仅强信号才推）
- Tier 5: 客观不可达（永远静默）

配额：70% / 20% / 10% / 0 / 0。Tier 2 占 20% 是 ε-exploration 的具象化——故意不让 Tier 1 占满。

## 代价 / 后果

- system prompt 变长（要严格定义 5 个 tier 的判断规则）
- 每次 LLM 调用 token 量增加约 30%
- 用户需要适应"按 tier 分组"而不是"按时间排序"
- Tier 边界偶有错判（Tier 1 vs 2 容易混）——可接受，让用户手动重分类
- LLM 不可用时默认 Tier 5（refuse-to-push）—— **这是有意的姿态**：宁可不推也不瞎推

什么时候会后悔：
- 如果用户使用一段时间后发现 Tier 2 推送的命中率系统性低于预期 → 配额可能要调整为 80/15/5
- 如果 LLM 模型升级后变得能稳定输出多维 score → 可以重审"为什么不用方案 B"

## 备注

- 实现：`compass/services/tier_classifier.py:SYSTEM_PROMPT`
- v1 教训："工程师 ≠ 调酒师"是 Tier 5 hard ban 的直接动因
- 与之绑定：`ADR-0007 calibration slow drift`（Tier 4/5 的"远距/不可达"判断不应被用户偏好快速覆盖）
- 与之对照：传统搜索引擎追求 ranking，Compass 追求 typing——这是**反搜索引擎**的设计
