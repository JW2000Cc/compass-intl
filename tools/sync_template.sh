#!/usr/bin/env bash
# tools/sync_template.sh — Compass/ → Compass_template/ 源码同步
#
# 用法: sync_template.sh
#
# 同步范围: 所有源码文件 (compass/, docs/, tools/, 顶层 .md / .py / .toml /
# requirements.txt 等)。明确不同步: data/, .env, .venv/, __pycache__/,
# .pytest_cache/, .git/, *.zip, .DS_Store, data_backup_*。
#
# 完成后跑 diff 验证只剩白名单内的差异。

set -e

SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DST_DIR="$(dirname "$SRC_DIR")/Compass_template"

if [ ! -d "$DST_DIR" ]; then
  echo "✗ 目标目录不存在: $DST_DIR"
  exit 1
fi

echo "════════════════════════════════════════════════════════"
echo "  sync: $SRC_DIR"
echo "    →   $DST_DIR"
echo "════════════════════════════════════════════════════════"

# rsync 排除清单
EXCLUDES=(
  --exclude='.venv/'
  --exclude='__pycache__/'
  --exclude='.pytest_cache/'
  --exclude='.git/'
  --exclude='data/'
  --exclude='data_backup_*/'
  --exclude='.env'
  --exclude='*.pyc'
  --exclude='*.pyo'
  --exclude='*.swp'
  --exclude='*.log'
  --exclude='*.zip'
  --exclude='.DS_Store'
)

# rsync -a (archive) -v (verbose minimal) --delete (镜像，删 dst 多出的)
# 注意: --delete 仅在排除项之外生效，data/ 等被 exclude 的不会被删
echo ""
echo "rsync 同步 (镜像模式, 删除 dst 多余项)..."
rsync -a --delete "${EXCLUDES[@]}" \
  "$SRC_DIR/" "$DST_DIR/" 2>&1 | head -20

echo ""
echo "════════════════════════════════════════════════════════"
echo "  diff 验证 (期望只剩白名单)"
echo "════════════════════════════════════════════════════════"

diff_out=$(diff -rq "$SRC_DIR" "$DST_DIR" \
  --exclude='.venv' --exclude='__pycache__' --exclude='data' \
  --exclude='*.pyc' --exclude='*.log' --exclude='.git' \
  --exclude='.pytest_cache' --exclude='.env' \
  --exclude='data_backup_*' --exclude='*.zip' \
  --exclude='.DS_Store' 2>&1 || true)

if [ -z "$diff_out" ]; then
  echo "✓ 同步干净 — 仅白名单内差异"
  exit 0
else
  diff_count=$(echo "$diff_out" | grep -c .)
  echo "⚠ 仍有 $diff_count 处差异 (检查):"
  echo "$diff_out" | head -10
  echo ""
  echo "如果都是 .env / data / .git 等白名单, OK; 否则 rsync 排除清单需补"
  exit 1
fi
