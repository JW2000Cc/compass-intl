# 2026-05-10: jobspy/tls-client 永久挂死

**Symptom**:
用户点「一键抓取」后看到 0 个新增。再点提示「⏳ scrape running」，刷新也没用。

**Detection**:
用户上报 → 用 investigate skill 跑 → `data/error.log` 没新错误 → DB 看 5-09/5-10 完全没 `external_scrape` 事件 → 发现 `data/.scrape_in_flight` 存在 28 分钟、pid 还活着 → `sample <pid>` 看到 worker 卡在 `lock_PyThread_acquire_lock`，两条 `tls-client-arm64.dylib` goroutine 卡在 `__psynch_cvwait`

**Root cause**:
jobspy 内部用 [bogdanfinn/tls-client](https://github.com/bogdanfinn/tls-client) (Go cgo) 绕 LinkedIn TLS 指纹检测。LinkedIn 偶尔 drop TCP 不发 RST，tls-client 没设 read 超时，goroutine 永久卡 `__psynch_cvwait`。Python 上层调用 `scrape_jobs(...)` 同步等结果，跟着卡。Compass 的 `_scrape_lock` 上下文等不到 scrape 函数返回，`data/.scrape_in_flight` 文件不删，**lock-stale-reclaim 也救不了**（持锁进程还活着，不算 stale）。

**Pattern class**: **P-002** (cgo / 外部 binary 调用无超时 → Python 永久卡死)

**Fix**:
- `compass/services/scraper.py:scrape_one()` 把 `scrape_jobs(**kw)` 包进 daemon 线程 + `t.join(SCRAPE_CALL_TIMEOUT_SEC=90)`
- 超时则当 `raw=0` 跳过那个 keyword、错误信息进 `errors` 列表
- 不用 `concurrent.futures.ThreadPoolExecutor`：with-block 退出时 `wait=True` 强制等 future 完成，正好踩进死等

**Sibling propagation scan**:
- ✅ Compass: 仅 jobspy 一处用 cgo 风格库，已修
- ⚠ Visa Sniper: 用 Playwright，`auth.py:page.goto()` 都带 `timeout=` 参数（25-30s），但 form 提交后的等待要确认
- ⚠ Sundial: `bot.py` 用 pyTelegramBotAPI 长轮询，需查 long_polling_timeout
- 工具: `tools/sibling_scan.sh 'scrape_jobs|page\.goto|requests\.get|httpx\.\w+\(' | grep -v 'timeout='`

**Prevent recurrence**:
- bug-patterns.md 已加 P-002，含 sibling 检查清单
- release_check.sh 加 grep 任何外部调用缺 `timeout=` 参数的告警
- memory: feedback_jobspy_hang.md 已记

**Time spent**: 找原因 25min / 修 15min / mock 验证 10min

**Lesson**:
任何用了 cgo / 外部 binary / 网络库 的 Python 调用，**默认它会卡死，不会**。daemon 线程 + `join(timeout)` 是最低保险。
