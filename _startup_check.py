"""Startup diagnostic - run before launching Compass.

Mirrors Sundial/_startup_check.py for sibling consistency.
Exit 0 = all good. Exit 1 = problem found, user should see startup.log.
"""
import importlib
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).parent
errors: list[str] = []

print("=" * 50)
print("Compass Startup Check")
print("=" * 50)

# 1. Python version (告示, .bat/.command 已挡过 3.10-3.13)
print(f"Python: {sys.version}")

# 2. ZoneInfo (Compass 用时区显示时间)
try:
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("Europe/Rome")
    print(f"[OK] ZoneInfo: {tz}")
except Exception as e:
    print(f"[FAIL] ZoneInfo: {e}")
    errors.append(f"ZoneInfo failed: {e}")

# 3. 核心 Python 包
# importlib.import_module 而不是 __import__,
# 因为 __import__('google.auth') 在 google-auth 没装时也返回 google namespace
for pkg in ["flask", "sqlalchemy", "alembic", "pydantic", "dotenv",
            "anthropic", "openai", "httpx", "jinja2"]:
    try:
        importlib.import_module(pkg)
        print(f"[OK] {pkg}")
    except ImportError as e:
        print(f"[FAIL] {pkg}: {e}")
        errors.append(f"Missing package: {pkg}")

# 4. .env  (LLM_API_KEY 必填，其它都有 default)
env_path = ROOT / ".env"
_PLACEHOLDERS = {"", "your-api-key-here", "your_key_here", "change-me"}
if env_path.exists():
    content = env_path.read_text(encoding="utf-8", errors="replace")
    def _val(prefix):
        for l in content.splitlines():
            if l.startswith(prefix):
                v = l[len(prefix):].split("#")[0].strip()  # 容忍行尾注释
                return v if v not in _PLACEHOLDERS else ""
        return ""
    key_val = _val("LLM_API_KEY=")
    has_key = bool(key_val)
    print(f"[OK] .env found | LLM_API_KEY={'set' if has_key else 'EMPTY/PLACEHOLDER'}")
    # .env 值缺失不算 error — Compass dashboard 会启 web wizard (/setup) 接管。
else:
    print("[INFO] .env not found — first-run web wizard will create it on browser open")

# 5. compass 模块能 import (verify code itself isn't broken)
print("Testing compass module import...")
try:
    importlib.import_module("compass")
    print("[OK] compass module imports")
except Exception:
    tb = traceback.format_exc()
    print("[FAIL] compass import error:")
    print(tb)
    errors.append("compass module import failed")

# 6. data/ 可写
print("Testing data/ write access...")
try:
    test_path = ROOT / "data" / ".write_test"
    test_path.parent.mkdir(exist_ok=True)
    test_path.write_text("ok", encoding="utf-8")
    test_path.unlink()
    print("[OK] data/ writable")
except Exception as e:
    print(f"[FAIL] data/ not writable: {e}")
    errors.append(f"data/ not writable: {e}")

print("=" * 50)
if errors:
    print(f"FOUND {len(errors)} PROBLEM(S):")
    for i, e in enumerate(errors, 1):
        print(f"  {i}. {e}")
    print("=" * 50)
    sys.exit(1)
else:
    print("ALL CHECKS PASSED - ready to start")
    print("=" * 50)
    sys.exit(0)
