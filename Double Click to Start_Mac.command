#!/bin/bash
# Compass — Mac 启动脚本
# Self-locating; data lives next to this script.

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

# Remove macOS quarantine attribute if present (downloaded zip)
xattr -d com.apple.quarantine "$0" 2>/dev/null || true
xattr -dr com.apple.quarantine "$DIR" 2>/dev/null || true

# Prepend Homebrew paths (Finder doesn't load shell PATH)
export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:$PATH"

mkdir -p "$DIR/data"
LOG="$DIR/data/startup.log"

echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║          Compass · 指南 启动中...            ║"
echo "╚══════════════════════════════════════════════╝"
echo ""
echo "[$(date)] starting" > "$LOG"

# ---- [1/5] Check Python 3.10–3.13 -------------------------------
# 上限 3.13: 3.14 上 numpy/jobspy 还没预编译 wheel, 装会触发 meson 源编译
# 然后在 Windows 中文路径上 UnicodeDecodeError 爆掉 (5-11 事故)
MIN_VER=310
MAX_VER=313
echo "[ 1/5 ] Checking Python..."
PYTHON_CMD=""
TOO_NEW_VER=""
for cmd in python3.13 python3.12 python3.11 python3.10 python3 python; do
  if command -v "$cmd" >/dev/null 2>&1; then
    VER=$("$cmd" -c 'import sys; print(sys.version_info[0]*100+sys.version_info[1])' 2>/dev/null)
    if [ -n "$VER" ]; then
      if [ "$VER" -gt $MAX_VER ] 2>/dev/null; then
        TOO_NEW_VER="$VER"
      elif [ "$VER" -ge $MIN_VER ] 2>/dev/null; then
        PYTHON_CMD="$cmd"
        break
      fi
    fi
  fi
done

if [ -z "$PYTHON_CMD" ]; then
  if [ -n "$TOO_NEW_VER" ]; then
    MAJ=$((TOO_NEW_VER / 100)); MIN_=$((TOO_NEW_VER % 100))
    echo "        ✗ Python $MAJ.$MIN_ 太新, 部分依赖 (numpy/jobspy) 还没预编译 wheel"
    echo "          请装 Python 3.10–3.13:"
    echo "            brew install python@3.12"
    echo "          (3.14+ 等上游包跟上后再开放)"
  else
    echo "        ✗ Python 3.10–3.13 not found."
    echo "          Install with: brew install python@3.12"
    echo "          Or download: https://www.python.org/downloads/"
  fi
  read -p "Press Enter to exit..."
  exit 1
fi
echo "        ✓ $PYTHON_CMD ($($PYTHON_CMD --version 2>&1))"

# ---- [2/5] Setup venv (self-heal: catches the typical "folder got moved"
# failure — pip's shebang is a hardcoded absolute path,移动后 pip 就跑不起来。
# 注意:不查 sys.prefix——Python 启动时会从 sys.executable 重算,反而会被
# /tmp ↔ /private/tmp 这类 symlink 假阳性触发。)
echo "[ 2/5 ] Checking virtual environment..."
EXPECTED_VENV="$DIR/.venv"
NEED_REBUILD=0
REBUILD_REASON=""
if [ -d "$EXPECTED_VENV" ]; then
  if [ ! -x "$EXPECTED_VENV/bin/python" ]; then
    NEED_REBUILD=1; REBUILD_REASON="python missing"
  elif ! "$EXPECTED_VENV/bin/python" --version >/dev/null 2>&1; then
    NEED_REBUILD=1; REBUILD_REASON="python broken (folder may have been moved)"
  elif ! "$EXPECTED_VENV/bin/pip" --version >/dev/null 2>&1; then
    NEED_REBUILD=1; REBUILD_REASON="pip shebang stale (folder was moved)"
  else
    VENV_VER=$("$EXPECTED_VENV/bin/python" -c "import sys; v=sys.version_info; print(v.major*100+v.minor)" 2>/dev/null || echo "0")
    if [ "$VENV_VER" -lt $MIN_VER ]; then
      NEED_REBUILD=1; REBUILD_REASON="Python too old"
    elif [ "$VENV_VER" -gt $MAX_VER ]; then
      NEED_REBUILD=1; REBUILD_REASON="Python too new (venv=$VENV_VER, 上限=$MAX_VER)"
    fi
  fi
  if [ "$NEED_REBUILD" = "1" ]; then
    echo "        venv unhealthy: $REBUILD_REASON — rebuilding"
    echo "[$(date)] venv rebuild: $REBUILD_REASON" >> "$LOG"
    rm -rf "$EXPECTED_VENV"
  fi
fi
if [ ! -d "$EXPECTED_VENV" ]; then
  echo "        Creating .venv (first run, ~30s)..."
  "$PYTHON_CMD" -m venv "$EXPECTED_VENV" 2>&1 | tee -a "$LOG"
fi
VENV_PY="$EXPECTED_VENV/bin/python"
echo "        ✓ venv ready"

# ---- [3/5] Install dependencies ---------------------------------
echo "[ 3/5 ] Checking dependencies..."
NEED_INSTALL=0
if ! "$VENV_PY" -c "import flask, sqlalchemy, anthropic" >/dev/null 2>&1; then
  NEED_INSTALL=1
fi
if [ $NEED_INSTALL -eq 1 ]; then
  echo "        Installing (first run, 1–2 min)..."
  "$VENV_PY" -m pip install --upgrade pip --quiet 2>&1 | tee -a "$LOG"
  "$VENV_PY" -m pip install -r "$DIR/requirements.txt" 2>&1 | tee -a "$LOG"
fi
echo "        ✓ dependencies ready"

# ---- [4/5] startup check (verify deps + .env + module imports) ---
echo "[ 4/5 ] Running startup checks..."
"$VENV_PY" "$DIR/_startup_check.py" 2>&1 | tee -a "$LOG"
CHECK_RC=${PIPESTATUS[0]}
if [ "$CHECK_RC" != "0" ]; then
  echo ""
  echo "  ✗ Startup check failed."
  echo "  Common cause: .env LLM_API_KEY missing — open .env, paste key, save, retry."
  echo "  See $LOG for full output."
  echo ""
  [ -f "$DIR/.env" ] && open -a TextEdit "$DIR/.env" 2>/dev/null || open -a TextEdit "$LOG" 2>/dev/null
  read -rp "  Press Enter to exit..."
  exit 1
fi

# ---- [5/5] Start Compass ----------------------------------------
echo "[ 5/5 ] Starting Compass..."

# Kill any stale process on our port (default 7000)
PORT=$(grep -E "^COMPASS_PORT=" "$DIR/.env" 2>/dev/null | cut -d= -f2 | tr -d '"' | tr -d "'" )
PORT="${PORT:-7000}"
EXISTING=$(lsof -ti:"$PORT" 2>/dev/null || true)
if [ -n "$EXISTING" ]; then
  echo "        Killing stale process on :$PORT (pid $EXISTING)..."
  kill -9 $EXISTING 2>/dev/null || true
  sleep 1
fi

echo ""
echo "  Compass running at http://127.0.0.1:$PORT"
echo "  Press Ctrl+C to stop."
echo ""

# Open browser after 2s (in background)
( sleep 2 && open "http://127.0.0.1:$PORT" ) &

exec "$VENV_PY" -m compass.run
