#!/usr/bin/env bash
# tools/release_check.sh — Compass 发布门禁
#
# 用法:
#   release_check.sh                # 默认 --full：全部 12 项检查
#   release_check.sh --baseline     # 只读模式，给"动手前"用，确认基线绿
#   release_check.sh --diff         # 跑一次 + 跟上次 baseline.json 比，只报新增红灯
#   release_check.sh --quick        # 只跑 Tier 1（5 项 < 30 秒）
#
# 退出码: 0 = 全绿; 1 = 任何阻断项失败
#
# 设计原则:
#   - 不假设有 pytest fixture / framework，所有检查都自包含
#   - 不依赖外部账户/密钥（除非已配置 .env，那是用户责任）
#   - 启动 Compass 跑 boot smoke 时使用临时端口，不冲突活跃实例
#   - 状态写到 data/.release_check_baseline.json (data/ 在 .gitignore)

set -e

MODE="${1:---full}"
DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$DIR"

# ── 颜色 ────────────────────────────────────────────────────────────
GREEN=$'\033[0;32m'
RED=$'\033[0;31m'
YELLOW=$'\033[0;33m'
GREY=$'\033[0;90m'
RESET=$'\033[0m'

PASS_COUNT=0
FAIL_COUNT=0
WARN_COUNT=0
declare -a FAILED=()
declare -a WARNED=()

ok()    { printf "  ${GREEN}✓${RESET} %s\n" "$1"; PASS_COUNT=$((PASS_COUNT+1)); }
fail()  { printf "  ${RED}✗${RESET} %s\n      %s\n" "$1" "${2:-}"; FAIL_COUNT=$((FAIL_COUNT+1)); FAILED+=("$1"); }
warn()  { printf "  ${YELLOW}⚠${RESET} %s\n      %s\n" "$1" "${2:-}"; WARN_COUNT=$((WARN_COUNT+1)); WARNED+=("$1"); }
skip()  { printf "  ${GREY}–${RESET} %s ${GREY}(%s)${RESET}\n" "$1" "${2:-skipped}"; }
section(){ printf "\n${GREY}── %s ─────────────────────────${RESET}\n" "$1"; }

# 端口选择: 临时实例用 7099（不冲突活跃 7000）
TMP_PORT=7099

# ── 辅助 ────────────────────────────────────────────────────────────
need() {
  local cmd="$1"
  command -v "$cmd" >/dev/null 2>&1 || { fail "缺命令: $cmd" "请先 brew install"; return 1; }
}
boot_compass_temp() {
  # 启动临时实例到 TMP_PORT，等就绪
  rm -f /tmp/compass_release_check.log
  COMPASS_PORT=$TMP_PORT "$DIR/.venv/bin/python" -m compass.run \
    > /tmp/compass_release_check.log 2>&1 &
  TMP_PID=$!
  for i in 1 2 3 4 5 6 7 8 9 10; do
    sleep 1
    if /usr/bin/curl -sf "http://127.0.0.1:$TMP_PORT/healthz" -o /dev/null; then
      return 0
    fi
  done
  return 1
}
stop_compass_temp() {
  if [ -n "${TMP_PID:-}" ] && kill -0 "$TMP_PID" 2>/dev/null; then
    kill "$TMP_PID" 2>/dev/null || true
    sleep 1
  fi
}

# 安全退出: kill 临时进程
trap 'stop_compass_temp' EXIT

# ── 启动横幅 ────────────────────────────────────────────────────────
echo "════════════════════════════════════════════════════════"
echo "  Compass 发布门禁  ·  mode=${MODE}"
echo "  $(date '+%Y-%m-%d %H:%M:%S')"
echo "════════════════════════════════════════════════════════"

# ────────────────────────────────────────────────────────────────────
# Tier 1: 静态 + 启动 smoke (5 项, 30 秒内)
# ────────────────────────────────────────────────────────────────────

section "Tier 1: 静态检查"

# [1] Python 语法
if "$DIR/.venv/bin/python" -m compileall -q compass/ 2>/dev/null; then
  ok "[1] Python 语法 (compileall compass/)"
else
  fail "[1] Python 语法" "compileall 失败，跑 'python -m compileall compass/' 看具体错"
fi

# [2] CDN 引用回归扫
cdn_hits=$(grep -rEon "unpkg\.com|cdnjs\.cloudflare|jsdelivr\.net" \
  compass/templates/ 2>/dev/null | wc -l | tr -d ' ')
if [ "$cdn_hits" -eq 0 ]; then
  ok "[2] 无 CDN 引用回归 (templates 不含 unpkg/cdnjs/jsdelivr)"
else
  fail "[2] CDN 引用回归" "templates 中发现 $cdn_hits 处 CDN 引用，运行 grep 查看"
fi

# [3] 残余调试代码
debug_hits=$(grep -rE "console\.log|^[[:space:]]*print\([\"']debug" \
  compass/ --include='*.py' --include='*.html' \
  2>/dev/null | grep -vE "^.*tests/|^.*node_modules" | wc -l | tr -d ' ')
if [ "$debug_hits" -eq 0 ]; then
  ok "[3] 无残余调试代码 (console.log / print('debug...))"
elif [ "$debug_hits" -lt 5 ]; then
  warn "[3] 残余调试代码" "$debug_hits 处，可接受但建议清理"
else
  fail "[3] 残余调试代码" "$debug_hits 处过多"
fi

# [4] Audit 占位符扫描 (P-003)
# 只扫真正存运行时配置的位置：data/*.json (llm_settings 等) + .env* — 这是
# audit fake key 落地的地方。源码里 compass/app.py _PLACEHOLDERS 集合含字面量
# "sk-fake-for-audit"，那是占位符识别白名单 (反向过滤用)，不是污染。
audit_hits=$(grep -rEon "sk-fake-for-audit|REPLACE_ME_BEFORE|<your-key-here>" \
  data/*.json .env .env.example 2>/dev/null | \
  grep -v ".audit_bak\|.audit_residue\|.bak_" | \
  wc -l | tr -d ' ')
if [ "$audit_hits" -eq 0 ]; then
  ok "[4] 活跃配置无 audit 占位符 (P-003 防回归)"
else
  fail "[4] Audit 占位符回归" "$audit_hits 处活跃文件含 fake key 占位符 (排除 .bak)"
fi

# [5] active vs template diff 在白名单内
section "Tier 1: 文件同步"
TEMPLATE_DIR="$(dirname "$DIR")/Compass_template"
if [ -d "$TEMPLATE_DIR" ]; then
  diff_out=$(diff -rq . "$TEMPLATE_DIR" \
    --exclude=".venv" --exclude="__pycache__" --exclude="data" \
    --exclude="*.pyc" --exclude="*.log" --exclude=".git" \
    --exclude=".pytest_cache" --exclude=".env" \
    --exclude="data_backup_*" --exclude="*.zip" \
    --exclude=".DS_Store" 2>&1 || true)
  if [ -z "$diff_out" ]; then
    ok "[5] active <-> template 同步 (diff 在白名单内)"
  else
    diff_count=$(echo "$diff_out" | grep -c .)
    warn "[5] active <-> template 有差异" "$diff_count 处，跑 sync_template.sh 同步"
  fi
else
  skip "[5] active vs template diff" "无 template 目录"
fi

# [6] P-004 验证陷阱: tools/ 中禁用 unzip -l (Apple infozip 不读 bit11)
unzip_l_hits=$(grep -rEn "unzip[[:space:]]+-l" tools/ \
  --include='*.sh' --include='*.py' --include='*.command' \
  --exclude='release_check.sh' \
  2>/dev/null | wc -l | tr -d ' ')
if [ "$unzip_l_hits" -eq 0 ]; then
  ok "[6] P-004 验证: tools/ 无 unzip -l (用 zipfile.infolist 或实际解压)"
else
  fail "[6] P-004 验证陷阱" "tools/ 含 $unzip_l_hits 处 unzip -l — 见 docs/bug-patterns.md P-004 验证陷阱"
fi

# 结束 Tier 1
if [ "$MODE" = "--quick" ]; then
  section "结果"
  printf "  ${GREEN}✓ %d 通过${RESET}  ${RED}✗ %d 失败${RESET}  ${YELLOW}⚠ %d 警告${RESET}\n" \
    "$PASS_COUNT" "$FAIL_COUNT" "$WARN_COUNT"
  exit $([ "$FAIL_COUNT" -eq 0 ] && echo 0 || echo 1)
fi

# ────────────────────────────────────────────────────────────────────
# Tier 2: 运行时 boot smoke + 业务逻辑
# ────────────────────────────────────────────────────────────────────

section "Tier 2: 启动 + 路由 smoke"

if ! [ -x "$DIR/.venv/bin/python" ]; then
  fail "[boot] venv 不存在" "先双击启动器自动建 venv"
  echo ""
  echo "════════════════════════════════════════════════════════"
  echo "  ABORT — 后续 Tier 2 检查需要 venv"
  echo "════════════════════════════════════════════════════════"
  exit 1
fi

# 临时实例: 占用 TMP_PORT 防冲突活跃 7000
if boot_compass_temp; then
  ok "[6] 启动临时实例 (port $TMP_PORT)"
else
  fail "[6] 启动临时实例失败" "看 /tmp/compass_release_check.log"
  echo ""
  exit 1
fi

# [7] 17 核心页面探活
PAGES=(/ /jobs/ /resume/ /reflect/dashboard /reflect/assumptions /identity/ \
       /feedback/ /calibration/ /funnel/ /negotiation/ /rejection/ \
       /jobs/search-config/ /jobs/llm-settings/ /blacklist/ /gmail/ \
       /connections/ /healthz)
page_fails=0
for p in "${PAGES[@]}"; do
  code=$(/usr/bin/curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:$TMP_PORT$p")
  case "$code" in
    200|302) ;;
    *) page_fails=$((page_fails+1)); ;;
  esac
done
if [ "$page_fails" -eq 0 ]; then
  ok "[7] 17 核心页面全部 200/302"
else
  fail "[7] $page_fails 个页面非 200/302" "手动 curl 查看具体页"
fi

# [8] healthz?detail=1 全绿
health=$(/usr/bin/curl -s "http://127.0.0.1:$TMP_PORT/healthz?detail=1")
status=$(echo "$health" | python3 -c "import json,sys; print(json.loads(sys.stdin.read()).get('status','?'))" 2>/dev/null || echo "?")
if [ "$status" = "ok" ]; then
  ok "[8] /healthz?detail=1 status=ok"
else
  fail "[8] /healthz?detail=1 status=$status" "$(echo "$health" | head -c 300)"
fi

# [9] LLM ping (会真打 Anthropic, 用户允许才跑)
if [ -f "$DIR/.env" ] && grep -qE "^LLM_API_KEY=sk-" "$DIR/.env" 2>/dev/null; then
  llm_resp=$(/usr/bin/curl -s -X POST \
    "http://127.0.0.1:$TMP_PORT/jobs/llm-settings/test" \
    -H "Content-Type: application/json" -d '{}')
  ok_flag=$(echo "$llm_resp" | python3 -c "import json,sys; print(json.loads(sys.stdin.read()).get('ok',False))" 2>/dev/null)
  if [ "$ok_flag" = "True" ]; then
    ok "[9] LLM ping (.env key 真活, pong 返回)"
  else
    fail "[9] LLM ping 失败" "$(echo "$llm_resp" | head -c 200)"
  fi
else
  skip "[9] LLM ping" "无 .env 或无 LLM_API_KEY，跳过"
fi

# [10] /jobs/api/scrape/progress 端点活
prog_code=$(/usr/bin/curl -s -o /dev/null -w "%{http_code}" \
  "http://127.0.0.1:$TMP_PORT/jobs/api/scrape/progress")
if [ "$prog_code" = "200" ]; then
  ok "[10] /jobs/api/scrape/progress 端点活"
else
  fail "[10] progress 端点 $prog_code" "5-10 新加端点, 应为 200"
fi

# [11] 4 个 scrape form 都标 data-progress-source="scrape"
section "Tier 2: 一致性"
forms_marked=$(grep -rln 'data-progress-source="scrape"' compass/templates/ 2>/dev/null | wc -l | tr -d ' ')
if [ "$forms_marked" -ge 4 ]; then
  ok "[11] 全部 scrape form 含 data-progress-source 标记 ($forms_marked 处)"
else
  fail "[11] scrape form 标记不全" "找到 $forms_marked 处, 期望 ≥4"
fi

# [12] 文档 ↔ 代码漂移
section "Tier 2: 文档漂移"
drift_warn=0
for kw in "进度浮层" "离线指示器" "90 秒超时" "vendor"; do
  if grep -qE "$kw|${kw// /}|$(echo $kw | tr -d ' ')" 使用说明.md 2>/dev/null; then
    : # 文档提到了
  else
    drift_warn=$((drift_warn+1))
    warn "[12] 关键功能 \"$kw\" 在使用说明.md 中未明确提及"
  fi
done
if [ "$drift_warn" -eq 0 ]; then
  ok "[12] 使用说明.md 覆盖 5-10 关键功能"
fi

# Tier 2 stop
stop_compass_temp

# ────────────────────────────────────────────────────────────────────
# Mode-specific: --baseline 保存当前态; --diff 对比
# ────────────────────────────────────────────────────────────────────

BASELINE_PATH="$DIR/data/.release_check_baseline.json"

if [ "$MODE" = "--baseline" ]; then
  mkdir -p "$DIR/data"
  cat > "$BASELINE_PATH" <<EOF
{
  "timestamp": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "pass": $PASS_COUNT,
  "fail": $FAIL_COUNT,
  "warn": $WARN_COUNT,
  "failed": [$(printf '"%s",' "${FAILED[@]}" | sed 's/,$//')],
  "warned": [$(printf '"%s",' "${WARNED[@]}" | sed 's/,$//')]
}
EOF
  echo ""
  printf "${GREY}baseline 已存到 %s${RESET}\n" "$BASELINE_PATH"
fi

if [ "$MODE" = "--diff" ] && [ -f "$BASELINE_PATH" ]; then
  prev_fail=$(python3 -c "import json; print(json.load(open('$BASELINE_PATH'))['fail'])")
  delta=$((FAIL_COUNT - prev_fail))
  echo ""
  if [ "$delta" -le 0 ]; then
    printf "${GREEN}✓ 较 baseline 无新增红灯 (delta=$delta)${RESET}\n"
  else
    printf "${RED}✗ 较 baseline 新增 $delta 红灯${RESET}\n"
  fi
fi

# ────────────────────────────────────────────────────────────────────
# 结果汇总
# ────────────────────────────────────────────────────────────────────
section "结果"
printf "  ${GREEN}✓ %d 通过${RESET}  ${RED}✗ %d 失败${RESET}  ${YELLOW}⚠ %d 警告${RESET}\n\n" \
  "$PASS_COUNT" "$FAIL_COUNT" "$WARN_COUNT"

if [ "$FAIL_COUNT" -gt 0 ]; then
  echo "失败项:"
  for f in "${FAILED[@]}"; do printf "  ${RED}✗${RESET} %s\n" "$f"; done
  echo ""
fi

# 提醒手动 smoke checklist
if [ "$MODE" != "--baseline" ] && [ "$MODE" != "--quick" ]; then
  echo "${GREY}最后还需走一遍人眼 smoke (3 分钟):${RESET}"
  echo "${GREY}  cat docs/smoke-checklist.md${RESET}"
  echo ""
fi

exit $([ "$FAIL_COUNT" -eq 0 ] && echo 0 || echo 1)
