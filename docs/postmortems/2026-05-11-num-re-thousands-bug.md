# 2026-05-11: `_NUM_RE` 千分位逗号导致 L5b 误判 fabrication

**Symptom**:
用户简历里写 "Scaled platform to 1,500 users" 进 source fact。AI 改写出变体 "Scaled platform to 1500 users"。Claim grounder 报 **L5b fabrication, hard block** —— 拒绝保存变体。

实际上 source = 变体 = 1500，是完全合法的等价改写（去千分位逗号），不是事实编造。**任何源文本含 `1,500` / `$50,000` / `1,000,000` 这种 US 千分位写法的用户都会被无故 block**。

**Detection**:
对抗式 review agent 跑 Compass 时手动追了 `_check_numeric_drift` 的代码路径，发现 `_NUM_RE` regex `(?<!\d)(\d+(?:\.\d+)?)\s*(%|x|K|M|B)?(?!\d)` 把 "1,500" 切成两个独立 match：`1` 和 `500`。后续 `_extract_numbers` 把 source 算成 `[1.0, 500.0]`，variant 算成 `[1500.0]`。1500 跟 source 任一数字 rel_diff > 20%，直接判 L5b。

**Root cause**:
- 正则没考虑千分位 `,` 作为数字内部分隔符
- `(?<!\d)` 只挡前一字符是数字，不挡 `,`
- `\d+` 贪婪到第一个非数字字符（即 `,`）就停
- 结果：完美的数字 boundary，完美的错切

**Pattern class**: **新 pattern 候选——文本解析没考虑 locale 格式**。暂归 P-006? 待 bug-patterns.md 加新条目时统一编号。

**Fix**:
`compass/services/claim_grounder.py:51-57`：正则增加千分位匹配分支
```python
_NUM_RE = re.compile(
    r"(?<![\d,])"
    r"(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"\s*(%|x|K|M|B)?"
    r"(?!\d)",
    re.I,
)
```

`compass/services/claim_grounder.py:96`：`float()` 前 strip 逗号
```python
val = float(m.group(1).replace(",", ""))
```

加 5 个测试 `tests/test_claim_grounder_l5ab.py`：
- `test_extract_numbers_handles_thousands_separator` — "1,500" 不再切成 [1, 500]
- `test_extract_numbers_handles_currency_format` — "$50,000" 识别为 50000
- `test_extract_numbers_handles_million_with_commas` — "1,000,000" 识别为 1M
- `test_extract_numbers_handles_decimal_with_thousands` — "12,345.67" 千分位+小数同时正确
- `test_l5_no_false_positive_on_thousands_vs_plain` — source "1,500" vs variant "1500" 不再误判 L5b

**Sibling propagation scan**:
- ✅ Compass：本 PM 的修复对象
- ❌ Sundial：不用 `_NUM_RE`（无 grounder），N/A
- ❌ Visa Sniper：不用 `_NUM_RE`（无 grounder），N/A
- 工具：`grep -rn "_NUM_RE\|num_re" ~/Desktop/我的程序/` 确认只 Compass 用

**Known follow-ups**（本轮不修，记下来）:
- **负数** `-5%` 工资变动会被读成 `5`（丢符号 → 语义错）
- **欧式格式** `1.500,00`（小数点 vs 千分位反过来）会被错读
- 这两个都是 locale 问题。Compass 用户主要是 US/英文求职场景，优先级较低。如果发现欧洲用户群增长再处理。

**Prevent recurrence**:
- 测试 5 条已覆盖核心场景
- `bug-patterns.md` 可考虑加一条 "文本解析需考虑 locale"（暂不立即加，等下一次类似事件再固化）
- LLM 对抗 review 是发现这种"用户输入边界 + 系统判断"型 bug 的好工具——下次发布前应再跑一次

**Time spent**: 找原因 10min（看 regex 直接懂） / 修 5min / 写测试 + 跑测试 10min / PM 15min

**Lesson**:
**正则表达式对 locale 不友好的默认假设是隐形地雷**。`(?<!\d)\d+` 看着稳妥，遇到任何非纯数字的合法格式（千分位 / 货币 / 国际写法）就出 bug。**任何"解析用户写的数字"的代码都要在 PR 里列出明确不支持的格式**，否则就是默认承诺支持。
