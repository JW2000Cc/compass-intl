# 2026-05-10: 打包 zip 中文文件名乱码

**Symptom**:
打 `Compass_personal_20260510.zip` 后 `unzip -l` 看到 `Compass/使?? 说???.md`，但活跃目录里是正常的 `使用说明.md`。打开 zip 给别人，对方解压找不到「使用说明.md」。

**Detection**:
release-prep verify 时检查"用户面向文档进 zip 了吗" → grep `使用说明` → 0 命中 → 用 `zipinfo -1` 看到名字乱码 `使?? 说???.md`

**Root cause**:
- macOS Finder 创建的中文文件名是 NFD 编码（`使` 拆成基字符 + 重组符）
- 系统自带 `zip -r` 把 NFD 字节直接塞进 zip header
- **没设 zip General Purpose Bit Flag bit 11 (UTF-8 flag)**
- macOS 自己 unzip 看着对（系统知道默认 NFD-ish）
- Windows / Linux unzip 把字节当 cp437 / utf-8 解 → 乱码

**Pattern class**: **P-004** (macOS `zip -r` 对中文文件名乱码)

**Fix**:
不再用 `zip` 命令。改写 Python 自己打：
```python
arc = base + "/" + unicodedata.normalize("NFC", rel)  # NFD → NFC
zi = zipfile.ZipInfo.from_file(full, arc)
zi.flag_bits |= 0x800  # bit 11 = UTF-8
```

**Sibling propagation scan**:
- ✅ Compass: 已迁移到 `tools/package_zips.py` (NFC + UTF-8 flag)
- ⚠ Sundial: 之前也用 system `zip` 打？检查桌面 `Sundial_*_2026050[78].zip` 中文文件名
- ⚠ Visa Sniper: 同上检查 `Visa Sniper_*.zip`
- 工具: `tools/sibling_scan.sh 'zip\s+-r' programs` (扫描所有 .sh / .command 找用 system zip 的脚本)

**Prevent recurrence**:
- bug-patterns.md 已加 P-004
- 共享 `package_zips.py` 模板（NFC + UTF-8 flag），下次给 Sundial / Visa Sniper 也复制一份
- release_check.sh 末尾解 zip 验中文文件名能 print（不报 `\u???` 转义或 `?`）
- memory: feedback_macos_zip_cjk.md 已记

**Time spent**: 发现 2min（unzip -l 一眼看出）/ 修 10min（写 Python 重打 + 验证）

**Lesson**:
`zip -r` 在 macOS 上**永远不要**用于含中文/日文/韩文文件名的目录。Python `zipfile` 是稳定方案。
