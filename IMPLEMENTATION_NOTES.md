# Compass — Implementation Notes (Phase 0–3 完成)

完整实现记录。最后一次 sync: 2026-05-01.

---

## 完成情况一览

| Phase | 状态 | 行数 |
|---|---|---|
| Phase 0  反思层骨架（drift dashboard + tool assumptions） | ✅ | ~400 |
| Phase 1  Tier matcher + scraper + funnel + jobs UI | ✅ | ~900 |
| Phase 2  Resume 多步流水线 + 访谈 + claim grounding + judge | ✅ | ~1500 |
| Phase 3  Calibration 学习 + adversarial voice + drift detector | ✅ | ~600 |
| **总计** | **27 .py + 13 .html** | **~4300 + 800** |

所有文件通过 Python 语法检查、ORM 模型 import 通过、settings 加载正常。

---

## Phase 1 — Tier Matcher + Funnel

### 关键文件

| 文件 | 作用 |
|---|---|
| `services/tier_classifier.py` | LLM 5-tier 分类器；输出 `{tier, score, reason, learning_gap}` |
| `services/scraper.py` | 薄包装 jobspy + 9 国 country mapping + 稳定 md5 ID |
| `services/job_funnel.py` | 7 阶段漏斗（含 by_tier 分布） + 时间序列 |
| `routes/jobs.py` | list/detail/status/thumbs/classify/scrape |
| `routes/funnel.py` | 漏斗 dashboard |

### 设计要点

- **Tier 1-5 模型** 替代 v1 的 push/skip 二分类
- **Tier 5 永远不推**（"工程师 ≠ 调酒师" 硬规则升级版）
- **70/20/10 配额**: Tier 1 占 70% / Tier 2 占 20% (反茧房甜点) / Tier 3 占 10% (附 learning_gap)
- **没 LLM key 时默认 Tier 5**（refuse-to-push 姿态）

---

## Phase 2 — Resume 多步流水线

### 关键文件

| 文件 | 作用 |
|---|---|
| `services/identity_engine.py` | 简历上传 → markitdown 解析 → LLM 抽 profile_json → 拆 facts |
| `services/interview_engine.py` | 5-8 问访谈式发现 → 用户回答 → 合成新原子 facts |
| `services/claim_grounder.py` | 6 层变换光谱 + L5 deterministic 硬拦截 |
| `services/adversarial_voice.py` | 永远不学习的"刚正声音"，反 sycophancy |
| `services/resume_writer.py` | 6 步流水线：keyword extract → match → rewrite → ground → adversarial → 3-judge |
| `routes/identity.py` | 上传 / 查看 facts / 启动访谈 / 合成 |
| `routes/resume.py` | workbench / run pipeline / review variants |

### 设计要点

#### Identity Layer
- IdentityVersion 是身份的"版本快照"，旧版本不删（漂移分析用）
- ResumeFact 是不可变原子事实（编辑 = 停用旧的 + 新建一个）
- 每个 fact 有 sensitive_category 标记 (education_credential / language_level / employment_dates / certification)

#### 6 层变换光谱（claim_grounder.py）
```
L0  直接照抄         ✓ auto-allow
L1  同义改写         ✓ auto-allow
L2  隐含显化         ⚠ 用户一次确认
L3  视角重构         ⚠ 必须先访谈
L4  推断填充         ⚠ 必须用户审批
L5  事实编造         ✗ 服务层硬拦截
```

L5 拦截是 **deterministic**（regex 而非 LLM），覆盖：
- 语言等级引入（`B1` / `native` / etc.）
- 学位类型引入（`PhD` / `MSc` / etc.）
- 标题升级（`participated` → `led` 这种动词跃迁）
- 敏感类目下的日期变化

#### 访谈式发现
解决用户的核心 insight："工科生很少写课程小项目，是不是事实不足？"

答：90% 是挖掘不足，10% 才是真灰区。
访谈引擎专门解决前 90%——LLM 不编造，只提问，让用户回忆出真事实。

工作流：
```
fact "Built electrical grid simulation in coursework"
  ↓
LLM 生成 5-8 个具体问题（数据规模/算法/baseline/扩展性...）
  ↓
用户回答
  ↓
LLM 合成出新的原子 facts（标记 user_verified=True）
  ↓
新 facts 作为 source 喂给改写流水线
```

#### Adversarial Voice
**永远不学习用户偏好**——反 sycophancy 抗体。

每个 variant 提案都过一次。如果起反对，反对意见显式存在 `FactVariant.adversarial_objection`，**用户能看见但不阻塞**。

#### 三角色 Judge
```
ATS bot:           keyword 密度 / 解析友好
HR (6s scan):      前 3 行 / logos / 立刻可读
Hiring manager:    候选人能解决我的问题吗
```

每个 0-10 分 + 一句反馈 → composite 加权平均。

---

## Phase 3 — Calibration 学习 + 反向调节

### 关键文件

| 文件 | 作用 |
|---|---|
| `services/calibration_learner.py` | 从最近变换决策学边界 + 漂移检测 |
| `routes/calibration.py` | 边界查看/编辑/relearn/价值观回归 |
| `templates/calibration/overview.html` | UI |

### 设计要点

#### 学的是边界，不是偏好
```python
# 这是关键——Reflective Flywheel vs Engagement Flywheel 的本质差异
max_amplification_level    # 你接受到 L0-L4 哪一级
preferred_verbs_avoid      # 拒绝过的强势动词
preferred_verbs_do         # 接受过的"恰到好处"动词
sensitive_categories_locked  # 高拒绝率类目自动 lock
```

#### 漂移防失控
- max_amplification_level 每次最多 ±1（slow drift）
- 严格的"avoid 优先于 do"规则（防 sycophancy）
- 价值观回归按钮（一键清空学习，回到中性默认）
- detect_drift 比较最近 2 个快照，跳变 >=2 就 alert

#### 用户随时能编辑
calibration UI 里所有字段都可手编。手编后，relearn 不会立刻覆盖（直到下次 30 天累积出新模式）。

---

## v1.0 后续 patches（lock 后补回的实用功能）

v1.0 锁定后第一天就发现"理论框架完整但实际用着不爽"——立刻补了 v1/v2 验证过的实用 gap。**这些不算违反 lock**（见 [CORE_LOCKED.md 修正条款](docs/CORE_LOCKED.md)）。

| 补丁 | 关键文件 | 价值 |
|---|---|---|
| **First-run DB bug 修复** | `compass/app.py` 加 `from . import models` | create_all 之前 metadata 是空的——所有请求 500 |
| **多组抓取配置** | `routes/jobs.py:_parse_scrape_groups`, `templates/jobs/list.html` | 不同地区独立关键词；localStorage 持久化 |
| **多语言 keyword aliases** | `services/keyword_alias.py`, `routes/jobs.py:suggest_aliases` | LLM 提议跨语言变体；用户审核才加入；中文用户在德国能搜到 |
| **每页 module help** | 8 个模板顶部 `<details>` 块 | 降低初次使用门槛 |
| **黑名单** | `services/blacklist.py`, `routes/blacklist.py`, `templates/blacklist/overview.html` | 公司 + 关键词过滤；JSON 文件存储不破坏 schema |
| **hours_old 时效过滤** | `services/scraper.py` + `routes/jobs.py` + UI select | 不抓 6 个月前过期岗位；每组独立配置 |
| **每组 enabled flag** | `templates/jobs/list.html` checkbox | 临时禁用一组而不删配置 |
| **JSON Resume 导出 + HTML preview** | `services/json_resume_exporter.py`, `routes/exports.py` | 接入开放生态；浏览器 Cmd+P 出 PDF |
| **Gmail 自动状态检测** | `services/gmail_sync.py`, `routes/gmail.py` | funnel 闭环；calibration 拿到真投递结果反馈 |
| **MCP server + reference skills** | `tools/mcp_server.py`, `compass/skills/` | Claude Code Skill 生态接入 |
| **错误页 traceback 暴露** | `app.py` errorhandler unwrap `original_exception` | 调试 500 时浏览器直接显示真因 |
| **依赖修复** | `requirements.txt` 加 `python-jobspy`, `markupsafe` | v1.0 漏写——抓取/错误页 crash |
| **测试代码** | `tests/test_tier_classifier.py` 等 3 个 | L5 拦截、slow drift 等关键不变量 |

---

## 跑起来

```bash
cd "~/Desktop/我的程序/求职工具/Compass"
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # 填 LLM_API_KEY（推荐 Claude）
python -m compass.run  # http://localhost:7000
```

或直接双击 `Double Click to Start_Mac.command` / `Double Click to Start_Windows.bat` —— 启动脚本会自动 venv + pip install + 检测端口冲突 + 自动开浏览器。

第一次启动建议顺序：

1. **打开 http://localhost:7000** → 自动跳到 `/reflect/dashboard`（空的）
2. **每页顶部 "看这是什么"** 折叠帮助里有"是什么/为什么/什么时候用"
3. **`/identity/upload`** 上传简历 → LLM 抽 facts
4. **`/identity`** 看 fact 列表，挑一个点 "🎤 访谈深挖"
5. **`/blacklist`** 加几个不感兴趣的公司 / 噪声关键词（用着加，不用预先配）
6. **`/jobs`** 配置抓取（多组配置、每组独立 hours_old / 关键词）—— 试试 "💡 建议别名"
7. **任意 job 详情页**，点 "🔄 重新分类" 触发 tier classifier
8. **`/resume`** 选一个 job → "▶ 跑流水线" → review 页逐 bullet 审核
9. **`/resume/review/<attempt_id>`** 审核完点"📥 HTML preview"浏览器 Cmd+P 出 PDF
10. **`/gmail`** 配置 Google OAuth → 同步邮件自动检测申请状态
11. **`/calibration`** 用一段时间后看 calibration 学到了什么
12. **`/reflect/dashboard`** 漂移仪表盘 + ε-exploration 健康度
13. **`/reflect/assumptions`** 让 LLM 写"对你的 6 条假设"，逐条质疑

---

## 没做的事 / 下一步

### Phase 4 (backlog) — Skill 生态层
- 把 Compass 的服务包装成 MCP server
- 写 5 个 reference skill (ATS audit / salary negotiation / interview brief / 转行决策 / 月度反思生成器)
- skill 索引 (类似 obsidian community-plugins.json)

### 数据迁移
`tools/migrate_from_v1.py` 是 stub 级别的实现。要点：
- 读 v1 SQLite 的 jobs / actions / resume_profiles
- 选择性迁移（只迁有 match_decision 或 status != 'new' 的 job）
- v1 push/skip 二元 → v2 tier 推断（push+score>=70 → tier 1; push → tier 2; skip → tier 4）
- 实际跑前先 `--dry-run`

### 已知 TODO
- `take_drift_snapshot` 可以接 cron-like 后台调度（现在只能手点）
- `regenerate_assumptions` 应该有节流（防止用户狂点）
- 简历最终 export 到 docx（现在只看到 review 页，没"下载最终版"）— 这是下次最值得做的体验补完
- ESCO 数据集成（路径四，给可达圈一个客观锚点）

### 已知小坑
- 第一次启动时 reflection dashboard 空白——需要至少 2 个 calibration_drift_point 才会画图。可以手点 "📌 现在抓取一个快照" 一次解决
- adversarial_voice 多花一次 LLM 调用——成本敏感时可以在 `.env` 设 `ADVERSARIAL_VOICE_ENABLED=false` 关掉

---

## 调试时的 first principle

如果哪里坏了，按这个顺序查：

1. **数据库就在 `data/compass.sqlite`** — 用 `sqlite3` CLI 直接看表
2. **所有"用户做了什么"的事件都在 `reflection_events` 表** — `SELECT kind, created_at FROM reflection_events ORDER BY created_at DESC LIMIT 30`
3. **LLM 调用失败基本只有三种**: `LLM_API_KEY` 为空 / 网络 / API quota — 看 stdout 的 `LLM attempt N failed: ...` 日志
4. **任何 LLM 解析返回 "fail"** 都是 tolerant 的——`parse_json_lenient` 会先剥 ``` fences，再正则提取 JSON。如果你看 `text.parse_json(default=[])` 返回 `[]`，多半是 LLM 没遵守 JSON 格式
5. **flask debug=False 默认不展开堆栈** — 调试时改 `app.run(debug=True)` 临时打开

---

## 这次实现的最深一句话

Compass 的代码量（~5100 行 Python+HTML）和 Job Mate 当前规模差不多——但**每个组件的存在都是为了一个明确的反向调节目的**：

| 组件 | 防什么 |
|---|---|
| 5-tier model | 防 push/skip 丢信息 |
| Tier 5 hard ban | 防"工程师 → 调酒师"乱推 |
| 6-level grounding | 防 LLM 美化过头 |
| L5 deterministic 硬拦截 | 防 LLM 创造性绕过 |
| Adversarial Voice | 防 sycophancy |
| Slow calibration drift | 防边界滑坡 |
| 价值观回归按钮 | 防累积漂移 |
| 工具假设暴露 | 防黑盒画像 |
| ReflectionEvent stream | 防失忆（一切可回溯） |
| 用户可手编 calibration | 防"AI 替我决定" |

Job Mate 是"加功能让推送更准"。
Compass 是"加约束让用户保持自主"。

这两件事在代码上看起来都是 "加"——但**思路相反**。

---

*Doc generated 2026-05-01, alongside the implementation it documents.*
