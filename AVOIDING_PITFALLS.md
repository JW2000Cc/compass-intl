# Compass · 避坑指南

> 这份指南是从 8 轮内部审计（25 个真 bug）里提炼的"用户实际会踩到的坑 + 用得最顺的姿势"。
> 不是替代 `使用说明.md`（那个讲怎么用），而是讲**怎么用得不出问题**。

---

## 1. 启动 / 端口 / 重启

| 场景 | 怎么做 |
|---|---|
| **第一次启动** | 双击 `Double Click to Start_Mac.command`（Win 是 `.bat`），等 30-60 秒首次装依赖。浏览器自动打开 `http://localhost:7000` |
| **改完 `.env`（如换 LLM key）** | 不需要重启 server，下一个请求会 reload；如果 UI 测试连接不通，再重启 |
| **代码 / 模板改动** | **必须重启**——Flask 没开 auto-reload；Ctrl+C 终端窗口 → 再次双击启动器 |
| **重启后浏览器看到老 UI** | **硬刷新**：`Cmd+Shift+R`（Mac）/ `Ctrl+Shift+R`（Win）。普通刷新会用浏览器缓存 |
| **端口被占** | 终端会报 "Port 7000 is already in use"。在 `.env` 加 `COMPASS_PORT=7001` 换端口 |

---

## 2. 求职意图 / 抓取配置（最容易踩坑）

### 永远先填好"想找什么"再点抓取

`抓取配置`面板的 **"这条求职意图想找什么"** 文本框是 **tier 分类的核心输入**——LLM 拿这条话决定一个岗位算 Tier 1（核心）还是 Tier 5（不可达）。
- 写得空泛（"找好工作"）→ 分类全是 Tier 3，反茧房失效
- 写得具体（"电气/电力工程师 + 金融咨询，排除土木/HVAC/护理，研究生末期"）→ tier 区分度立刻拉开

### 一个国家可以多个 profile（不同方向分组）

不要把"德国-法兰克福-电气"和"德国-柏林-数据分析"塞同一个 profile。**每个城市 + 每个方向独立一组**。这样：
- Tier quota（推送配额）按 profile 算，不会被一个方向吃光
- 关键词列表更精准，jobspy 抓回来的相关度高
- 启用 / 禁用某个城市可以独立切换

### `dealbreakers.country_must_be` 必填

LinkedIn 经常按 IP 返回意大利岗（即使搜的是德国）。如果不锁国家，dashboard 全是错国岗。
- 抓配置 → 折叠"高级"→ `country_must_be` 填正确国家代码 / 名字

### 别在"抓取中"再点一次抓取

修复后**已经防住了**：5 个并发请求只有 1 个真跑，其他 4 个立刻提示"⏳ scrape running"。但用户体验上：
- 看到 ⏳ toast 就 **等**——不要狂点
- 真要中断卡死的抓取：等 10 分钟 stale lock 自动清，或直接重启 server

### 改完 profile，刷新页面看不到改动？

5-08 修了——server 数据是权威源，浏览器 localStorage 不会再覆盖服务端。但你的旧 cache 可能还残留：
- 第一次硬刷新（`Cmd+Shift+R`）即可清掉 stale localStorage
- 之后 server 改 → 普通刷新就能看到

---

## 3. LLM 配置

### 测试连接是 Source of Truth

填完 key 后**点"测试连接"**——直到看到 ✓ 才继续。
- 401（密钥无效）：检查 key 复制完整，没多空格
- 403（权限不足）：账号没开通这个模型 / 没付费
- 429（配额限流）：等几分钟再试，或换更便宜的模型
- 502（其他失败）：网络问题，重试

修复后这些状态码**都 properly 分类**了，不会再无脑 500。

### "preset" 一键切换不会动 API key

切预设不需要重新填 key。如果切完看到旧 key 还在那儿，是对的——key 跨预设保留。

### Step overrides 现在真生效

5-08 之前是个**安慰剂**——UI 让你设 "rewrite 用 sonnet, judge 用 haiku"，保存到 JSON 但 LLM 调用根本不读它。现在**真生效**：每个 step 用你指定的 model。
- 想省钱：把 grounder / judge / objection 改成 haiku，rewrite 留 sonnet
- 想质量：全部 sonnet（贵 3 倍）

---

## 4. 简历改写流水线（6 步）

### 一次 run 一份岗位

不要并行起 5 份岗位的 rewrite——LLM 限流、跑断、得不偿失。一份一份来，每份 30-60 秒。

### "再跑一次"会保留你 ✓ / ✎ 过的决定

5-07 的关键设计：你已经 approve / edit 的 variants，**第二次跑 pipeline 不会被覆盖**。pipeline 只重生你 reject 或 pending 的。所以：
- 看到一条特别好的 → 立刻点 ✓ 锁住
- 看到不对的 → ✗ 拒绝并填理由（理由会 feed 回 LLM 当负样本）
- 不确定 → 留 pending，再跑一次会重新生成不同角度

### 拒绝时一定填理由

理由有两个用：
1. 同岗位再跑 pipeline 时作为 negative example，LLM 给出真不一样的版本
2. 跨岗位累积成 calibration drift——5 次都拒"过度修饰"会自动把 `max_amplification_level` 降下去

### Education facts 编辑变体不会进 export

已知遗留：`kind="education"` fact 的 variant 决定，目前 export 时**不替换** structured_json 里的 school/degree。如果你想改"MSc Electrical Engineering" → "MSc EE (in progress)"，**直接编辑 ResumeFact text**（在 Identity 页面），不要走 variant 决定。

---

## 5. 简历模板（Themes）

### `/templates/upload` 只接受 HTML

5-08 加了二进制检测——往这个端口拽 .docx / .pdf 会被拒。要从 DOCX 学样式，走 **`/templates/inspire`** 流程：上传 DOCX → LLM 转 HTML → 保存为模板。

### 模板里别用 raw `{{ }}`

模板是 Jinja，`{{ ... }}` 会被求值。如果你的简历正文里有"年薪 {{salary}}"这种字面字符串，会触发 jinja 错误。要么转义 `{{ '{{' }}`，要么放进 HTML 注释。
- 上传时已加 jinja pre-compile 检查，语法错会提前拦下不会保存

### 删除 user theme 不会删 export 历史

`/templates/<id>/delete` 移除模板，但用这个模板已经导出过的 `.md` / `.docx` 文件**留在你硬盘上**——这是有意的。

---

## 6. 反思 / 漂移

### 复盘要看到模式必须留痕

每次 reject variant、change job status、quick feedback 都会写一条 `ReflectionEvent`。**至少要 30+ events 才能看出 drift**。
- 头一周：感觉 dashboard "什么也看不出"——正常
- 两三周后：calibration drift 才有数据点画曲线

### 拒信不要直接删（Gmail）

Gmail 同步功能（如果你授权）会从拒信里 LLM 提取拒因——这是 **funnel 闭环的金矿**。删了就没数据。
- 先标 Read，等 sync 再处理
- 没用 Gmail 的话，用 `/rejection/` 手动粘贴拒信文本

---

## 7. 数据 / 备份

### 备份目录

| 路径 | 内容 | 删的代价 |
|---|---|---|
| `data/compass.sqlite` | 全部岗位 / 简历 / 反思事件 | 全失去 |
| `data/last_scrape.json` | 求职意图配置 | 重新填 8 个 profile |
| `data/llm_settings.json` | LLM key + step overrides | 重新填 |
| `data/user_docx_templates/` | 用户上传的 .docx 模板 | 重新上传 |
| `data/templates.json` | 模板索引 | 重新建索引 |

把 `data/` 整个目录定期 backup（rsync / Time Machine），就够了。

### 个人版 vs Template 版 zip

| 桌面文件 | 含 | 用途 |
|---|---|---|
| `Compass_personal_*.zip` | 你的 .env 真 key + sqlite + last_scrape | **别给别人**——里面有你的 LLM key 和岗位历史 |
| `Compass_template_*.zip` | 排除上述敏感文件 | 给别人用（朋友、同学）|

---

## 8. 已知务实偏离（不是 bug，是设计决定）

| 行为 | 原因 |
|---|---|
| 编辑 fact 文本会 UPDATE，不是新建 fact | ADR-0005 严格说"facts immutable"，但纠错小改没必要新版本——保留 `original_text` + `FactRevision` audit trail |
| Education fact 的 variant 不进 export | 设计上 education 是 immutable 学历事实，rewrite pipeline 不应改它 |
| `cdn.jsdelivr.net` 加载 Alpine.js | 离线启动会卡 ~3 秒；如果完全断网，下载 Alpine 放 `static/` 自托管即可 |
| 日志在 `data/startup.log` | macOS / Win 都看这个文件——异常先 grep ERROR |

---

## 9. 紧急脱困

| 症状 | 怎么办 |
|---|---|
| Dashboard 打不开（5xx）| `data/startup.log` 看 traceback；如有 `OperationalError: no such column ...` 删除 `data/compass.sqlite-shm` `-wal`，重启会自动 alembic upgrade |
| 抓取按钮永远转圈 | 关浏览器标签 + 重启 server。`.scrape_in_flight` 文件如果 10 分钟还在，手动 `rm` 它 |
| LLM 一直 401 | key 过期 / 没充值 / region 不对——先在 `console.anthropic.com` 测一下原始 key |
| 一份岗位反复 rewrite 都生成同一垃圾 | 至少拒一次 + 填理由（不是空 reason），LLM 才有 negative example 转角度 |
| 模板 preview 500 | user theme 文件被破坏。去 `/templates/` 删掉那条；或直接 `rm data/templates/<id>.html` + `data/templates.json` 里删条目 |

---

## 10. 已修复但值得知道的历史 bug（万一还遇到，能识别）

| 症状 | 修了吗 | 你需要做什么 |
|---|---|---|
| 点📥抓取 → profile id 全变 / global 配置消失 / tier_quota 被重置 | ✓ 5-08 修 | 硬刷一次浏览器即可 |
| 配置了 step overrides 但 pipeline 不读 | ✓ 5-08 修 | 重新打开 `/jobs/llm-settings/` 确认存的还在 |
| 翻译了 variant 但 `?lang=zh` 导出仍是英文 | ✓ 5-08 修 | 重新 export 试试 |
| 双击 / 三连击抓取按钮 spawn 多个 pipeline | ✓ 5-08 修（O_EXCL atomic）| 现在最多只会跑 1 个，其他直接 ⏳ |
| `flash(.., "warn")` 显示成 ⓘ 不是 ⚠ | ✓ 5-08 修 | 警告 toast 现在正确黄色边框 |

---

更新于 2026-05-08，对应 8 轮审计修复后的状态。
