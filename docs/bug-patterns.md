# Bug Patterns Registry

> 一个 bug 不是个体 — 它是某类 pattern 的具体实例。修一处不够，要扫一类。
> 这个文件记 **抽象 pattern**（不是 instance），下次见类似症状时先查这里。

---

## 怎么用

1. **遇到 bug 时**：先查这文件，看是不是已知 pattern 的另一处实例
2. **修完 bug 时**：判断是不是值得记一个新 pattern（症状有特征、修法可复用、可能复现）
3. **每月一次**：跑 `tools/sibling_scan.sh` 用 pattern 的 signature regex 扫三个程序

新 pattern 编号格式：`P-<3 位>`，按时间顺序递增，不重用。

---

## P-001: SQLAlchemy `create_all` 调用早于 model import

**Signature**:
- 启动时报 `sqlite3.OperationalError: no such table: <name>`
- `Base.metadata.create_all()` 在 import models 之前调用
- ORM 表定义没注册到 metadata

**症状特征**:
- 全新 sqlite 文件第一次启动就崩
- 删 sqlite 重启不解决（重启时还是空表）
- alembic stamp 看着对，但 ORM 觉得表不存在

**修法**: 把 `from .models import *` 移到 `Base.metadata.create_all()` 之前，或者 import models 后再 create_all

**该 pattern 涵盖的程序**:
- ✅ Compass (历史问题, 已修)
- ⚠ Sundial: 检查 `dashboard.py` 启动顺序
- N/A Visa Sniper (无 SQLAlchemy)

**捕获手段**: 任何用 SQLAlchemy 的程序，启动检查表存在 (`SELECT name FROM sqlite_master`) 加进 release_check.sh

---

## P-002: cgo / 外部 binary 调用无超时 → Python 永久卡死

**Signature**:
- Python 调 cgo 库（tls-client）/ Playwright / subprocess.Popen
- 库自身没设 read timeout
- 上游服务 drop TCP 不发 RST
- 被卡住的线程在 sample 里看到 `__psynch_cvwait` 在某 `.dylib` 里

**症状特征**:
- worker 线程 sleeping 0% CPU 数分钟+
- 进程不挂、不报错、不重启
- 用户感觉"按钮没反应"或"抓取了 0 个"
- in-flight lock 文件不释放，后续请求被拦回 "X running"

**修法**:
- daemon thread + `t.join(timeout=N)`，超时弃用线程让进程退出时跟着死
- ❌ 不用 `concurrent.futures.ThreadPoolExecutor` 的 with-block —— 退出时 `wait=True` 强制等 future 完成，正好踩进死等
- 兜底：上层 try/finally 兜 lock release

**该 pattern 涵盖的程序**:
- ✅ Compass jobspy/tls-client (5-10 已修, see PM 2026-05-10-jobspy-hang)
- ⚠ Visa Sniper Playwright: 检查 `page.goto` 是否带 timeout 参数 + Page-level operations
- ⚠ Sundial TG bot polling: `pyTelegramBotAPI` 默认 long_polling_timeout 25s，验证一下
- ⚠ Compass GCal sync (`google-api-client`): 默认有 timeout 但要确认

**捕获手段**:
- `release_check.sh` grep 任何 `scrape_jobs(`/`page.goto(`/`subprocess.run(`/`requests.get(` 缺 `timeout=` 的告警
- sample 任何卡死进程超 60 秒 → 自动 abort 并记 PM

---

## P-003: 安全审计/template 打包时占位符污染活跃 overlay

**Signature**:
- `data/<config>.json` 是 `.env` 的 overlay（非空覆盖, 空 fall back）
- 打包/审计脚本把活跃文件的某字段改成 `"sk-fake-for-audit"` 之类
- 改完没改回真值
- 旁边有 `<file>.audit_bak*` / `<file>.audit_residue_*` 印证

**症状特征**:
- 用户报"明明 .env 里有真 key 但测试连接 401"
- 启动 log 显示读到非空 key 但调用就报 invalid
- `.env` 和 `data/<config>.json` 两份 key 不一致

**修法**:
- 立刻把 overlay 文件的污染字段清成 `""`，让 .env 接管
- overlay 加载逻辑：**空字符串 = fall back，非空 = 覆盖** 写入文件顶部注释，避免下次审计脚本写错
- 打包流程：审计永远在副本上跑，不动活跃文件

**该 pattern 涵盖的程序**:
- ✅ Compass `data/llm_settings.json` (5-10 已修, see PM 2026-05-10-llm-fake-key)
- ⚠ Sundial 任何 overlay 文件: 检查 `config.json` / `.env` 双层
- ⚠ Visa Sniper `config.json`: 没 overlay 模式但有审计风险

**捕获手段**:
- `release_check.sh` grep `data/` 下任何 `*.json` 含 `fake-for-audit` / `placeholder` / `xxx` 字串
- 打包后立刻跑 LLM ping / TG ping / GCal auth check

---

## P-004: macOS `zip -r` 对中文文件名乱码

**Signature**:
- macOS Finder 创建中文文件名是 NFD 编码（`使` = `使` + 重组符）
- 系统 `zip` 命令直接塞字节进 zip header
- 没设 General Purpose Bit Flag bit 11 (UTF-8 flag)

**症状特征**:
- macOS 自己 unzip 看着正常
- Windows / Linux unzip 看到 `使?? 说???.md`
- 用户拿到 zip 找不到「使用说明.md」误以为缺文档

**修法**: 改用 Python `zipfile`：
```python
arc = base + "/" + unicodedata.normalize("NFC", rel)  # NFD → NFC
zi = zipfile.ZipInfo.from_file(full, arc)
zi.flag_bits |= 0x800  # bit 11 = UTF-8
```

**该 pattern 涵盖的程序**:
- ✅ Compass (5-10 已修, see PM 2026-05-10-zip-cjk-mojibake)
- ✅ Sundial 打包：`tools/package_zips.py` 已用同模式
- ✅ Visa Sniper 打包：`tools/package_zips.py` 已用同模式（5-11 验证）

**捕获手段**: 所有打包流程统一走 `package_zips.py`（NFC + UTF-8 flag）；release_check 解 zip 验中文文件名能 print 出来

**⚠ 验证陷阱（5-11 踩过）**:
- **不要信 macOS 系统 `unzip -l` 的输出**——Apple 自带的 infozip 不正确处理 bit 11 UTF-8 flag，会把已修好的 zip 显示成 `使�?�说�??.md` 形式
- 这只是显示 bug，zip 本身可能完全正确
- **正确验证方式**（任一）：
  1. Python: `python3 -c "import zipfile; [print(i.filename, hex(i.flag_bits)) for i in zipfile.ZipFile('x.zip').infolist()]"` — 看 flag_bits 是否有 `0x800`
  2. 实际解压: `python3 -m zipfile -e x.zip /tmp/test && ls /tmp/test/`
  3. 在 Windows / 新版 Linux 上解压看是否正常
- release_check 脚本里如果要校验 P-004，必须用上述方式而不是 `unzip -l`

---

## P-005: 同名 form / button 多入口，新功能改 base 漏标某些子模板

**Signature**:
- 一个动作（如「一键抓取」）在多个模板中重复出现（侧栏 / 仪表盘 / 列表页）
- 改 base.html 加新行为后，子模板版本没同步加同样的 marker

**症状特征**:
- "侧栏抓取有进度浮层，但仪表盘那个没有"
- 用户在某入口看不到新功能，以为是 bug

**修法**:
- 全局搜 `action=` 路由名 → 列出所有 form / button 实例
- 每个都加同样的 `data-` marker
- 逻辑层用属性触发而不是 DOM 位置

**该 pattern 涵盖的程序**:
- ✅ Compass scrape forms (4 处都标了 `data-progress-source="scrape"`)
- ⚠ Sundial bot start/stop button: 多个入口检查
- ⚠ Visa Sniper start/stop: 看 templates/index.html 是否唯一入口

**捕获手段**: release_check.sh 检查"标记一致性" — 给定 action 路由名，所有命中的 form 必须有指定 marker 集合

---

## P-006: 浏览器原生 form submit 的 navigation 期间 JS 优先级降级

**Signature**:
- 用浏览器原生 `<form action method="post">` submit
- submit listener 里调 `setInterval` polling 或 DOM 修改
- 期望 "提交 + 立即显示 loading / 进度 / 浮层"

**症状特征**:
- 用户点提交后**看不到任何反馈** (浮层 / spinner / 进度)
- 30-60s 后才看到结果页 (服务器 redirect 后)
- 后端 endpoint 自己工作完全正常
- 前端 JS 代码看上去也注入对了

**Root cause**:
浏览器 form submit 后立即进入 navigation 等待状态:
- 旧页面 DOM 仍 visible 但 JS 优先级降级
- `setTimeout(0)` / `setInterval` 在 navigation pending 期间被浏览器 throttle
- DOM 修改 (如 `classList.add('is-active')`) 可能没及时 paint
- 用户感知"什么都没发生"

**修法**:
任何"提交 + UI 反馈"场景, **必须 AJAX 化**, 不能赌浏览器 navigation 期间的 JS 优先级:

```javascript
document.addEventListener('submit', function (e) {
  if (!form.matches('[data-progress-source]')) return;
  e.preventDefault();           // 阻止浏览器 navigation
  startPolling();               // 同步 DOM 修改, 立即 paint
  fetch(form.action, {
    method: 'POST',
    body: new FormData(form),
    redirect: 'follow',
  }).then(function (r) {
    window.location.href = r.url;  // 手动跳转, flash 走 session 自动带
  }).catch(handleErr);
});
```

**该 pattern 涵盖的程序**:
- ✅ Compass 5-10 修复 (一键抓取浮层, see PM 2026-05-10-progress-overlay-not-visible)
- N/A Sundial: pill 不需要 form 提交触发
- N/A Visa Sniper: 无类似进度浮层

**捕获手段**: smoke-checklist 必须真实点击, 不能只靠 curl POST 看后端

---

## P-005b: CDN 依赖未本地化（P-005 子模式）

**Signature**: 模板里直接引用 `unpkg.com` / `cdnjs` / `jsdelivr` 加载 JS/CSS

**症状特征**:
- 离线 / CDN 抽风时 UI 失去交互（Alpine `x-data` 哑巴 / htmx `hx-*` 哑巴）
- 不同模板引用同一库不同版本（如 Compass / Sundial 都踩过 chart.js 4.4.0 vs 4.4.1 同时存在）

**修法**:
1. `mkdir -p static/js/vendor static/css/vendor`
2. `curl -L <CDN URL> -o static/js/vendor/<name>.min.js`
3. 模板里 `https://cdn.../<lib>` → `/static/js/vendor/<name>.min.js`（或 `{{ url_for('static', filename=...) }}`）
4. 多版本统一为单一最新版本

**例外（by design 保留 CDN）**:
- 导出独立 HTML 给别人看的页面（`Content-Disposition: attachment`）—— 用户离线打开时不依赖你的服务，CDN 比本地路径更合适。例如 Sundial `templates/snapshot.html` 的 fullcalendar 引用。该例外要在模板里加注释说明，避免日后清理时误删。

**该 pattern 涵盖的程序**:
- ✅ Compass: 5-10 已迁移 alpine + htmx (2 库)
- ✅ Sundial: 5-10 已迁移 alpine + htmx + chart.js + html2canvas + marked (5 库) + fullcalendar 已在; snapshot.html 2 处 by design 保留
- ✅ Visa Sniper: inline 所有 JS/CSS, 天然无 CDN 依赖

**捕获手段**: `tools/sibling_scan.sh 'unpkg\.com|cdnjs|jsdelivr'` 默认仅扫 source / 模板 / 配置（不扫 .md / docs/，避免文档自我描述被误命中）

---

## P-007: 敏感数据排除用窄黑名单，新增数据类型必漏

**Signature**:
- 打包 / 导出 / 备份脚本里有 `EXCLUDE_FILES` / `EXCLUDE_GLOBS` 之类排除清单
- 清单是 enumerated list，每条对应某个具体文件 / 目录
- 程序新增数据类型时（加上传、加缓存、加历史快照），**有人**应该回头扩这个清单
- 但没人回头 → 新数据从默认通路漏出去

**症状特征**:
- "干净版" / "无 PII" zip 解压后里面有用户简历 PDF / 真实事件 CSV / 联系人缓存
- release_check 没报警，因为清单本身没扫描覆盖逻辑
- 漏的内容往往是"过去三个月新加的功能"产生的（旧清单时代还没存在）
- 同类程序（sibling）的排除清单结构相似 → 一个漏其他大概率也漏

**Root cause**:
**黑名单是开发者纪律的债**。每次新增私有数据通路（如 `data/uploads/`），开发者要主动想起来"这条新通路要不要加进 template 排除清单" —— 但脑子在写新功能，没人回头。一个排除清单 enumerate 5-10 项就到极限，过了就一定漏。

**修法（设计反转）**:
1. 整个敏感目录默认 **全排**（如 `TEMPLATE_EXTRA_GLOBS = ["data/*"]`）
2. 需要保留的具体文件用 **allowlist 显式救回**（如 Sundial 顶层 `config.json` 不在 data/ 下，天然不被打到）
3. 打包脚本最后加 **PII 防御自检层**：`if "/data/" in name or n.endswith(".pdf"/.docx"): return 1` —— 出现就 fail，不上线

```python
# WRONG (窄黑名单, 新增必漏)
TEMPLATE_EXTRA_FILES = {"data/sqlite.db"}
TEMPLATE_EXTRA_GLOBS = ["data/*.backup_*"]

# RIGHT (宽排除 + 自检兜底)
TEMPLATE_EXTRA_GLOBS = ["data/*"]  # 整个私有目录排空
# 自检
pii_bad = [n for n in zf.namelist()
           if "/data/" in n or n.lower().endswith((".pdf", ".docx"))]
if pii_bad: return 1
```

**该 pattern 涵盖的程序**:
- ⚠ Compass `tools/package_zips.py` (5-11 事故主角, 已修 — see PM 2026-05-11-template-zip-pii-leak)
- ⚠ Sundial `tools/package_zips.py` (同事故 sibling, 已修)
- ✅ Visa Sniper: template `data/` 本是空的没触发，但同 pattern 已对齐
- ⚠ 任何"打包 / 导出 / 备份"脚本：审视一遍排除清单是黑名单还是白名单

**捕获手段**:
- `tools/sibling_scan.sh 'TEMPLATE_EXTRA|EXCLUDE_FILES|EXCLUDE_GLOBS'` 看三家清单结构
- release_check 可加：解 template zip 验 `data/` 目录无文件
- 设计 review 看到 "新增功能 + 写新数据 + 没碰 package 脚本" 的 PR 自动 flag

**附带教训（5-11 实例）**:
- 防御代码不要在源码里 literal 写真名做匹配（如 `if "<UserGivenName>" in n`）—— 反而把真名打进 template 源码。用泛型 pattern（`*.pdf / *.docx / /data/`）做检测
- `*.example` 文件如果含 `/Users/<realname>/...` 真实路径，本身就是设计 bug —— example 应该用 `/PATH/TO/...` 占位符

---

## P-008: 首次配置应走 web wizard，不是让用户编辑文本文件

**Signature**:
- 程序需要 API key / token / 账号密码才能跑（LLM key / Telegram bot / Google OAuth / 站点登录）
- 当前给用户的 onboarding 是"自己找到 `.env` / `config.json`，用记事本打开填进去"
- 启动时跑 `_startup_check.py` 之类预检脚本，缺值就报 `ERROR Startup check failed` + 弹 troubleshoot.md

**症状特征**:
- 新用户解压程序、双击启动后**第一眼看到的就是 "FOUND 2 PROBLEM(S)" / "ERROR"**
- 误以为程序坏了, 而实际只是缺第一次配置
- 即便明白要填 .env 的, 也要：找到 .env 文件 → 用什么编辑器打开 → KEY=VALUE 语法 → 保存 → 重启
- 非技术用户 dropoff 严重；技术用户也觉得繁琐

**Root cause**:
"让用户编辑配置文件"是给开发者的 onboarding，不是给最终用户的。Modern self-hosted apps (Home Assistant / Plex / Sonarr / Grafana / Bitwarden) 全都是 first-run web wizard 模式。

**修法（架构）**:
程序本身就是个 web app，复用 Flask app 加 wizard，不另起新 UI 栈：

1. **`before_request` 钩子**判断配置完整性：
   ```python
   @app.before_request
   def _require_setup():
       p = request.path
       if p.startswith("/static/") or p == "/setup" or p.startswith("/setup/"):
           return None
       if _setup_needed():
           if p.startswith("/api/"):
               return jsonify({"error": "setup_required", "redirect": "/setup"}), 503
           return redirect("/setup")
   ```

2. **GET `/setup`**: 表单 + 实时状态标签（已填/待填）+ 申请 key 的链接（BotFather / Anthropic Console / GCP Console）

3. **POST `/setup`**: 原子写配置文件
   - `.env` (Sundial / Compass): 按行 `KEY=VALUE` 替换，保留注释和其它字段
   - `config.json` (Visa Sniper): JSON merge — example default + 现存 overlay + 新字段
   - 写法：`tmp.write_text(...) → os.replace(tmp, final)` 防中断坏文件

4. **保存后让进程立刻拿到新值**：
   - `.env`: `load_dotenv(env_path, override=True)` 推进 `os.environ`
   - 用 dataclass Settings 的（Compass）: `app.config["SETTINGS"] = Settings.from_env()` 原地刷新
   - 用子进程吃 env 的（Sundial bot）: 提示用户重启程序

5. **OAuth 场景**（Sundial GCal）：用 Flask 路由代替 `flow.run_local_server(port=8085)`
   - `/setup/gcal/upload` 接 client_secret.json file upload
   - `/setup/gcal/start`: `Flow.authorization_url()` + 存 state 到 Flask session → 跳 Google
   - `/setup/gcal/callback`: 验 state → `flow.fetch_token()` → 写 token.json

**该 pattern 涵盖的程序**:
- ✅ Sundial: `/setup` (.env: Telegram + Anthropic) + `/setup/gcal/{upload,start,callback}` (OAuth)
- ✅ Compass: `/setup` (.env: LLM_PROVIDER + LLM_API_KEY); `app.config["SETTINGS"] = Settings.from_env()` 原地刷新无需重启
- ✅ Visa Sniper: `/setup` (config.json: country + email + password + TOTP + targets); JSON merge 保留 notification/monitor 历史设置

**捕获手段**:
- release_check.sh 加 grep `_startup_check.py.*errors.append.*"is empty"` 触发警告 —— 这种"配置缺失就 raise error" 模式是反 pattern
- 设计 review 时见 `FOUND N PROBLEMS` 风格的 user-facing 错误，问"这能不能改成 wizard"

**附带教训（5-11 实例）**:
- 不要让 `_startup_check.py` 把"配置缺失"和"程序装坏了"混在一起。前者交给 web wizard, 后者才是真 error
- wizard 表单不要 extend base.html — base 里可能有 JS 调 /api 端点，被 before_request 503 → 表单页本身就报错。setup.html 应独立
- before_request 必须 allowlist `/static/*` 和 `/setup` `/setup/*`, 否则 OAuth callback / 静态资源回不来
- Flask session 需要 `app.secret_key`。OAuth state 用 session 存做 CSRF 防护
- Visa Sniper 这种文件配置型: 保存时要 example default + 现存 overlay + 新字段三合一，否则 wizard 会清掉用户在 dashboard 已配的 notification 等

**附带教训（5-13 Sundial 实例）**:

- **🔴 启动脚本不得拦在 wizard 之前 — 最容易踩的子坑**:
  dashboard.py 把 `/setup` 路由 + setup.html + `_setup_needed()` + `before_request` 装齐了，但启动脚本里有一段：
  ```bash
  if [ ! -f ".env" ]; then
    cp .env.example .env
    open .env 2>/dev/null || nano .env   # ← 致命
    read -rp "  Press Enter after saving .env..."
    exit 0
  fi
  ```
  用户根本走不到浏览器，wizard 等于不存在。**修法**: 启动脚本只做 `[ ! -f .env ] && cp .env.example .env` 静默兜底, 把判断和引导交给 dashboard 的 before_request. Sundial 5-13 这坑藏在 Mac .command + Win .bat × (personal + template) 共 4 个文件里, sibling sync 必须扫完。
- **Bot 子进程也要 gate**: Sundial 有 Telegram bot 子进程(supervisor.py + bot.py)。启动时 TOKEN 是占位符的话 bot 会反复崩重启刷 `logs/bot.log`. Compass 没这问题(无 bot)。修法: launcher 启 supervisor 前 grep .env 看 TOKEN 是否真值, 占位/空就 echo 跳过, 提示用户填完 wizard 重启。
- **(子 pattern) Test 按钮 — Sonarr/Grafana 行业 pattern**: 每个 key 字段旁加"测试"按钮, 点了实际调 API 验证 (TG getMe / Anthropic Haiku 1-token), 不写入文件, 仅校验。挂在 `/setup/test/*` 下复用 before_request 的 allowlist, 始终回 200 + JSON `{ok, error}` 便于前端解析。意义: 把"盲存 → 重启 → 还坏 → 再填"循环压成"填错当场红字"。Sundial 5-13 已加, Compass / Visa Sniper 可借鉴。
- **捕获手段补丁**: release_check.sh 应该再加一条 `grep -E 'open .env|notepad .env' "Double Click to Start*"` —— 命中即报警告。Sundial 5-13 漏检就是因为只查 `_startup_check.py` 而没查 launcher。

---

## 附：pattern 分类轴

便于以后归档新 pattern：

| 轴 | 类型 |
|---|---|
| **来源层** | 框架 / 第三方库 / 自家代码 / OS / 环境 |
| **症状** | 崩 / 卡 / 错数据 / 静默成功 / UI 不一致 |
| **触发概率** | 必现 / 常见 / 偶发 / 罕见 |
| **捕获难度** | 启动报错 / 测试用例 / 用户上报 / 代码审计才看到 |
| **修法成本** | 1 行 / 1 文件 / 多文件 / 架构改动 |

如果一个新 bug 在这五轴里和已有 pattern 完全重合 → 不是新 pattern，是 P-X 的另一处实例，去那个 pattern 加一行 sibling 即可。
