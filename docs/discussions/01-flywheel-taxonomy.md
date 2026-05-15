# Discussion 01 · 飞轮不是一种东西，是一个家族

Date: 2026-05-01

## 引子

讨论 Compass 时用户问："是不是所有软件最后都走到飞轮？"
回答这个问题前先要拆开"飞轮"——这个词被滥用，至少混了 4 种完全不同的东西。

## 四种飞轮

| 飞轮类型 | 机制 | 代表产品 | Compass 能走吗 |
|---|---|---|---|
| 品牌/复合飞轮（Bezos 原版） | 低价 → 流量 → 卖家 → 选品 → 体验 → 流量 | Amazon | 不能。无供应方网络 |
| 网络飞轮 | 用户越多，对每个用户的价值越大 | LinkedIn / 微信 | 不能。求职是个人事件 |
| 数据飞轮 | 用得越多 → 数据越多 → 模型越准 → 体验越好 | ChatGPT / 推荐系统 | 能走，但单用户级（天花板低） |
| 生态飞轮 | 平台养活第三方 → 第三方反哺平台 | AWS / Stripe / Shopify | 难，但有想象空间（路径三） |

## 数据飞轮的死胡同

求职软件的根本困境：**用户求职成功就走，飞轮转不起来**。

这就是 **流失即胜利的悖论**——做得越好，用户离开越快。这解释了为什么 Sonara 死了、为什么求职类创业公司活得艰难。

## Engagement Flywheel vs Reflective Flywheel

数据飞轮自身有两种形态：

### Engagement Flywheel（容易茧房）
- 系统观察用户偏好 → 优化推送 → 用户被动接受 → 行为加强
- TikTok / YouTube / 推荐系统的标准毒化路径
- 失控：信息茧房、视野收窄、推送毒化

### Reflective Flywheel（共进化）
- 系统提问用户 → 用户主动反思 → 用户决定 → 系统学边界
- 用户每一步都在做判断，所以有内置觉知
- 失控形态（calibration 漂移）可以通过反向调节防

**这是 Compass 的根本选择**——不做 Engagement，做 Reflective。

## 比飞轮更高的层

```
Level A：飞轮（自我加强）       — Bezos / 推荐系统
Level B：飞轮 + 反向调节        — YouTube 加健康度抑制
Level C：多飞轮共振（生态）      — Apple / AWS
Level D：飞轮触发涌现           — GPT 涌现 in-context learning
Level E：用户即资产（共进化）    — Notion / Roam / Anki ← Compass 的目标
```

Level E 的特征：**产品改造用户，被改造的用户让产品更值钱**。
Notion 的高级用户互相教学；Roam 让用户思维变了；Anki 让用户记忆模式变了。
这是工具和用户**共同演化**，而不是工具单方面优化用户。

## 飞轮的暗面

讨论里被忽略但很重要的：**飞轮转起来后会反向加速**。
- Twitter 算法飞轮 → 平台毒化
- 推荐系统飞轮 → 信息茧房
- AI 训练飞轮 → 模型坍缩

**Level B 的"反向调节"不是可选项，是必需品**。任何想做飞轮的产品，
day 1 就要想：失控形态是什么？怎么加 negative feedback？

Compass 的反向调节四件套（漂移可视化 / adversarial voice / 假设暴露 / 价值观回归）
对应这一原则——具体见 ADR-0004 / 0006 / 0008。

## 这条 discussion 落地到的具体 ADR

- ADR-0004 Reflective vs Engagement Flywheel
- ADR-0006 Adversarial Voice frozen
- ADR-0008 ToolAssumption overridable
- tg-calendar ADR-0005（快反馈飞轮的反例对照）
