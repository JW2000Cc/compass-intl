# Change Template (solo dev 的"PR description"自查表)

> **每次开始一个新的编辑会话前**，在脑里或新建一份临时文件走一遍这个清单。
> Solo dev 缺"对手方"——这份模板是把自己变成自己的对手方。
> 不是 ceremony，是 5 分钟逼自己想清楚。

---

## 这次改什么

<一句话>

## 为什么

<根本动机：用户报的、自己发现的、连锁修的、定期清理的>

## 风险面（哪些既有功能可能被波及）

按优先级列至少 3 个**可能被改坏的**既有功能（不是新加的）：

- [ ] <既有功能 1>
- [ ] <既有功能 2>
- [ ] <既有功能 3>

如果列不出 3 个 → 你对系统理解不够，别动手，先去看代码 30 分钟。

## Sibling pattern 扫过了吗

- [ ] 这次的修改/bug 是**已知 pattern**（docs/bug-patterns.md 里的 P-XXX）的另一处实例吗？
- [ ] 如果是新发现，**Sundial / Visa Sniper 是否有同样 pattern**？跑 `tools/sibling_scan.sh '<signature regex>'`
- [ ] 修了 Compass 这一处，其他程序的同 pattern 处要不要同步修？

## 动手前 baseline 跑过吗

- [ ] 跑了 `tools/release_check.sh --baseline`
- [ ] 当前红/黄灯数：<X 红 / Y 黄>
- [ ] 我这次改动**预期不引入新红灯**，pre-existing 的 X 红里**不是这次任务范围**的我会忽略

## 文档 / CHANGELOG 同步计划

- [ ] 使用说明.md 哪节要更新？<>
- [ ] CHANGELOG.md 这次的 entry 标题：<>
- [ ] 影响 template/ 还是仅 personal 自用？<>

## 完成后 smoke checklist 是什么

(从 `docs/smoke-checklist.md` 抽相关项)

- [ ]
- [ ]
- [ ]

## 完成时再回填这部分

### 实际改了多少文件
<>

### 是否有 bug 被发现需要写 postmortem
- [ ] 不需要（无非平凡 bug）
- [ ] 需要，PM 文件名：<docs/postmortems/YYYY-MM-DD-<short>.md>

### diff 跑 `tools/release_check.sh --diff` 结果
- 新增红灯数：<>
- 如非 0：阻断发布，先修

### memory 有要补的吗
- [ ] 不需要
- [ ] 加了：<feedback_*.md>
- [ ] 更新了：<>

---

## 红线（满足任一不要继续）

- 列不出 3 个会被波及的既有功能
- baseline 红灯数 > 0 且其中包含**和这次修改路径相关的红灯**
- 跨程序 pattern 没扫
- 文档/CHANGELOG 同步无计划
- post-change diff 显示新红灯且不是预期的

---

## 借鉴

这模板从 GitHub PR description 借来了"风险面"和"测试 plan"两节，加了 solo dev 特有的"sibling 扫描"和"baseline 对照"。
GitHub 是给团队的——这个是给一个人的。
