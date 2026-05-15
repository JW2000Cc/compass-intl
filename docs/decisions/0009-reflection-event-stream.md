# ADR-0009 · ReflectionEvent 事件流（Event Sourcing）

Status: Accepted
Date:   2026-05-01

## 情境

Compass 要做漂移分析、自我假设生成、stated-vs-revealed 对照——**所有这些都需要历史**。

v1 用 UserAction 表记录用户操作，但是**扁平的——只有 kind + payload + created_at**。
查"过去 30 天用户的所有 thumbs-down"还行；查"用户对 Tier 2 岗位的接受率随时间怎么变化"
要拼好几张表 join。

要不要重新设计数据架构？

## 选项

### A. 继续扁平 UserAction（v1 模式）
- ✓ 简单
- ✗ 复杂查询要 join 多表
- ✗ 没有原子性保证（先改 Job.status，再写 UserAction，可能脱节）

### B. 完整 Event Sourcing（CQRS 风格，所有写入都通过 event）
- ✓ 完整可审计
- ✗ 复杂度爆炸——读模型要专门维护
- ✗ 个人工具用不上这个复杂度

### C. ReflectionEvent 作为"反思层的事件流"——和 OLTP 数据并存
- ✓ Job/JobMatch/FactVariant 等表是 OLTP（current state）
- ✓ ReflectionEvent 是"重要决策的不可变日志"——append-only
- ✓ 所有反思相关的查询从 ReflectionEvent 走（不从 OLTP 表 reverse）
- ✓ 既不强迫所有写入走 event（保持工程简单），也保留完整决策历史
- ✗ 需要明确"什么是 reflection event"——不是所有写入都要记

## 决定

**选 C** —— ReflectionEvent 是反思层专用事件流，OLTP 数据正常存。

事件类型分类（不强制完整，但记录的都按这套来）：
```
identity_uploaded           身份层变化
fact_added | fact_deactivated
variant_proposed | variant_approved | variant_rejected | variant_edited_by_user
job_classified | job_pushed
job_thumbed_up | job_thumbed_down
job_opened | job_status_changed
calibration_changed
assumption_regenerated | assumption_overridden
adversarial_objection_raised
drift_alert | drift_snapshot_taken
l5_violation_blocked
value_regression_checkpoint
external_scrape | rewrite_attempt_completed
interview_started | interview_completed
```

每个事件存：`kind` + `payload_json` + `related_fact_id` + `related_job_id` + `created_at`。

> **更新**：上述列表写于 5-01，代表反思层最初识别的事件类。后续新增的事件类（如
> `fact_corrected` / `gmail_*` / `outreach_*` / `epsilon_exploration_triggered` /
> `tier_demoted_by_salary_floor` 等）会持续追加。**权威清单是代码里 `ReflectionEvent(kind=…)`
> 的所有出现处**。grep `kind=` in `compass/` 即可拿到当前完整集合。新增事件类时请：
> 1. 选短动词形 + 名词（与现有命名一致）
> 2. 在 payload 里带上 `model` / `attempt_id` / `score` 等够 replay 的上下文
> 3. 不需要回头改这个 ADR——这里只列"事件流是反思层入口"这个决定，不维护事件类目录。

读模型（漂移仪表盘 / 假设生成 / stated-vs-revealed）全部从这个流读。

## 代价 / 后果

- 每个写入要多一行 INSERT（性能影响极小，SQLite 很快）
- 数据库会增长——但每个事件平均几百字节，一年下来几十 MB
- 事件 kind 命名要稳定——改名会让历史数据"消失"
- 需要 grep 友好——所有事件 kind 都用 snake_case，可被 grep

## 这条决策的延伸价值

事件流是**Compass 反思层的真正基础设施**。所有"反思类"功能本质上都是
event stream 的不同 query。这让"加新反思视角"变得简单——只是写新 query。

Compass 故意**没**用完整 Event Sourcing（CQRS / aggregates / projections）—— 那是
分布式系统的解决方案，对单机个人工具是过度设计。**反思事件流是轻量的 ES 思想，
应用在最值得 ES 的那个层。**

## 备注

- 实现：`compass/models/core.py:ReflectionEvent`
- 写入入口：每个 service 在关键决策处 `session.add(ReflectionEvent(...))`
- 读取入口：`reflection_engine.py` 的所有函数都从 event stream 读
- 教训：**Event Sourcing 不是全有/全无——挑反思层这个值得 ES 的子领域用**
