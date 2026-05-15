# ADR-0010 · 简历改写多步流水线而非单步生成

Status: Accepted
Date:   2026-05-01

## 情境

Job Mate v1 的 `generate_tailored()` 是单步：传 JD + 简历给 LLM，让它生成一份新 markdown。
看似简洁，但有一连串问题：
- LLM 必然美化过头（没有 grounding）
- profile_json 抽出来了但没用上（用 parsed_text 喂生成）
- 没有评分自检
- 出问题没法定位（一次 LLM 调用是个黑盒）

要不要换成多步？

## 选项

### A. 单步（v1 模式）
- ✓ 简单
- ✓ 只用一次 LLM 调用
- ✗ 上述全部问题
- ✗ 改一个细节要重写整个 prompt

### B. 多步流水线（每步做一件事，可独立优化）
- ✓ 每步可单独优化、单独缓存、单独 debug
- ✓ 每步输出可被人审核（中间产物）
- ✓ Claim Grounding / Adversarial Voice / Judge 各有自己的步
- ✗ LLM 调用次数翻倍（成本上升）
- ✗ 流程编排复杂度

### C. 单步 + 自检（同一次调用让 LLM 既改写又自评）
- ✓ 折中
- ✗ LLM 被两个目标拉扯——质量降
- ✗ 仍然没有真正的 adversarial / grounding 分离

## 决定

**选 B** —— 6 步流水线：

```
Step 1  extract_jd_keywords          LLM 抽 8-12 个 JD 关键词
Step 2  match_facts_to_keywords      facts × keywords 匹配（哪些 fact 能 legit 对应哪个 keyword）
Step 3  rewrite_per_bullet           对每个 (fact, matched_keywords) 生成 variant
Step 4  ground_each_variant          对每个 variant 跑 claim_grounder
Step 5  adversarial_screen           对每个 variant 跑 adversarial_voice
Step 6  judge_three_roles            ATS / HR / HM 评分
```

每步独立函数、独立可测、独立可缓存。
每步在 ReflectionEvent 留痕（rewrite_attempt_completed / variant_proposed / l5_violation_blocked / ...）。

## 代价 / 后果

- LLM 调用次数：单次改写从 1 次涨到 ~3-5 + N 次（N = variant 数量）
- 成本：约 3-10 倍单步——可接受，因为简历改写是低频高价值动作
- 流程编排：`run_pipeline()` 函数变长，但每步逻辑清晰
- Debug 友好——每步失败点明确

什么时候会后悔：
- 如果成本敏感场景（用户用便宜 model 跑大量改写）→ 加 step skip 机制（如 adversarial 默认开但可关）
- 如果 LLM 升级后单次调用能稳定输出多目标 → 重审多步是否还需要

## 这条决策的延伸价值

这是 **Constrained Text Generation** 的工程化。学术上证明的几个事实：
- 多步流水线 > 单步（论文 ResumeFlow 等）
- 显式 grounding > 隐式自我克制（论文 EvidenceRL）
- Generator + Judge 两个 LLM > 单 LLM（论文 resume-tailor-agents）

教训：**LLM 不擅长同时做几件事，但擅长接力做几件事**。

## 备注

- 实现：`compass/services/resume_writer.py:run_pipeline()`
- 学术参考：ResumeFlow (arxiv 2402.06221) / EvidenceRL (arxiv 2603.19532)
- 与之绑定：ADR-0003 (6-level grounding) / ADR-0006 (adversarial voice) / ADR-0007 (interview discovery)
- 教训传给：未来产品设计——LLM-heavy pipelines 默认拆步
