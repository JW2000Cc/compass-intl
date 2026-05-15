# Compass · 设计哲学总览

> 这份文档是给"想理解为什么 Compass 这样设计"的人看的——
> 不是给"想用 Compass"或"想改 Compass"的人。
> 那两件事看 README 和 IMPLEMENTATION_NOTES。

---

## 一句话

**Compass 不是更好的求职过滤器，是把求职工具从"主语"改回"宾语"的尝试。**

- 主语形态："系统帮你过滤 → 你接受推送"——你是宾语
- 宾语形态："你提问 → 工具回答 → 你决定"——工具是宾语

---

## 三个根本切换

### 1. 从过滤到反思

**Job Mate**: "少看噪声"——把不合适的裁掉。
**Compass**: "看清自己"——你看不见的那部分，比噪声更重要。

二者不是优化关系，是**不同物种**。你把过滤器做得再好也不会变成反思系统。

### 2. 从学偏好到学边界

**RLHF 标准做法**: 学用户喜欢什么 → 越来越像用户喜欢的样子 → 茧房 / sycophancy。
**Compass**: 学用户接受到哪种程度（max_amplification_level）→ 让用户对自己的边界更清晰。

学偏好让用户**消失**到推荐里。学边界让用户**显形**为决策者。

### 3. 从用户黏性到用户毕业

**SaaS metric**: retention / DAU / churn 是失败。
**Compass metric**: 用户三个月后对自己的理解比三个月前清晰了多少。

用户最终毕业 = 成功。这反过来限制了所有的"加个功能让用户离不开"诱惑。

---

## 组件 → 防什么 → 对应 ADR

每个组件存在的目的都是**防一个具体的失控**：

| 组件 | 防什么 | 对应 ADR |
|---|---|---|
| **5-tier 模型** | push/skip 二分丢失 90% 信息 | [0002](decisions/0002-five-tier-not-binary.md) |
| **Tier 5 hard ban** | "工程师 → 调酒师" 类乱推 | [0002](decisions/0002-five-tier-not-binary.md) |
| **Tier 2 配额 20%** | 视野收窄到核心圈（茧房） | [0002](decisions/0002-five-tier-not-binary.md) |
| **6-level grounding** | LLM 默认美化过头 | [0003](decisions/0003-six-level-grounding.md) |
| **L5 deterministic 拦截** | LLM 自我克制不可靠 | [0003](decisions/0003-six-level-grounding.md) |
| **访谈式发现** | 工科生事实"不足"是挖掘问题，不是真不足 | [0003](decisions/0003-six-level-grounding.md) |
| **Reflective Flywheel** | Engagement Flywheel 的茧房失控 | [0004](decisions/0004-reflective-vs-engagement-flywheel.md) |
| **Slow calibration drift** | 边界滑坡（max_level 漂移） | [0004](decisions/0004-reflective-vs-engagement-flywheel.md) |
| **价值观回归按钮** | 累积漂移 | [0004](decisions/0004-reflective-vs-engagement-flywheel.md) |
| **Facts immutable** | 多 variant 之间互相矛盾 | [0005](decisions/0005-facts-immutable-variants-mutable.md) |
| **FactEvidence provenance** | 不可审计的 LLM 输出 | [0005](decisions/0005-facts-immutable-variants-mutable.md) |
| **Adversarial Voice (frozen)** | Sycophancy 腐蚀判断标准 | [0006](decisions/0006-adversarial-voice-frozen.md) |
| **ToolAssumption 用户可推翻** | 黑盒画像 | (未来 ADR-0008) |
| **ReflectionEvent stream** | 失忆——一切可追溯 | (未来 ADR-0009) |
| **fork v2 而不在 v1 改** | 旧产品定位的肌肉记忆 | [0001](decisions/0001-fork-not-inplace.md) |
| **访谈式发现** | 90% 是挖掘问题，不是事实不足 | [0007](decisions/0007-interview-driven-discovery.md) |
| **ToolAssumption 用户可推翻** | 黑盒画像 / 无觉知依赖 | [0008](decisions/0008-tool-assumptions-overridable.md) |
| **ReflectionEvent stream** | 失忆——一切可追溯 | [0009](decisions/0009-reflection-event-stream.md) |
| **多步 resume pipeline** | 单步 LLM 同时做 N 件事质量崩 | [0010](decisions/0010-multi-step-resume-pipeline.md) |
| **Claude-only skill 生态** | 复刻二流元认知层 | [0011](decisions/0011-claude-only-skill-ecosystem.md) |
| **lock 标准修正** | 把 lock 用成机械保守 → 工具变成哲学论文 | [0012](decisions/0012-lock-standard-correction.md) |
| **多组抓取配置** | 共享 keywords 在多语言市场失效 | (port back from v1, no ADR) |
| **多语言 keyword aliases** | 单语言关键词搜不到外语职位 | (port back from v2, no ADR) |
| **黑名单（公司+关键词）** | 重复噪声浪费 LLM 调用 | (port back from v1, no ADR) |
| **hours_old 时效过滤** | 抓回 6 个月前过期岗位 | (port back from v1, no ADR) |
| **每页 module help** | 用户进入 abstract 页面茫然 | (UX polish, no ADR) |
| **JSON Resume 导出** | 自己造 PDF 渲染器是闭门造车 | (no ADR; uses jsonresume.org open standard) |
| **Gmail 状态自动检测** | funnel 数据靠手动改 = 永远不闭环 | (no ADR; relies on jobspy + LLM classification) |

### 高层讨论（不绑定单个决策）

ADR 是"具体代码后果的决策"。还有一些**贯穿多个决策的高层思考**——它们没法塞进单个 ADR，
但塑造了整个 Compass 的方向。这些放在 [`docs/discussions/`](discussions/README.md)：

| # | 标题 |
|---|---|
| [01](discussions/01-flywheel-taxonomy.md) | 飞轮不是一种东西，是一个家族 |
| [02](discussions/02-fast-vs-slow-feedback.md) | 反馈周期决定一切 |
| [03](discussions/03-user-graduation.md) | 用户毕业是成功，不是 churn |
| [04](discussions/04-paths-2-and-3.md) | 路径二（匿名集体智慧）+ 路径三（开放 skill 生态） |
| [05](discussions/05-anti-bubble-strategies.md) | 反茧房不是反推荐——是约束化探索 |
| [06](discussions/06-stated-vs-revealed-preference.md) | 用户声称的偏好和真偏好不同 |

---

## 三句箴言

> **Job Mate 是"加功能让推送更准"。Compass 是"加约束让用户保持自主"。**
> 两件事在代码上都是"加"——但思路相反。

> **A compass shows direction, not destination.**
> 它指方向，不指目的地。所以你必须自己走。

> **工具的最高荣誉，是被它的下一代取代。**
> Job Tracker 让我看到关键词的局限；Job Mate 让我看到二分类的局限；Compass 是这两次反思的累积产物。
> 三个月后，如果 Compass 也被某个更好的东西取代了——那是好事。

---

## 这份文档怎么使用

- **想做新功能** → 先看这份 + 相关 ADR，问自己"这是加约束还是加诱惑"
- **想改设计** → 先看 ADR，理解为什么是这样——可能你的"改进"已经被讨论过
- **想 fork 出 v3** → 这份是 v2 的精神画像，复制不是目的，理解之后**重新做选择**才是

---

*同时存在的还有：*
- *`README.md` — 给新用户的简介*
- *`IMPLEMENTATION_NOTES.md` — 给开发者的实现细节*
- *`反思与延伸.md` — 给三个境界一起看的演化故事*
- *`docs/decisions/` — 所有 ADR*
