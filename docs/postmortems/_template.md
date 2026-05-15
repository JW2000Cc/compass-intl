# YYYY-MM-DD: <一句话题目>

> 5-15 行写完。给三个月后的自己看，不是给团队汇报。
> 重点：根因 + 哪个 pattern 类 + 别处有没有同问题 + 怎么防再犯。

---

**Symptom**（用户/我看到啥）:

**Detection**（怎么发现的，被报 / 自查 / 监控触发）:

**Root cause**（实际技术原因，越具体越好）:

**Pattern class**: P-XXX (引用 docs/bug-patterns.md，无对应就先写 "新 pattern 候选"，确认后给个新编号)

**Fix**:
- <文件路径>:<行号> 改了什么

**Sibling propagation scan**（横向）:
- ✅/⚠ Compass: <扫描结果>
- ✅/⚠ Sundial: <扫描结果>
- ✅/⚠ Visa Sniper: <扫描结果>
- 工具: `tools/sibling_scan.sh '<regex>'`

**Prevent recurrence**（怎么防再次出现）:
- bug-patterns.md 加/更新 P-XXX
- release_check.sh 加 grep <signature>
- 文档/CHANGELOG 同步

**Time spent**: 找原因 Xmin / 修 Ymin / 验证 Zmin

**Lesson**（一句话，给未来的自己）:
