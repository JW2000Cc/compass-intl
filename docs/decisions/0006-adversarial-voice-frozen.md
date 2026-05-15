# ADR-0006 · Adversarial Voice 永远不学习用户偏好

Status: Accepted
Date:   2026-05-01

## 情境

Reflective Flywheel（ADR-0004）让 calibration learner 从用户的 accept/reject 决策学边界。这个学习有个**不容易察觉**的失控形态：**Sycophancy 学习**。

机制：
- LLM 学到"用户喜欢哪种话术"
- 慢慢偏向"听起来很 polished 但实际更虚"的方向
- 不是事实挖深，是话术更滑

这不是 calibration drift（边界滑坡），是**判断标准本身**被腐蚀。slow drift 防不住——它防的是"max_amplification_level 跳变"，但 sycophancy 是同 level 内的 quality 滑落。

我们需要一个**永远不被用户偏好驯化**的判断声音。

## 选项

### A. 完全相信 calibration learner
- ✗ Sycophancy 风险——上面的失控形态
- ✗ 没有任何"不会被腐蚀的"判断标准

### B. 让 calibration learner 慢一点 + 加严格 cap
- ✓ 缓解 drift
- ✗ 但 sycophancy 是 level 内 quality 问题，不是 level 跳变，cap 也防不住

### C. 加一个"永远不学"的 adversarial voice
- ✓ 提供反 sycophancy 的硬抗体
- ✓ 像免疫系统——主路可以学习适应，但有个独立模块永远是从中性立场出发
- ✓ 不阻塞——只提出反对意见 + 显示给用户，用户保留决定权
- ✗ 多一次 LLM 调用（成本 +30%）
- ✗ 需要配置 enable/disable 开关（成本敏感时可关）

## 决定

**选 C**。具体设计：

1. `services/adversarial_voice.py:ADVERSARIAL_SYSTEM` 是**冻结**的 system prompt
2. **永远不被任何 user feedback 训练**——这条规则在代码注释里明示
3. 它只看：原始 fact + 候选 variant + JD——不看用户 calibration、不看历史决策
4. 输出：`{"objection": "..."}` 或 `{"objection": null}`
5. objection 存在 `FactVariant.adversarial_objection` 字段
6. UI 显示 objection 给用户**但不阻塞接受**——用户保留决定权
7. 默认开启，可在 `.env` 里 `ADVERSARIAL_VOICE_ENABLED=false` 关掉（成本敏感时）

## 代价 / 后果

- 每个 variant 多一次 LLM 调用（+30% 成本）
- 用户偶尔会觉得 adversarial 的反对"太死板"——但这是有意的：它就是要刺激
- 维护时**必须警惕**——任何"让 adversarial 更友好" / "让它学用户偏好"的诉求都要拒绝
- adversarial prompt 一旦修改要走 ADR——不能随手改

什么时候会后悔：
- 如果 99% 的 objection 都是 false positive → 调整 prompt（但仍不学用户偏好）
- 如果用户每次都忽略 objection → 这不是 bug，是 feature——objection 是给用户的信息，不是阻塞
- 如果模型升级让 objection 质量大幅下降 → 重审 prompt 但保持冻结原则

## 备注

- 实现：`compass/services/adversarial_voice.py`
- 概念来自免疫学的"中央免疫耐受"——主体能识别自我但保留对抗自我的"反向"判断
- 与之对照：calibration_learner.py（**会**学习的部分）—— 二者构成 yin-yang
- 哲学根基：ADR-0004 Reflective Flywheel 的反向调节四件套之一
- 用户体验：`templates/resume/review.html` 用 ⚠ 视觉显式标出 adversarial objection

---

## Amendment 2026-05-06 (per ADR-0015) — 输入结构改造

### 触发问题

ADR-0015 在审视 Compass v4 改动时识别出本 ADR 冻结原则没覆盖的盲点：本 ADR 冻结的是 *prompt content*（对抗者的"价值观"不被反馈训练），但 *输入结构* 仍是
`(source_text, variant_text, jd_excerpt)` —— adversarial LLM 总是同时看到 fact 和**润色后的 variant**。

研究背景（参见 ADR-0015 §问题 1）：

- LLM-as-judge 系统性受 stylistic bias 影响（"Justice or Prejudice?" arxiv:2410.02736 / "Self-Preference Bias" arxiv:2410.21819）
- 同源训练分布的多个 LLM 对"听起来专业的措辞"有共谋偏好（D3: Debate Deliberate Decide arxiv:2410.04663）
- 真正的独立判断需要 *输入异构*，而不是模型数量

也就是说：当 adversarial LLM 直接看到 polished bullet，它会被措辞欺骗——和 grounder / judge 同向偏差而不是反向。这违背了本 ADR 第一段定义的"antibody"角色。

### 改造（保留 frozen 原则，只动输入结构）

`raise_objection()` 改为**两阶段**调用：

```
Phase 1  EXPECTED_OBJECTIONS_SYSTEM
   inputs: fact + JD only        ← variant_text 不在视野内
   outputs: list of fact-anchored expected objections

Phase 2  ADVERSARIAL_SYSTEM (本 ADR 原 prompt 的角色，重写为接受 expected_objections)
   inputs: fact + JD + Phase-1 expected_objections + variant_text
   outputs: { objection, addressed_by_facts, addressed_by_wording }
```

关键不变量：
- **Phase 1 的输入只有事实层**，对抗者在这一阶段拥有 generator 看不到的独立视角
- **Phase 2 把 Phase-1 输出锚定在事实层**，再判断 variant 是用"事实"还是"措辞"应对
- 两个 system prompt **均仍 frozen**——不被任何用户反馈路径修改
- **未引入新概念到对抗者价值观里**——只是把"判断要从哪个起点出发"显式化了

### Amendment 影响范围

- `compass/services/adversarial_voice.py`：完全重写为 two-phase
- `compass/services/resume_writer.py:451` 和 `:611` 两处 caller 签名更新（从 `source_text=..., jd_excerpt=...` → `fact=..., job=...`）
- `tests/test_adversarial_voice_v2.py`：新增 6 个测试，含 *Phase 1 不见 variant_text* 的硬不变量
- `tests/test_integration_baseline.py:test_raise_objection_signature_v2_post_adr_0015`：替换原 baseline 测试

### Frozen 原则的更准确表述

经此次反思，本 ADR 的 frozen 原则**显式补充**：

1. **Prompt content frozen** — 对抗者的"价值观"不被任何反馈训练修改（**原始原则**，不变）
2. **Input structure must isolate adversarial perspective** — 对抗者的 *判断起点* 必须有 generator 看不见的内容（fact 层 + 独立 phase），否则 frozen prompt 也会被 stylistic bias 渗透（**新增原则**）

未来任何对 adversarial 链路的改动都要满足这两条。
