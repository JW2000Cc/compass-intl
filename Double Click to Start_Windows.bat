@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul 2>&1
cd /d "%~dp0"

set PYTHONIOENCODING=utf-8
rem ================================================================
rem  Compass -- Windows 启动脚本
rem  Lessons applied (from prior bot debugging):
rem    - py.exe vs python.exe detection
rem    - port-stale process cleanup
rem    - venv detection (Scripts\ on Win, bin/ on Mac)
rem    - startup.log for diagnostics
rem    - chcp 65001 for UTF-8 (中文 won't garble)
rem    - sequential checks; bail out clearly on failure
rem ================================================================

if not exist data mkdir data
set LOG=data\startup.log
echo [%DATE% %TIME%] starting > "%LOG%"

echo.
echo ===============================================
echo         Compass . starting...
echo ===============================================
echo.

rem ---- [1/5] Python detect (3.10-3.13) ---------------------------
rem 上限 3.13: 3.14 上 numpy/jobspy 没预编译 wheel, 源编译会在中文路径
rem 上 UnicodeDecodeError 爆掉 (5-11 事故)
echo [1/5] Checking Python...
echo [1/5] Checking Python... >> "%LOG%"
set PYTHON=
set MIN_VER=310
set MAX_VER=313
set TOO_NEW_VER=

py --version >nul 2>&1
if not errorlevel 1 (
    for /f %%v in ('py -c "import sys;v=sys.version_info;print(v.major*100+v.minor)" 2^>nul') do set VER=%%v
    if defined VER (
        if !VER! GTR %MAX_VER% (
            set TOO_NEW_VER=!VER!
        ) else if !VER! GEQ %MIN_VER% (
            set PYTHON=py
            goto :found_python
        )
    )
)
python --version >nul 2>&1
if not errorlevel 1 (
    for /f %%v in ('python -c "import sys;v=sys.version_info;print(v.major*100+v.minor)" 2^>nul') do set VER=%%v
    if defined VER (
        if !VER! GTR %MAX_VER% (
            set TOO_NEW_VER=!VER!
        ) else if !VER! GEQ %MIN_VER% (
            set PYTHON=python
            goto :found_python
        )
    )
)

if defined TOO_NEW_VER (
    set /a MAJ=!TOO_NEW_VER! / 100
    set /a MNR=!TOO_NEW_VER! %% 100
    echo        X Python !MAJ!.!MNR! 太新, 部分依赖 ^(numpy/jobspy^) 还没预编译 wheel
    echo          请装 Python 3.10-3.13:
    echo            winget install Python.Python.3.12
    echo          ^(3.14+ 等上游包跟上后再开放^)
    echo Python !MAJ!.!MNR! too new ^(need 3.10-3.13^) >> "%LOG%"
) else (
    echo        X Python 3.10-3.13 not found.
    echo          Install from https://www.python.org/downloads/  ^(check "Add Python to PATH"^)
    echo          Or via winget: winget install Python.Python.3.12
    echo          ^(After install, close this window and restart this .bat^)
    echo Python missing >> "%LOG%"
)
pause
exit /b 1

:found_python
for /f "tokens=*" %%v in ('!PYTHON! --version 2^>^&1') do set PYVER=%%v
echo        OK !PYTHON! ^(!PYVER!^)
echo OK !PYTHON! ^(!PYVER!^) >> "%LOG%"

rem ---- [2/5] venv setup (self-heal: pip shebang dies when folder moves) ----
echo [2/5] Checking virtual environment...
set NEED_REBUILD=0
set REBUILD_REASON=
if exist .venv\Scripts\python.exe (
    rem Check 1: python runs?
    .venv\Scripts\python.exe --version >nul 2>&1
    if errorlevel 1 (
        set NEED_REBUILD=1
        set REBUILD_REASON=python broken
    ) else (
        rem Check 2: pip works? (shebang stale = folder was moved)
        .venv\Scripts\pip.exe --version >nul 2>&1
        if errorlevel 1 (
            set NEED_REBUILD=1
            set REBUILD_REASON=pip shebang stale ^(folder moved?^)
        ) else (
            rem Check 3: Python version in supported range [MIN_VER, MAX_VER]?
            for /f %%v in ('.venv\Scripts\python.exe -c "import sys;v=sys.version_info;print(v.major*100+v.minor)" 2^>nul') do set VENV_VER=%%v
            if defined VENV_VER (
                if !VENV_VER! LSS %MIN_VER% (
                    set NEED_REBUILD=1
                    set REBUILD_REASON=Python too old
                ) else if !VENV_VER! GTR %MAX_VER% (
                    set NEED_REBUILD=1
                    set REBUILD_REASON=Python too new ^(venv=!VENV_VER!, 上限=%MAX_VER%^)
                )
            )
        )
    )
    if !NEED_REBUILD! EQU 1 (
        echo        venv unhealthy: !REBUILD_REASON! -- rebuilding
        echo [%DATE% %TIME%] venv rebuild: !REBUILD_REASON! >> "%LOG%"
        rmdir /s /q .venv 2>nul
    )
)
if not exist .venv\Scripts\python.exe (
    echo        Creating .venv ^(first run, ~30s^)...
    !PYTHON! -m venv .venv >> "%LOG%" 2>&1
    if errorlevel 1 (
        echo        X venv creation failed -- see %LOG%
        pause
        exit /b 1
    )
)
set VENV_PY=.venv\Scripts\python.exe
echo        OK venv ready

rem ---- [3/5] dependencies ----------------------------------------
echo [3/5] Checking dependencies...
"%VENV_PY%" -c "import flask, sqlalchemy, anthropic" >nul 2>&1
if errorlevel 1 (
    echo        Installing ^(first run, 1-2 min^)...
    "%VENV_PY%" -m pip install --upgrade pip --quiet >> "%LOG%" 2>&1
    "%VENV_PY%" -m pip install -r requirements.txt >> "%LOG%" 2>&1
    if errorlevel 1 (
        echo        X dependency install failed -- see %LOG%
        pause
        exit /b 1
    )
)
echo        OK dependencies ready

rem ---- [4/5] startup check (verify deps + .env + module imports) ---
echo [4/5] Running startup checks...
"%VENV_PY%" _startup_check.py > startup_check.tmp 2>&1
set CHECK_RC=!errorlevel!
type startup_check.tmp
type startup_check.tmp >> "%LOG%"
del startup_check.tmp >nul 2>&1
if !CHECK_RC! NEQ 0 (
    echo.
    echo   [ERROR] Startup check failed.
    echo   Common cause: .env LLM_API_KEY missing - open .env, paste key, save, retry.
    echo   See %LOG% for full output.
    echo.
    if exist .env ( start notepad .env ) else ( start notepad %LOG% )
    pause
    exit /b 1
)

rem ---- [5/5] kill stale process on port + launch -----------------
echo [5/5] Starting Compass...

rem Read port from .env if present, else default 7000
set PORT=7000
if exist .env (
    for /f "tokens=2 delims==" %%p in ('findstr /b "COMPASS_PORT=" .env 2^>nul') do set PORT=%%p
)
rem Strip quotes if any
set PORT=!PORT:"=!
set PORT=!PORT:'=!

echo        Clearing stale processes on port !PORT!...
for /f "tokens=5" %%p in ('netstat -ano 2^>nul ^| findstr ":!PORT! "') do (
    if not "%%p"=="" taskkill /F /PID %%p >nul 2>&1
)
timeout /t 1 /nobreak >nul

echo.
echo   Compass running at http://127.0.0.1:!PORT!
echo   Press Ctrl+C to stop.
echo.

rem Open browser after 2s, in background
start "" /b cmd /c "timeout /t 2 /nobreak >nul && start http://127.0.0.1:!PORT!"

"%VENV_PY%" -m compass.run

endlocal
