# ADR-0014 · Kanban 是只读 funnel 视图，不做拖拽改状态

Status: Accepted
Date:   2026-05-02

## 情境

Stage B 加了一个 `/jobs/?view=kanban` 视图——按 status 分 6 列展示 jobs：未读 / 已看 / 已投 / 面试中 / Offer / 被拒。

Huntr 等求职跟踪工具的 Kanban 是**可拖拽**的——把卡从 "已投" 拖到 "面试中" 直接改 status。

我们做不做？

## 选项

### A. 完整 Huntr 风格——拖拽改状态
- ✓ 用户体验"丝滑"
- ✗ 改 status 是**有副作用**的动作（首次切到 applied 触发 `pre_submit_reflection`，写 ReflectionEvent，更新 funnel 数据）
- ✗ 拖拽误操作概率高——划过整个 board 时手抖
- ✗ 拖拽改完没有 review 步骤，**绕过了 Compass 的反思层**
- ✗ 实现复杂：HTML5 drag-drop API + HTMX 集成 + sortable.js 之类

### B. 只读 Kanban——视图归视图，状态归详情页
- ✓ 看自己 pipeline 形状的需求 100% 满足
- ✓ 改 status 仍走详情页——保留 `pre_submit_reflection` 拦截 + 警示
- ✓ 实现极简（纯 CSS flex + 现有数据）
- ✗ 需要点详情页才能改状态——多一步

### C. 混合：拖拽但弹模态确认
- ✓ 保留反思层
- ✗ 拖拽 + 模态 = 体验割裂
- ✗ 复杂度高于 A

## 决定

**选 B**——Kanban 是只读视图。

理由的核心：**Compass 的 status 切换不是"轻动作"**——它会触发反思层的拦截（pre_submit_reflection 在首次切 applied 时强制弹一页让用户回顾偏好对齐 / Adversarial Voice），还会写多条 ReflectionEvent 喂给 calibration learner。

如果做拖拽：
- 拦截要么取消（破坏反思飞轮）要么变成模态弹出（破坏拖拽流畅感）
- 两难都不好

最干净的姿势：**Kanban 看形状，详情页改状态**。两个 surface 各做一件事。

## 代价 / 后果

- 用户从 Kanban 看到一个想改状态的 job → 必须点进详情 → 改完回 Kanban。**多一次点击**。
- 这是有意的摩擦——状态切换值得用户主动 acknowledge

## 什么时候应该重审

- 用户实际反馈："我每天改 10 次状态，Kanban 来回点累"——那时考虑加拖拽 + 弹模态确认（选项 C）
- 但现在用户还没真用过 Compass 跑过一周——**先按 B 出，看真实需求频率**

## 备注

- 实现：`compass/templates/jobs/kanban.html`（~80 行）+ `_kanban_columns()` helper（~25 行）
- Tier 5（客观不可达）和 status=passed 不显示——pipeline 板上的噪音
- 教训：**功能选 simple-but-philosophically-aligned，不选 fancy-but-undermining**
