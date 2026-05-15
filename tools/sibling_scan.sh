#!/usr/bin/env bash
# tools/sibling_scan.sh — 跨程序 pattern 扫描
#
# 用法:
#   sibling_scan.sh '<regex>'                    # 扫所有源码 + 模板
#   sibling_scan.sh '<regex>' programs           # 仅扫 .sh / .command / .bat
#   sibling_scan.sh '<regex>' --files-only       # 仅列文件名，不展开
#
# 例:
#   sibling_scan.sh 'unpkg\.com|cdnjs|jsdelivr'  # CDN 引用回归扫
#   sibling_scan.sh 'scrape_jobs|page\.goto|requests\.get|httpx\.\w+\(' programs
#   sibling_scan.sh 'sk-fake-for-audit|placeholder|REPLACE_ME'
#
# 设计原则:
#   - 三个程序写死路径 (个人项目，跨机器移植成本高于硬编码价值)
#   - 默认扫源码 + 模板 + 配置 (data/ 跳过, 那是用户数据)
#   - 输出每程序的命中数 + 命中位置, 便于快速判断"是否需要同步修"

set -e

REGEX="${1:?Usage: $0 '<regex>' [programs|--files-only]}"
SCOPE="${2:-source}"

# 三个程序的根目录 (按 memory: project_programs_folder)
declare -a PROGS=(
  "Compass:$HOME/Desktop/Programs/求职工具/Compass"
  "Sundial:$HOME/Desktop/Programs/时间反思工具/Sundial"
  "Visa Sniper:$HOME/Desktop/Programs/Visa Sniper/Visa Sniper"
)

# 排除的目录 / 文件 pattern
# - 默认不扫 docs/ (文档示例 / 历史 plan 不是 runtime)
# - 默认不扫 static/js/vendor/ (库文件内部注释引用 CDN URL 合理)
# 想扫 docs/ 时可显式 --include-docs (未实现, 直接 grep)
EXCLUDE_DIRS=(".venv" "__pycache__" ".pytest_cache" ".git" "data" "data_backup_*" "node_modules" "*.zip" "docs" "vendor")
EXCLUDE_FILES=("*.pyc" "*.pyo" "*.swp" ".DS_Store" "*.sqlite*" "*.log")

# 拼 grep 的 --exclude-dir / --exclude 参数
GREP_ARGS=()
for d in "${EXCLUDE_DIRS[@]}"; do GREP_ARGS+=("--exclude-dir=$d"); done
for f in "${EXCLUDE_FILES[@]}"; do GREP_ARGS+=("--exclude=$f"); done

# Scope-specific 文件模式
# 默认 source: 仅扫 .py / .html / .js / .css / .json / .yaml / .toml — 即 runtime 影响面
# (不扫 .md / .txt 因为文档/CHANGELOG 自我描述 "之前用 unpkg" 是 false positive)
case "$SCOPE" in
  programs)
    INCLUDE_GLOBS=("--include=*.sh" "--include=*.command" "--include=*.bat")
    ;;
  --files-only)
    EXTRA_FLAGS=("-l")
    INCLUDE_GLOBS=("--include=*.py" "--include=*.html" "--include=*.js" "--include=*.css" "--include=*.json" "--include=*.yaml" "--include=*.toml")
    ;;
  --all)
    INCLUDE_GLOBS=()  # 扫所有文本文件 (含 .md / .txt)
    ;;
  *)
    INCLUDE_GLOBS=("--include=*.py" "--include=*.html" "--include=*.js" "--include=*.css" "--include=*.json" "--include=*.yaml" "--include=*.toml")
    ;;
esac

echo "════════════════════════════════════════════════════════"
echo "  Sibling pattern scan"
echo "  pattern: $REGEX"
echo "  scope:   $SCOPE"
echo "════════════════════════════════════════════════════════"

TOTAL_HITS=0
for entry in "${PROGS[@]}"; do
  name="${entry%%:*}"
  path="${entry#*:}"
  printf "\n[ %-12s ] %s\n" "$name" "$path"
  if [ ! -d "$path" ]; then
    echo "  ⚠ 目录不存在，跳过"
    continue
  fi
  # grep -r with all the exclusions; -E for extended regex
  hits=$(grep -rEn "$REGEX" "$path" \
    "${GREP_ARGS[@]}" "${INCLUDE_GLOBS[@]}" \
    ${EXTRA_FLAGS[@]+"${EXTRA_FLAGS[@]}"} \
    2>/dev/null | head -30)
  if [ -z "$hits" ]; then
    echo "  ✓ 0 命中"
  else
    cnt=$(echo "$hits" | grep -c .)
    echo "  ⚠ $cnt 命中:"
    echo "$hits" | sed "s|$path/|    |"
    TOTAL_HITS=$((TOTAL_HITS + cnt))
  fi
done

echo ""
echo "════════════════════════════════════════════════════════"
echo "  总计: $TOTAL_HITS 命中"
echo "════════════════════════════════════════════════════════"

# Exit code: 0 if no hits (regression baseline OK), 1 otherwise
[ $TOTAL_HITS -eq 0 ]
