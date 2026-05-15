# Compass · 人眼 Smoke Checklist

> 程序化测试覆盖不到的"动画 / 视觉 / 交互"层。每次重打 zip 后双击启动器跑一遍。
> 全部加起来 < 3 分钟。

---

## 准备

1. 双击 `Double Click to Start_Mac.command`（或对应 .bat）
2. 浏览器打开 `http://127.0.0.1:7000`（启动器会自动开）
3. 打开 F12 控制台 + 网络面板（待会儿要切 throttling）

---

## 主流程 smoke

### A. 一键抓取浮层

- [ ] 点侧栏顶部 ⚡ **一键抓取**
- [ ] 0.5 秒内看到居中浮层弹出（半透明背景 + 居中卡片）
- [ ] 浮层标题「⚡ 一键抓取」+ 右上角「已用时 0:01」开始走数
- [ ] 「当前 profile」行显示 `(1/N) <profile 标签>`
- [ ] 「当前 keyword」行显示 `(N/M) <kw> · linkedin, indeed`
- [ ] 「累计」行 5 个数（抓取/新增/已存在/规则挡/失败）每 2 秒会更新
- [ ] 抓完跳页 → 浮层消失 → 顶部 flash 显示总结
- [ ] 总结里如果有 errors，超时的 keyword 会列出来

### B. LLM 测试连接

- [ ] 侧栏 ⚙️ **LLM 配置**
- [ ] 点页面里 **测试连接** 按钮
- [ ] 看到 ✓ pong 在 1-3 秒内返回（✗ 表示 API key 出问题）

### C. 岗位详情 + 子页

- [ ] 进 **岗位** 页 → 点任一岗位
- [ ] 详情页正常渲染（标题、JD、状态选择）
- [ ] 点 **效果**（effort）面板 → 加载子页
- [ ] 点 **联络人** → 加载子页（cold contacts 也试）

### D. 简历改写流水线（需要 LLM）

- [ ] 岗位详情页 → 点 **改写简历**
- [ ] 6 步流水线显示（按钮变 loading text、顶部进度条滑动）
- [ ] 完成后跳到 review 页 → 看每条 variant 显示 + 五维度菜单

### E. 离线指示器

- [ ] F12 → 网络面板 → throttling 下拉选 **Offline**
- [ ] 1-2 秒内，右上角冒红色「● 离线」pill
- [ ] 鼠标悬停 → 看到 tooltip 说明哪些功能还能用
- [ ] 切回 **Online** → pill 消失

### F. 资源全部本地（无 CDN 依赖）

- [ ] F12 → 网络面板 → 刷新页面
- [ ] 检查所有 JS / CSS 请求 → **不应该**有任何 `unpkg.com` / `cdnjs.cloudflare.com` / `jsdelivr.net`
- [ ] 应该看到 `static/js/vendor/alpine.min.js` + `static/js/vendor/htmx.min.js` 来自 127.0.0.1

---

## 边界 smoke（每月一次或重大改后）

### G. 0 profile 边界

- [ ] 求职意图页 → 全部禁用 → 点一键抓取
- [ ] 期望: flash「还没配置抓取意图。下面是抓取面板，填一组就能跑。」
- [ ] **不应该**：浮层弹出后卡住 / 500 错误

### H. 触发 in-flight lock

- [ ] 点一键抓取后立刻再点
- [ ] 期望: flash「⏳ scrape running...」+ 浮层不重新计时
- [ ] **不应该**：起两个并行 scrape

### I. 模拟 jobspy 挂死

- [ ] 拔网线（彻底断网）
- [ ] 点一键抓取
- [ ] 期望: 浮层会跑约 90 秒（每个 keyword 都超时） → 完成 → flash 显示 N 个 errors
- [ ] **不应该**：浮层永远不消失 / 进程占用 CPU 100%

---

## 跨程序快查（季度一次）

跑这条 sibling 扫，确认其他程序也健康：

```sh
~/Desktop/我的程序/求职工具/Compass/tools/sibling_scan.sh 'unpkg\.com|cdnjs|jsdelivr'
```

期望：所有程序都 0 命中（CDN 都本地化）。

---

## 失败时

任何一项 smoke 失败 → **不要打 zip**。
- 写 `docs/postmortems/YYYY-MM-DD-<short>.md` 记下来
- 修
- 再跑一遍
