#!/usr/bin/env python3
"""tools/package_zips.py — 打 personal + template 双 zip

用法:
    python3 tools/package_zips.py             # 用今天日期
    python3 tools/package_zips.py 20260510    # 指定日期
    python3 tools/package_zips.py --personal-only

设计原则:
    - 强制 NFC + UTF-8 flag (P-004 防 macOS zip 中文乱码)
    - personal zip 含 .env / sqlite / 用户上传文件 (绝不发别人)
    - template zip 自动排除敏感数据
    - 排除 dev 残余: .venv / __pycache__ / .pytest_cache / *.pyc / .DS_Store
    - 排除运行时残余: .scrape_in_flight / .scrape_progress.json / *.log
    - 排除历史快照: data_backup_*

放在 求职工具/ 上一层目录, 与 Compass/ 和 Compass_template/ 同级。
"""
from __future__ import annotations

import argparse
import datetime
import fnmatch
import os
import sys
import unicodedata
import zipfile
from pathlib import Path

# ── 排除清单 ────────────────────────────────────────────────────────────

EXCLUDE_DIR_NAMES = {".venv", "__pycache__", ".pytest_cache", ".git"}

EXCLUDE_DIR_PREFIXES = ("data_backup_",)

EXCLUDE_FILE_GLOBS = [
    "*.pyc", "*.pyo", "*.swp", "*.log",
    ".DS_Store",
    ".scrape_in_flight",
    ".scrape_progress.json",
    ".release_check_baseline.json",
    # Transient backup files (created by humans/scripts before risky edits).
    # Both personal and template zips should be clean of these — they're
    # rollback safety nets for the live workspace, not artifacts for end users.
    "*.bak",
    "*.bak.*",
]

# template-only 额外排除：整个 data/ 目录（sqlite / uploads / templates /
# contact_cache / blacklist / llm_settings / last_scrape / audit residues /
# 历史备份等全部都是个人/运行时数据，模板不应携带任何一条）。
# 顶层 .env.example 在根目录, 不在 data/, 不受影响。
TEMPLATE_EXTRA_FILES: set[str] = set()
TEMPLATE_EXTRA_GLOBS = [
    "data/*",
]


def should_skip(rel_path: str, *, extra_files=None, extra_globs=None) -> bool:
    parts = Path(rel_path).parts
    for p in parts:
        if p in EXCLUDE_DIR_NAMES:
            return True
        for prefix in EXCLUDE_DIR_PREFIXES:
            if p.startswith(prefix):
                return True
    name = Path(rel_path).name
    for g in EXCLUDE_FILE_GLOBS:
        if fnmatch.fnmatch(name, g):
            return True
    if extra_files and rel_path in extra_files:
        return True
    if extra_globs:
        for g in extra_globs:
            if fnmatch.fnmatch(rel_path, g):
                return True
    return False


def build_zip(src_dir: Path, zip_path: Path, *,
              extra_excludes=None, extra_globs=None) -> tuple[int, int]:
    """打 zip, 返回 (file_count, total_bytes)."""
    base = src_dir.name
    n = 0
    total = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
        for root, dirs, files in os.walk(src_dir):
            # 在 walk 时就剪掉排除目录, 避免下钻
            dirs[:] = [d for d in dirs
                       if d not in EXCLUDE_DIR_NAMES
                       and not any(d.startswith(p) for p in EXCLUDE_DIR_PREFIXES)]
            for f in files:
                full = Path(root) / f
                rel = str(full.relative_to(src_dir))
                if should_skip(rel, extra_files=extra_excludes, extra_globs=extra_globs):
                    continue
                # P-004 防御: NFD → NFC + UTF-8 flag
                arc = base + "/" + unicodedata.normalize("NFC", rel)
                zi = zipfile.ZipInfo.from_file(str(full), arc)
                zi.flag_bits |= 0x800  # bit 11 = UTF-8 in name
                zi.compress_type = zipfile.ZIP_DEFLATED
                with open(full, "rb") as src:
                    data = src.read()
                zf.writestr(zi, data)
                n += 1
                total += len(data)
    return n, total


def verify_zip(zip_path: Path) -> tuple[bool, list[str]]:
    """sanity check: 中文文件名能正常 print, 不应有 ?  乱码."""
    issues = []
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        for n in names:
            # 检测乱码: 含 ?  或 ufffd 替代字符
            if "�" in n or "?" in n.split("/")[-1] and any(ord(c) > 127 for c in n):
                issues.append(f"乱码文件名: {n!r}")
        # 验证含核心面向用户文档
        prog_dir = names[0].split("/")[0] if names else ""
        for must_have in ["使用说明.md", "README.md", "CHANGELOG.md"]:
            if not any(n.endswith(f"/{must_have}") for n in names):
                issues.append(f"缺核心文档: {must_have}")
    return len(issues) == 0, issues


def main() -> int:
    ap = argparse.ArgumentParser(description="Package Compass zips (NFC + UTF-8 safe)")
    ap.add_argument("date", nargs="?", default=None,
                    help="日期串如 20260510, 默认今天")
    ap.add_argument("--personal-only", action="store_true",
                    help="只打 personal, 跳过 template")
    ap.add_argument("--template-only", action="store_true",
                    help="只打 template, 跳过 personal")
    args = ap.parse_args()

    date_str = args.date or datetime.date.today().strftime("%Y%m%d")

    # 推算路径: 此脚本在 Compass/tools/, 上层 Compass 与 Compass_template 同目录
    here = Path(__file__).resolve()
    parent = here.parent.parent.parent  # 求职工具/
    active = parent / "Compass"
    template = parent / "Compass_template"

    if not active.is_dir():
        print(f"✗ 找不到 active 目录: {active}", file=sys.stderr)
        return 1

    print(f"打包日期: {date_str}")
    print()

    if not args.template_only:
        out = parent / f"Compass_personal_{date_str}.zip"
        n, total = build_zip(active, out)
        print(f"✓ {out.name}  ({n} files, {total/1024/1024:.2f} MB)")
        ok, issues = verify_zip(out)
        if not ok:
            print(f"  ⚠ 自检发现问题:")
            for i in issues: print(f"    - {i}")
        else:
            print(f"  ✓ 自检通过 (中文文件名 OK, 核心文档齐全)")

    if not args.personal_only:
        if not template.is_dir():
            print(f"⚠ 跳过 template (目录不存在): {template}")
        else:
            out = parent / f"Compass_template_{date_str}.zip"
            n, total = build_zip(template, out,
                                 extra_excludes=TEMPLATE_EXTRA_FILES,
                                 extra_globs=TEMPLATE_EXTRA_GLOBS)
            print(f"✓ {out.name}  ({n} files, {total/1024/1024:.2f} MB)")
            ok, issues = verify_zip(out)
            # PII 防御层（事故 2026-05-11：template 漏 uploads/ + contact_cache/）。
            # 主防线是 data/ 整目录排除；同时拦 uploads/templates_user PDF/DOCX 等。
            with zipfile.ZipFile(out) as zf:
                names = zf.namelist()
                pii_bad = [n for n in names
                           if n.endswith("/.env")
                           or "/data/" in n  # template 应整个 data/ 排空
                           # 用户上传的简历/文档不能出现在 template
                           or n.lower().endswith((".pdf", ".docx"))]
                if pii_bad:
                    print(f"  ✗ template 含敏感文件 ({len(pii_bad)} 条):")
                    for n in pii_bad[:5]:
                        print(f"      {n}")
                    if len(pii_bad) > 5:
                        print(f"      ... 还有 {len(pii_bad)-5} 条")
                    return 1
            if not ok:
                print(f"  ⚠ 自检发现问题:")
                for i in issues: print(f"    - {i}")
            else:
                print(f"  ✓ 自检通过 (无 PII + 中文 OK + 核心文档齐全)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
