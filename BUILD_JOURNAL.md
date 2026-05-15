# Compass · Build Journal

> 不是 changelog 流水账，是"想了什么、绕了哪些弯、落地选哪条、踩了什么坑"。
> 给未来的自己（和未来要看代码的 AI）留条线索。

---

## 2026-05-03 · 沉淀工作流改造（FactRevision append-only）

**问**：用户在简历改写里点 ✓/✎ 后想"把它定下来当成简历的一部分"——简历该怎么版本化？

**绕弯**：第一版直接 mutate `ResumeFact.text` + 写 `ReflectionEvent` 记录。能用，但用户看不到、不可逆、违反 immutability 注释。第二版升级到 IdentityVersion 整简历快照（GitHub branch 模型，每次沉淀建新版本，HEAD 切换）—— 写一半发现是子弹打蚊子：用户的真实需求是**单条事实迭代**，不是整简历分叉。整版本机制是 over-engineering。

**落地**：fact-level `FactRevision` 表，4 种 kind（origin / promoted_from_attempt / rolled_back / manual_edit）。每条 fact 自己有 history（像 `git log path/to/file.py` 而不是 `git checkout branch`）。append-only — 没有 delete API。

**教训**：
- **"删了之后跑哪去了？"是判断 footgun 的最好探针**。用户原本要的是 delete，但他自己提出"删 v2 之后基于 v2 的 v3 该怎么办"——立刻自我证否。这正是 git rebase 难的根本原因。
- 设计版本化系统时，先想清楚"撤销"的语义。撤销 = append-only 的反向操作（写新行）而不是 destructive 的 delete。Notion/Google Docs/git revert 都用这个模式。

---

## 2026-05-03 · 简历 review UI 改 Claude-in-Word 风格

**问**：原 review 页每个 variant 一张大 card，每条变换底下都有反馈控件——用户原话："这每条改写都有个 ui 复杂程度都好高"。

**绕弯**：本想给反馈控件加折叠。后来意识到问题不是"控件多"是"信息组织错"——用户其实想看**整简历变成什么样了**而不是逐条 diff。

**落地**：左 iframe 实时 preview（应用了 ✓/✎ 的状态）+ 右 sticky 紧凑 rail（每条变换 compact card）+ 底部 1 个反馈 widget。可拖分隔条调左右占比。

**踩坑**：第一版用 HTMX `hx-swap="outerHTML"` 配服务端 302 redirect，结果整页被塞进单 card div（"界面套界面"）。HTMX 跟"重定向式 API"天然不兼容——它把 redirect 跟着走然后 swap 整页内容。

**教训**：HTMX 用前查后端 response 行为。要么写专门的 partial 端点，要么用 native form POST + redirect。两可选时 prefer native（少一层依赖）。

---

## 2026-05-03 · 「再跑一次」要不要继承用户决定

**问**：用户已经 ✓ 了 5 条，想看不同角度——点「再跑一次」该全部重生还是保留已批准？

**绕弯**：最初实现是无脑全重生。用户反应是"我刚才那些工作都白做了"。

**落地**：`run_pipeline` 加 inheritance — ✓/✎ 的 fact 跳过重生（保留旧 variant），✗ 的 fact 把 rejection_reason 当 negative prompt 喂给 LLM 让重生有方向感，pending 的正常重生。modal 里 explicit 写出"保留 X / 替换 Y / 重生 Z"破坏性数。

**教训**：
- **"再跑一次"按钮是用户对当前结果不满 + 但已积累了部分判断**——保留判断、只重生未决的，是默认。"全推倒"是异常路径不该是默认。
- 拒绝理由不复用是浪费——negative prompt 让重生有方向感而不是 LLM-temperature 抽奖。

---

## 2026-05-03 · 单条 ↻ 重生 vs 整轮重跑

**问**：用户对 1 条不满意要不要走整流水线？

**绕弯**：原来只有"重跑整流水线"一个粒度——杀鸡用牛刀。

**落地**：每条 pending variant 卡片加 ↻ 按钮 → `/resume/variant/<id>/regenerate`。复用最新 attempt 的 keywords + 这条 fact 历次 rejected text 当 negative。在 variant 内 in-place 替换（保留 variant.id 让 URL anchor 不动）。

**教训**：粒度匹配用户实际决策粒度。Notion AI / Cursor 都是"段落级重生"——人在大文本里看到一句话不爽不会想推翻整篇。

---

## 2026-05-03 · 删除某次沉淀按钮

**问**：用户想要"编辑或删除某次沉淀"。

**绕弯**：第一反应是给 delete API。用户自己提出场景——"如果第一段后半改了一次，然后整段重写一次，再后又改前半，删中间整段重写那次该回到哪？"

**落地**：**没有 delete 按钮**。只保留 ↺「设为当前」——本质是 append 一条 `rolled_back` 新 revision。文档里专门写了一段"为什么不能删"，避免用户重复期望。

**教训**：用户说他想要 X 不等于真要 X，先让他描述具体场景。这次自我证否得很干脆。

---

## 2026-05-03 · LLM 调用全局 timeout

**问**：网络故障时 LLM 调用会无限挂。

**落地**：`services/llm.py:_chat_claude/_chat_openai` Anthropic + OpenAI client 加 `timeout=60.0`。retry 循环已存在，timeout 是把每次单次调用封顶。

**教训**：每个外部 API 调用都该有 timeout，是 default-on 不是 nice-to-have。

---

## 2026-05-03 · graduating UI（入门链接条件渲染）

**问**：之前为了砍 nav 把入门删了。用户："那是闭环必不可少的"——对的，新用户找不到入口。

**落地**：`onboarding_done` context_processor 注入到所有模板。nav 里 `{% if not onboarding_done %}` 包裹入门链接。完成的用户看不到，新用户看到醒目蓝色 CTA。`/insights` 顶部也加 banner 兜底。`/settings` 留入口供老用户重跑。

**教训**：导航元素的可见性该跟用户阶段挂钩。GitHub 的"add a README"、profile 完成度提示都是这个 pattern——新用户引导 + 老用户清爽，不互相干扰。

---

## 2026-05-03 · 关于 docs 的 docs（这份本身的存在理由）

**问**：CHANGELOG 越写越像流水账，每个 bug 都列下来后失去信号噪声比。

**落地**：保留 CHANGELOG 记录 *what changed*，新建这份 BUILD_JOURNAL 记录 *why I went this way and what I learned*。两者读者不同：CHANGELOG 给"想知道版本变了什么"的人，BUILD_JOURNAL 给"想理解项目灵魂"的人。

**教训**：文档分层。一个文件想 serve 两种读者，结果两边都不舒服。

---

## 长期教训（rolling notes）

- **测试新功能前先想"用户问哪句话能证否它"**——比写测试快十倍发现设计问题
- **每加一个按钮先想撤销路径**——"加" 容易 "去" 难
- **append-only 是默认路径**，destructive 要有强理由
- **"沉默" 比"假装" 更诚实**——LLM 没真信息时少说不要硬编故事
