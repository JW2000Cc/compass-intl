# 2026-05-13 (PM): 简历多语言导出 — 字体 fallback 隐性卡住

> 用户要求"英/中/日 + 欧洲主要语言"导出. 调研发现 Compass **已经实现** 10 种
> 语言架构 (variant_translator + JSON Resume + UI + export 路由). 真正缺的
> 只是 docx 模板的 `<w:rFonts w:eastAsia="...">` 用了非 CJK 字体, 渲染中文/
> 日文时 fallback 到系统默认, 跨平台不一致.

---

**Symptom**:
- 用户预期"多语言简历导出"是个大 feature 需要开发
- 调研发现 90% 已实现, UI dropdown 已经有 9 种语言可选, export 路由都接受 `?lang=`, build_json_resume 已支持 target_language

**Detection**:
- 用户原始反馈"导出 Word 没按钮"调查中, 发现 docx 模板 `r.work[N]` 硬编码索引问题
- 修完后多语言架构 sweep, 发现 SUPPORTED_TARGETS = 10 种, variant_translator 完备
- 试翻译 + 渲染, 发现中文字符渲染时字体 fallback 不一致

**Root cause**:
zhenghao_cv_high_fidelity.docx 用 `<w:rFonts w:ascii="URWPalladioL"
w:hAnsi="URWPalladioL" w:eastAsia="URWPalladioL"/>`. URWPalladioL 是 LaTeX 拉丁文字体,
**不含 CJK 字形**. eastAsia 指向同样字体意味着 Word 渲染中文时找不到字形, 回退
到 system default — 跨平台不一致 (Win=宋体, Mac=PingFang SC, Linux 各异).

LMMathSymbols6 字体 (列表项符号字体) 同问题, 更甚 — 完全不含任何字母字形.

**Pattern class**: P-XXX 候选: "现成 feature 表面没卡但实际细节阻塞"
跟 P-008 "wizard 实现完整不代表用户走得到" 同类: 90% 工程已做, 1 个细节卡死.

**Fix**:
- `data/templates/zhenghao_cv_high_fidelity.docx`: 用 /tmp/add_cjk_fallback.py 脚本把所有 32 个 `<w:rFonts>` 的 `w:eastAsia` 改成 "Microsoft YaHei"
  - Win 原生 / Mac 通过 substitution / Linux 找不到时 Word 自动 fallback
- `compass/templates/resume/_review_panel.html`: `_export_languages` 加俄文 entry (后端早支持)

**Sibling propagation scan**:
- ✅ Compass: 仅 zhenghao 模板 (personal data) + UI 模板改动
- ❌ Sundial / Visa Sniper: 无 docx 导出, 不涉及
- 通用 pattern: 任何 user 上传的 .docx 模板都可能有同问题, 应在 template_inspire docx_route 加 PASS 4 自动注入 CJK fallback. 留作 future polish.

**Prevent recurrence**:
- ✅ CHANGELOG 记录
- ⏳ template_inspire 加 CJK font fallback PASS (T22 后续)
- ✅ E2E 测试覆盖 5 种语言: zh / ja / it / de / ru 渲染正确

**Time spent**: 调研 ~30min / 字体 fallback 实施 ~15min / E2E 验证 ~10min = ~1h

**Lesson**: 调研一个 feature 时, 不光看"代码有没有", 还看"是否真的能 work
across all dimensions" — 这次的 dimension 是字体, 之前的 dimension 是 launcher.
不同 dimension 都可能藏着"差最后一公里"的问题.
