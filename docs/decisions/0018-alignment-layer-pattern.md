# ADR-0018 · 对齐层模式（Alignment Layer Pattern）

Status: Accepted
Date:   2026-05-07

## 情境

ADR-0015 用三个独立 service 解决跨语言、字面 ATS、UI 术语三类用户需求：

  - `variant_translator` — element 级翻译，把 LLM 输出对齐到简历 locale
  - `native_ats_check` — 字面 + fuzzy 命中，把 LLM 不擅长的字面规则做掉
  - `user_facing_translator` — 把开发者术语 (L0-L5b / verb / metric) 对齐到普通话

事后看，这三个 service 共享一个**未被命名的设计原则**：

  - 用户需求不进 LLM prompt
  - LLM 输出后用代码做一次对齐
  - "对齐"针对的是**用户主权领域**：格式 / 语言 / 表达层级 / 字面规则

接下来 retain（按用户原 DOCX 模板生成简历）也是这个形态——LLM 给 plain text，
对齐层把它塞进用户的格式骨架。

如果不把这个原则形式化，每次新功能都要重新论证一遍"要不要改 prompt"。
形式化之后给一个**判断标尺**：先问能不能用对齐层，能就用，不能再考虑动 prompt。

## 选项

### A. 不形式化，只把 retain 当独立 service

- ✓ 不用写 ADR
- ✗ 下次类似问题（比如"按公司名调整 tone"、"按招聘方文化调整正式度"）又要重新论证
- ✗ ADR-0015 决议的根本原则散落在 line 38 否决的方案里，没明确写出来

### B. 形式化为「对齐层模式」（本 ADR）

- 把 LLM 输入边界 / 代码对齐边界 / 用户决策边界三者关系写清楚
- retain 当作第 4 个对齐层落实
- 给未来新功能一个判断流程

### C. 推迟，等更多对齐 service 出现再形式化

- ✗ Compass 已经有 3 个对齐 service，再积累价值边际递减
- ✗ retain 这次落实没标尺会重蹈覆辙（v3 时代的 template_inspire 就是因为没标尺，做了 70% 卡在最后一公里——"DOCX 模板暂时不能在 Compass /export 渲染"）

## 决定

**选 B。**

### 原则陈述

> **任何"用户需求"，都先问一句：能不能在 LLM 输出之后用代码对齐？能就用对齐层，不能再考虑动 prompt。**

### LLM / 代码 / 用户三方边界

| 类别 | 谁负责 | 例子 |
|---|---|---|
| 措辞、视角整合、语义判断 | LLM | bullet rewrite / claim grounding / adversarial objection |
| 格式、语言渲染、字面规则、locale 选择 | 代码（**对齐层**） | element-level 翻译 / 字面 ATS 命中 / DOCX 模板填充 / UI 术语翻译 |
| 内容采纳、方向选择、最终决策 | 用户 | review 页 accept/reject、模板上传、section 映射确认 |

### 对齐层的形态约束

一个合格的对齐层必须满足：

1. **输入是 LLM 已经产出的内容**——对齐层不能改写 LLM 的判断
2. **输出仍然忠实于 LLM 的语义**——对齐只动表达形态（格式/语言/呈现），不动结论
3. **不调 LLM**（少数特殊情况除外，如 element-level translation 的 connective tissue）
4. **可单独测试**——给定输入有确定输出，不依赖 LLM 调用
5. **失败时降级到不对齐**——对齐失败回到 LLM 原始输出，不阻塞主流程

### 已实施的 4 个对齐层

| 对齐层 | 解决的"用户需求" | LLM 输出形态 | 对齐后 |
|---|---|---|---|
| `services/variant_translator.py` | 跨语言 | English polished bullet | 简历 locale 渲染（zh/it/fr/...） |
| `services/native_ats_check.py` | JD 字面命中 | LLM 不知道 ATS 怎么扫 | 代码字面 + fuzzy 比对 |
| `services/user_facing_translator.py` | UI 用普通话 | L0-L5b / verb / metric 术语 | "原文照搬" / "换说法意思一样" |
| `services/template_inspire/*` + `themes.render_docx_theme` | 用户原 DOCX 格式 | plain text bullets | 塞回用户 DOCX 段落骨架，字体/间距全保留 |

### 给未来新功能的判断流程

新需求来了 → 走以下 3 步：

```
1. 这个需求是不是「格式 / 语言 / 表达层级 / 字面规则 / locale 选择」？
   ├── 是 → 走对齐层路径（参考已有 4 个的形态）
   └── 否 → 进入步骤 2

2. 这个需求是不是「对内容本身的判断（采纳/拒绝/方向）」？
   ├── 是 → 这是用户决策，不要让 LLM 替用户做。在 UI 暴露选项 + 写 ReflectionEvent
   └── 否 → 进入步骤 3

3. 这个需求是不是「LLM 必须知道的上下文（事实 / 简历 / JD）」？
   ├── 是 → 进 LLM prompt（这时改 prompt 是合理的）
   └── 否 → 重新评估这个需求是不是真的需求
```

### 反向价值：审计已有代码

形式化后能回头审计 v4 哪些地方违反了这个原则：

- `extract_jd_keywords` LLM 抽——本身没问题（"keyword 抽取"是无标准答案的语义任务）
- `match_facts_to_keywords` LLM 给 JSON 索引——这其实可以**部分**走对齐层（字面/fuzzy 命中代码做、语义命中 LLM 做）；现在两个混在一起。**未来重构候选**。
- `judge_resume` LLM 评分——保留（评分需要语义判断，不能字面对齐）

这些复盘是 ADR-0018 的副产品。

## 代价 / 后果

- 工作量：本 ADR 仅 30 分钟落地，但**约束所有未来新功能的设计选择**
- 维护成本：每次有人要改某 LLM prompt 时，必须先论证为什么不能用对齐层
- 文档同步：未来新对齐层入位时要回来更新本 ADR 的"已实施"表格

## 这条决策的延伸价值

- **LLM 边界划分**的一次形式化——不止 Compass，所有 LLM-heavy 产品都面临"哪些事 LLM 做、哪些事代码做、哪些事用户做"的边界问题
- 业界共识："**多 LLM 不等于多视角**"（ADR-0015 line 14）——独立性来自输入异构。对齐层是输入异构的实现
- 教训传给：未来任何 LLM 输出后处理的设计——优先考虑代码对齐而不是 prompt fitting

## 备注

- ADR-0017（in-place vs parallel）刚补的 in-place 重做，**也是这个原则的隐含应用**——把 v4 的 fork-blueprint 拍平回 jobs.bp，相当于"模块边界对齐用户的认知模型"，不是"为新功能新建命名空间"
- 配套实施：retain 路径 v4 已有 70%（template_inspire），本 ADR 落地的同时把最后一公里接通（routes/exports.py /export/docx + themes.render_docx_theme）
- 未来 ADR：当对齐层数量 > 6 时，考虑是否抽出 `compass/services/aligners/` 目录统一组织；目前 4 个分散是合理的
