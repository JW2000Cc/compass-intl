# ADR-0004 · Reflective Flywheel 而非 Engagement Flywheel

Status: Accepted
Date:   2026-05-01

## 情境

讨论到"让 Compass 学用户偏好"时，触发了一个警觉——这不就是又一个**信息茧房飞轮**？
- 用户 thumbs-up 多 → 系统更推那类 → 用户更看不到别的 → 偏好越来越窄
- 用户接受某种夸大 → 系统记住 → 下次默认更夸 → calibration 漂移

这是经典的 **Engagement Flywheel** 失控形态——TikTok / YouTube 推荐系统的标准毒化路径。

但完全不学习也不行——那 Compass 永远不会个性化。

我们需要一种**飞轮但不毒化**的形态。

## 选项

### A. 完全不学（每次 LLM 调用都从零开始）
- ✓ 没有飞轮 = 没有失控可能
- ✗ 永远不会个性化
- ✗ 用户每次教同样的事

### B. 学偏好（标准 RLHF 思路：用户 thumbs-up/down → 模型偏好微调）
- ✓ 个性化效果好
- ✗ Engagement Flywheel 的标准失控路径——视野收窄
- ✗ Sycophancy 风险——LLM 学会"用户喜欢哪种话术"

### C. 学边界（不是"你喜欢什么"，是"你接受到哪种程度"）+ 反向调节
- ✓ 学的是用户的伦理判断（max_amplification_level / sensitive 类目锁定）
- ✓ 这种学习不会窄化视野——只会让"什么是过分"更清晰
- ✓ 配合反向调节防漂移：slow drift（每次 ±1）+ adversarial voice（永远不学）+ 价值观回归按钮
- ✗ 需要清晰区分"偏好"和"边界"——前者别学、后者要学
- ✗ 用户得理解这个区别才不会感到困惑

## 决定

**选 C**——Reflective Flywheel 设计。命名上和 Engagement Flywheel 显式对照：

| 维度 | Engagement Flywheel | Reflective Flywheel |
|---|---|---|
| 系统观察 | 用户的偏好（thumbs / clicks） | 用户的边界决策（accept/reject variant） |
| 系统强化 | 用户当下倾向 | 用户对自己的清晰度 |
| 失控形态 | 信息茧房（视野收窄） | Calibration 漂移（边界滑坡） |
| 反向调节 | 难（要算法层面注入多样性） | 容易（用户每一步都在做判断，让漂移可见即可） |
| 健康终点 | 不存在（永远要黏住用户） | **用户毕业**（终有一天他不需要工具） |

具体落地（散在多个 ADR 里）：
- `ADR-0007 slow drift` — calibration 每次最多 ±1
- `ADR-0006 adversarial-voice-frozen` — 一个永不学习的"刚正声音"
- `ADR-0008 tool-assumptions-overridable` — 系统对你的假设可见可推翻
- 价值观回归按钮 — 一键清空学习

## 代价 / 后果

- "学边界不学偏好"这个区别需要明确传达给用户——通过 UI 文案、文档、交互设计
- 个性化效果会比 RLHF 慢——可接受，因为目标不同
- 用户需要理解 calibration 的 max_amplification_level 等概念——这是对用户的"信任投资"
- 系统对"用户毕业"是友好的——这反映在 metric 选择上（不追 retention 追"清晰度"）

什么时候会后悔：
- 如果用户反馈"calibration 学得太慢，每次还是要重教" → 加快学习速率，但要同时加强反向调节
- 如果产品形态出现迁移（不只做求职 → 做职业 OS） → 重审 Reflective Flywheel 是否还是最佳形态

## 备注

- 概念框架在用户那次"我闻到了一点飞轮的味道"对话中浮现
- 学术基础：Filter Bubble Systematic Review (arxiv 2307.01221)、Behavioral-Semantic Fusion (arxiv 2407.00082)
- 实现散布在：`calibration_learner.py`、`adversarial_voice.py`、`reflection_engine.py`
- 这是 Compass 哲学的**根本支柱**——其它 ADR 都是这一条的工程具象

## Update (2026-05-01) — 半自动化滑坡风险

讨论过浏览器扩展辅助投递时识别出一个新边界：**辅助 fill ≠ 自动 submit 的边界不在功能，在节奏**。

观察光谱（按用户介入度递减）：
```
L1 复制粘贴             ← 真 control
L2 浮窗建议 + 用户点采纳  ← 真 control
L3 一键填**单**字段       ← 真但开始稀释
L4 一键填**整表**         ← 表面 control，实际机械点击
L5 自动 fill + submit   ← 无 control
```

**任何"投递辅助"工具默认会被效率重力拉向 L4/L5**。Sonara/AIHawk 的"投得越多越好"哲学最终毒化用户判断力——投得多反而被 ATS 学习到 reject。

Compass 如果将来做扩展（已暂定不做，见 #11 backlog），必须**显式设计摩擦**：
- 每个字段单独审批（不允许批量）
- 显示对照（原 fact / 改写 / amplification level / adversarial 反对）
- submit 前强制 N 秒反思摘要
- 不激励、不徽章、不进度条
- 不模仿"人类节奏"——直接由人控制就是真节奏

**摩擦不是 UX 缺陷——摩擦是 calibration 数据质量的工程要求**。同时也是 LinkedIn 反自动化检测的工程要求（见 ADR-0006 同期讨论）：保留人类节奏 = 保留人类 control = 永远不会被识别为自动化。

这条边界在做扩展前必须先固化——所以记在 ADR-0004 里。
