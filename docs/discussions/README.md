# Discussions · 高层思考与产品哲学

> ADR 记录"有具体代码后果的决策"。
> Discussion 记录"贯穿多个决策的高层思考"——essay 类，不绑定单个文件，
> 但塑造了整个 Compass 的方向。

## 现有 Discussions

| # | 标题 | 一句话 |
|---|---|---|
| [01](01-flywheel-taxonomy.md) | 飞轮不是一种东西，是一个家族 | 至少有 4 种飞轮（品牌/网络/数据/生态）+ Reflective vs Engagement |
| [02](02-fast-vs-slow-feedback.md) | 反馈周期决定一切 | 反馈 < 1 周自健康；反馈 > 1 周必须重型反向调节 |
| [03](03-user-graduation.md) | 用户毕业是成功，不是 churn | Compass 的 metric 不是 retention |
| [04](04-paths-2-and-3.md) | 路径二（匿名集体智慧）+ 路径三（开放 skill 生态） | 还没做但应该做 |
| [05](05-anti-bubble-strategies.md) | 反茧房不是反推荐——是约束化探索 | 5-tier 可达圈 + ε-exploration |
| [06](06-stated-vs-revealed-preference.md) | 用户声称的偏好和真偏好不同 | 这是反思层的核心动机 |

## 这个目录和 ADR 目录的区别

- ADR：做了 X 而不是 Y，因为 Z（绑定具体代码）
- Discussion：关于 P 这件事我们的整体看法（贯穿多个 ADR）

写 ADR 时如果发现"这条记录不下来"——很可能它是 Discussion 而不是 ADR。
