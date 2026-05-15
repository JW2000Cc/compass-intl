# 2026-05-10: 一键抓取进度浮层用户感知不到

**Symptom**:
用户报"compass 一键抓取不走进度啊"——点了 sidebar 按钮后看不到浮层弹出，30-60 秒后跳到 /jobs/ 才看到 flash 完成。

**Detection**:
用户上报 → 用 investigate skill 排查 → 验证后端 endpoint `/jobs/api/scrape/progress` 在 1 秒内就返回完整 progress JSON (profile_index=1/8, kw_index=1/9, phase=keyword_running) → 验证 base.html HTML/JS 注入完整 (overlay div + 4 处 form 标记 + startPolling 函数都在) → 推断问题在浏览器 form submit 行为

**Root cause**:
原实现用浏览器原生 form submit:
```javascript
document.addEventListener('submit', function (e) {
  // ...check progressSource marker...
  setTimeout(function () { startPolling(); }, 0);
});
```
但浏览器 form submit 后立即进入 navigation 等待状态：
1. POST 请求发出, 等服务器响应 (30-60s scrape 期间)
2. 旧页面 DOM 还在但 JS 优先级降级
3. `setTimeout(0)` 推到下个 tick, 这时 navigation 已经开始, JS 可能不跑或晚跑
4. 即便 startPolling 跑了, setInterval 在 navigation pending 期间被浏览器 throttle
5. 浮层 `classList.add('is-active')` DOM 修改没及时 paint → 用户看不到

**Pattern class**: **新 pattern P-006 候选** (浏览器 navigation 期间 JS 优先级降级 → DOM 修改不及时 paint)

**Fix**:
- `compass/templates/base.html` submit handler 改成 AJAX:
  - `e.preventDefault()` 阻止浏览器原生 navigation
  - **立即同步** 调 `startPolling()`, DOM 修改在当前 event loop paint
  - `fetch(form.action, {method:'POST',body:FormData(form),redirect:'follow'})` 异步发请求
  - 响应回来后 `window.location.href = r.url` 手动跳转 (Flask 302 redirect 已 follow 到最终 URL)
  - `inflight` flag 防双击
  - `.catch()` 网络失败时 alert + stopPolling + 隐藏浮层

**Sibling propagation scan**:
- ✅ Compass: 唯一一处 form-submit-triggered overlay, 已修
- N/A Sundial: pill 不需要 form submit (只看 navigator.onLine + pending_ops polling, 独立于任何 form 提交)
- N/A Visa Sniper: 无类似进度浮层
- 工具: `tools/sibling_scan.sh 'data-progress-source|preventDefault.*scrape'`

**Prevent recurrence**:
- bug-patterns.md 加 P-006: "浏览器原生 form submit 的 navigation 期间不能假设 JS 优先级正常"
- release_check.sh 增 mock 浏览器交互测试? — 暂不加, 成本高于价值; 用户 smoke checklist 覆盖
- 教训: 任何"提交后还要 UI 反馈"的场景, 默认走 AJAX, 不要靠浏览器 navigation 期间的 JS 执行

**Time spent**: 调查 15min / 改 fix 5min / 验证 5min

**Lesson**:
浏览器 navigation 期间 JS 不是 first-class citizen. **任何要"提交 + 显示进度"的 form, 默认 AJAX 化 (preventDefault + fetch + 手动跳转)**, 不要赌浏览器在 navigation 期间还给 setInterval 正常优先级.
