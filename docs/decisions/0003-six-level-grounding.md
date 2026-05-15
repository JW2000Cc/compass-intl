# ADR-0003 · 简历改写用 6 层变换光谱（Claim Grounding）

Status: Accepted
Date:   2026-05-01

## 情境

简历改写最难的不是**让简历变好**，是**让简历变好但不过分**。
LLM 默认会美化过头——它训练数据里见过太多"夸张但有效"的 bullet。
单纯告诉它 "don't exaggerate" 不行，因为"轻微夸张"对 LLM 是模糊概念。

用户在讨论时给了两个例子：
- "德语初学者" → "German B1"——这是事实编造（绝不允许）
- "电力系统课程项目" → "在金融领域应用复杂网络系统量化建模方法"——这是合理的视角重构（如果用户当时确实想过）

这两个例子之间**有连续光谱**，但伦理边界**离散**——什么允许、什么不允许，需要明确分层。

## 选项

### A. 单一 prompt 警告 LLM 不要夸大
- ✓ 实现简单
- ✗ "不要夸大"对 LLM 是模糊词——它会自己定义边界
- ✗ 不可审计——用户无法判断每个 bullet 是 OK 还是越界
- ✗ 没有兜底——一旦 LLM 走过头，没有 deterministic 拦截

### B. Binary：保留原文 OR 重写
- ✓ 简单
- ✗ 浪费 LLM 能力——L1 同义改写明明可以自动通过
- ✗ 不解决"用户能接受到哪种程度"的差异

### C. 6 层变换强度光谱 + L5 deterministic 硬拦截
- ✓ 把"轻微 vs 过分"从模糊概念变成离散层级
- ✓ 用户的 calibration 直接对应"max_amplification_level"——你能接受到 L3，系统就停在 L3
- ✓ L5 用 regex/规则硬拦截，不依赖 LLM 自我克制
- ✓ 每个 variant 标注 level——用户审核时一目了然
- ✗ 6 层的边界判断需要训练（用户需要理解 L0-L5 的差别）
- ✗ LLM 偶尔会把 L2 和 L3 搞混——可接受，因为 L4-L5 是关键边界

## 决定

**选 C**。光谱定义：

```
L0  直接照抄         自动通过
L1  同义改写         自动通过
L2  隐含显化         用户一次确认
L3  视角重构         必须先访谈引导
L4  推断填充         必须用户审批
L5  事实编造         deterministic 硬拦截，永远不存
```

L5 拦截规则在 `claim_grounder.screen_for_l5`，是纯函数：
- 语言等级引入（`B1` / `native` 等不在原文里的词）
- 学位类型引入（`PhD` / `MSc` 不在原文）
- 标题升级（`participated → led` 这种动词跃迁）
- 敏感类目下的日期变化

`save_variant` 总会先跑 `screen_for_l5`，违反就 raise `ClaimGroundingError`，service 层不能 swallow。

## 代价 / 后果

- 每次 variant 多一次 LLM 调用（grounding 评估 level）—— 成本 +30%
- 6 层定义需要文档化（在用户看见的 UI 也要标 level）
- L5 规则是 regex——会有 false positive（比如简历里本来就有 "B1"，输出也有 "B1" 不应触发）。当前版本通过"new vs source"集合差判断，但仍有边缘情况
- L5 规则是 regex——也会有 false negative（LLM 用同义词绕过）。这是可接受的：adversarial voice 是第二道防线

什么时候会后悔：
- 如果 false positive 让用户觉得系统太死板→放宽 regex 或加 LLM-based 二次判断
- 如果 false negative 让事实编造漏过去→加更严格的语义检查

## 备注

- 实现：`compass/services/claim_grounder.py`
- 学术基础：`EvidenceRL` (arxiv 2603.19532) 把 evidence-based 比例从 18.8% 提到 41%
- 与之绑定：`ADR-0006 adversarial-voice-frozen`——Adversarial Voice 是第二道防线
- 与之绑定：`ADR-0005 facts-immutable`——L5 拦截依赖于"事实层"作为不可变基准
- 用户 insight 来源：那次"轻微捏造可不可以做"的对话——用户自己意识到边界离散
