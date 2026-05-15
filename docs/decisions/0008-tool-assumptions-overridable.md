# ADR-0008 · 工具假设暴露 + 用户可推翻

Status: Accepted
Date:   2026-05-01

## 情境

任何"学习用户偏好"的系统都会形成一个**画像**——它怎么理解你。
但传统设计里这个画像是黑盒：
- 推荐系统："为你推荐"——为什么是这个？不知道
- 广告系统："你可能感兴趣"——根据什么？不告诉你

这是 Engagement Flywheel 失控的根源——**用户看不到自己被怎么塑造**。

Compass 要反过来——**让画像可见、可推翻**。

## 选项

### A. 不展示画像（标准做法）
- ✓ 简单
- ✗ 用户失去对自己被塑造的觉知
- ✗ Reflective Flywheel 的核心保障消失

### B. 展示画像但只读
- ✓ 透明度
- ✗ 用户看到错误也无法修正——挫败感

### C. 展示画像 + 用户可推翻每条假设 + 推翻是 sticky
- ✓ 完整闭环——用户能看见、能反驳、推翻持续生效
- ✓ 把"用户即合作者"具象化
- ✗ 需要专门的 UI
- ✗ 需要数据模型支持（user_override 字段）

## 决定

**选 C**。具体设计：

`ToolAssumption` 表：
```python
category:       str   # values_held / role_directions / ...（6 类）
text:           str   # 系统对你的信念，如 "I seem to assume you most value..."
confidence:     float # 0-1
user_override:  str | None  # 用户的修正——一旦写入 sticky
is_current:     bool  # 是否为当前一份（每月重生成会标 false 旧的）
```

工作流：
1. 每月一次（或用户手点）LLM 读 facts + 最近 ReflectionEvent → 生成 6 条假设
2. 前端展示每条假设 + 信心条 + "不同意？写下你的修正" 按钮
3. 用户写修正 → 存到 user_override，触发 `assumption_overridden` 事件
4. 下次重生成时，user_override 跨代保留——直到用户主动清除

## 代价 / 后果

- 每次重生成是一次大 LLM 调用（要拼 facts + recent events）
- 6 条假设的类目固定（values / roles / growth / risk / env / boundary）—— 限制表达
- 用户可能从不点 "重生成"——需要主动提醒（"距离上次假设生成已 N 天"）

什么时候会后悔：
- 如果用户从不修正任何假设 → 这未必是 bug，可能是假设确实准；但要监控
- 如果假设质量随模型升级下降 → 重写 ASSUMPTIONS_SYSTEM prompt

## 这条决策的哲学根基

**用户即合作者，不是输入数据**。
- 标准 ML 系统："观察用户行为 → 优化输出"——用户是被动的
- Compass："系统提出假设 → 用户审核修正"——用户是主动的

这是 Reflective Flywheel 的**反向调节四件套**之一（与 adversarial voice / slow drift / 价值观回归并列）。

## 备注

- 实现：`compass/services/reflection_engine.py:regenerate_assumptions / override_assumption`
- 数据：`compass/models/core.py:ToolAssumption`
- UI：`templates/reflection/assumptions.html`
- 哲学溯源：Apple App Tracking Transparency 的 "用户必须能看见也能反驳" 设计原则
- 与之绑定：ADR-0004 Reflective Flywheel 反向调节
