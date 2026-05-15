# ADR-0001 · Fork v2 而不在 v1 上原地改

Status: Accepted
Date:   2026-05-01

## 情境

Job Mate (v1) 已经投产能用，是一个 Flask + SQLAlchemy 的求职过滤器。我们想把它升级为"反思系统"——加 Tier 模型、Claim Grounding、Reflective Flywheel 等。问题是：**这是在 v1 仓库里原地改，还是 fork 一个新仓库 v2 重新开始？**

我们花了一段时间讨论才意识到这两种做法不是工程量差别，而是**心理空间差别**。

## 选项

### A. In-place 改造（改 v1）
- ✓ 数据自然演进，alembic 帮你迁
- ✓ 不中断使用
- ✗ `matcher.py` 整个 prompt 是按"看门人"语义写的——要变成"分类器+反思器"得逐行重写
- ✗ `Job.match_decision` 是 push/skip 二元——扩到 tier 1-5 是 schema 改动 + 各处 if 逻辑都要改
- ✗ **最致命**：打开旧仓库 IDE 时肌肉记忆会自动按"修一个 Flask app"思考，永远跨不到"设计一个反思系统"

### B. 重做（推倒）
- ✓ 干净的画板
- ✗ 沉默知识全丢（"工程师 ≠ 调酒师" 那条 hard floor 等血泪经验）
- ✗ 数据迁移复杂或丢失
- ✗ 中断使用

### C. Fork + Reference Rewrite（v2 新仓库，v1 当 maintenance）
- ✓ 干净的心理空间——目录结构按新产品定位组织（不是按 v1 的 services/）
- ✓ v1 当**活的 reference**——写 v2 时把 v1 同模块文件打开放旁边，借鉴而不复制
- ✓ v1 继续顶着用户使用，v2 慢慢长大
- ✓ 数据可一次性迁移（`tools/migrate_from_v1.py`）+ 做一次清洗
- ✗ 一段时间内两份代码并存
- ✗ 需要明确退役节奏

## 决定

**选 C**——Fork。这是 Martin Fowler 的 Strangler Fig Pattern 的轻量化变体。

具体规则：
1. v2 不 import v1 任何代码
2. 但写每个模块时把 v1 对应文件**打开当参考**（思想搬，prompt 重写，schema 重设计）
3. v1 进 maintenance mode（只修必要 bug，不加新功能）
4. v2 第一版不必功能齐全——先做"在 v1 数据上的反思层"，慢慢扩
5. 切换节奏：v2 成熟后切换，预计 8–12 周

## 代价 / 后果

- 一段时间维护两份代码（约 8-12 周）
- 沉默知识需要主动迁——尤其是 prompt 经验、jobspy 包装、markitdown fallback 链等
- 用户切换时机需要主动决定（不像 in-place 那样自然过渡）

什么时候会后悔这个决定：
- 如果两个版本并存超过 6 个月还没切换 → 决策错了，重新评估
- 如果 v2 的"清白心理空间"没真的产生差异化设计 → 那 fork 是浪费

## 备注

- Martin Fowler 的 [Strangler Fig Application](https://martinfowler.com/bliki/StranglerFigApplication.html)
- v1 的 maintenance 范围：只修 bug，不加新功能
- v2 数据迁移见 `tools/migrate_from_v1.py`
