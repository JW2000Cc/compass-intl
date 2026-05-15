# Discussion 04 · 路径二（匿名集体智慧）+ 路径三（开放 skill 生态）

Date: 2026-05-01
Status: 未实现，但应该走

## 引子

讨论 Compass 长期方向时识别出两条**还没做但应该做**的路径，分别突破两个不同瓶颈。

---

## 路径二：匿名集体智慧

### 它解决什么

单用户的反思有一个根本盲点——**你不知道你的"漂移"是个人特殊还是普遍现象**。

例：你过去 3 个月 thumbs-up 全是 startup 岗位，thumbs-down 全是大公司——
- 是你真的偏向 startup（个人偏好）
- 还是市场上 startup 招聘量在增加（市场信号）
- 还是 Compass 的某个偏置导致只推 startup（系统漂移）

单用户视角无法区分这三种情况。需要**和"和你 profile 类似的人"对照**。

### 为什么 LinkedIn 不做

LinkedIn 的商业模式三层：
1. Premium 个人订阅
2. **Recruiter 订阅**（主流，~65%）
3. **B2B 广告**

后两层都需要实名身份——招聘方要联系真人，广告主要 target 真实 profile。
**匿名数据无法变现**——招聘方/广告主都不会买。

所以 LinkedIn 看不上"匿名职业洞察"这块——但这块对个人用户极有价值。

### 为什么 Compass 反而能做

- Compass 是**本地工具**，不需要变现
- "不卖你的数据 + 用聚合数据让你受益" 是付费产品做不到的
- 完全自愿（用户可以选择不参与聚合）

### 技术路径（按隐私强度递增）

1. **简单聚合**：用户自愿周期性上传**匿名 profile 摘要**（hash 后的关键 skill / 经验年限分布 / 申请的岗位 role）→ 中央服务器做统计 → 返回"你的同类"对比
2. **联邦学习**：用户本地训练 user vector，只上传梯度
3. **同态加密 / MPC**：连服务器都看不到原始数据

但说实话——**Layer 1 的"自愿摘要上传"已经够了**。Compass 用户量级远没到需要联邦学习的规模。

### 用户能看到什么

```
你的 profile 在最近 6 个月：
  📊 同类比较（基于 137 个相似 profile）
  - 你被 push 的 Tier 2 岗位 / 同类: 18% / 35%（你的少 — 系统可能偏保守）
  - 你的 thumbs-up 命中 Tier 1: 62% / 同类 78%（你的低 — 边缘探索更多）
  - 你这个方向的 thumbs-up 命中率今年比去年: -12%（市场在凉）

  💡 同类选择（参考）
  - 同类人最多跳到的 5 个公司: ...
  - 同类人最近回报最高的 3 个 skill: ...
```

### 落地依赖

- 一个简单的中央服务（Flask + Redis 即可，不需要复杂基础设施）
- 一份匿名化协议（hash + k-anonymity 至少 k=10）
- 一个用户同意流程（默认关闭，opt-in）

---

## 路径三：开放 Skill 生态

### 它解决什么

Compass 的核心能力（反思层 / tier matcher / claim grounding）已经稳定。
但用户的求职需求是 long-tail 的：
- 意大利劳动法咨询
- 薪资谈判模拟
- 面试公司情报简报
- 转行决策辅助
- 月度反思生成器

这些每个都很有价值，但没一个值得 Compass 核心团队花时间做——**长尾需求适合社区做**。

### 为什么 Obsidian 是最好的对照

Obsidian 是**软件历史上最成功的"小核心 + 巨型 plugin 生态"案例**。
- 核心团队 < 10 人
- 2750+ 社区插件
- 核心保持极简 5 年没怎么变
- 靠 plugin 把"Markdown 编辑器"变成 second brain 平台

Obsidian 做对的几件事：
1. **核心保持极简**
2. **plugin API 极薄**——暴露 vault / 编辑器 / UI 注入点几个核心抽象
3. **plugin 在用户机器上跑**——不需要中心审查
4. **核心团队不和 plugin 竞争**——任何 plugin 都不会被官方"收编后弃用"
5. **社区先验证再核心采纳**——plugin 当 prototype

### Compass 怎么走

**核心保留极简**：抓 + 过滤 + 推送 + 漏斗追踪 + 反思层。这就是 "Compass Obsidian 内核"。

**暴露 4 个抽象**：
```
1. 数据 API:    读 jobs / facts / variants / actions
2. LLM API:    复用用户已配置的 key
3. UI 注入:    在 funnel / job detail / settings 注册按钮和面板
4. 事件钩子:    on_job_classified / on_status_changed / on_thumbs_down
```

**每个 skill 是一个目录**：`SKILL.md`（描述 + prompt 模板）+ 可选 `handler.py`。
装载时自动注册路由和钩子。

### 一个有意思的现实——你已经在 skill 生态里

用户已经在用 Claude Code 的 skill 系统。Compass 完全可以**寄生在这套已有的基础设施上**：
- skill 用 Claude Code skill spec
- Compass 提供 "compass.*" 命名空间的工具
- 任何 Claude Code skill 可以调用这些工具
- 用户装了 Compass 后，所有相关 skill 自动激活

**这意味着不需要从零造一套 plugin 系统**——只要把 Compass 的内部 API 包装成 MCP server 或 tool。
极小投入、极大杠杆。

### 可能的早期 skill

- ATS 关键词审计 skill
- 薪资谈判模拟 skill
- 公司情报简报 skill（5 分钟）
- 意大利劳动法咨询 skill
- 转行决策辅助 skill
- 月度反思生成器 skill
- LinkedIn 自动写消息 skill

每个 skill 是独立的 GitHub 仓库 / Claude Code Skill 包。

---

## 路径二 vs 路径三

二者解决不同瓶颈：
- 路径二：突破"单用户视角"——加横向（同类人）维度
- 路径三：突破"核心团队带宽"——加深度（垂直领域）维度

理想情况二者都做，不互斥。但优先级上：**路径三比路径二容易，建议先做**。

## 这条 discussion 不绑定具体 ADR——但应该绑定未来的

未来如果走这两条路，应该有：
- ADR-FUTURE: 匿名聚合协议设计
- ADR-FUTURE: skill 接口规范
- ADR-FUTURE: 数据上传同意流程

写下来，等到该做的时候。
