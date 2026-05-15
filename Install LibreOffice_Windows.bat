@echo off
chcp 65001 >nul
echo.
echo ================================================
echo    LibreOffice 安装程序
echo    Compass PDF 导出所需
echo ================================================
echo.

:: 先检查是否已安装
set LO_FOUND=0
if exist "C:\Program Files\LibreOffice\program\soffice.exe" set LO_FOUND=1
if exist "C:\Program Files (x86)\LibreOffice\program\soffice.exe" set LO_FOUND=1
if %LO_FOUND%==0 (
    for /d %%d in ("C:\Program Files\LibreOffice*") do (
        if exist "%%d\program\soffice.exe" set LO_FOUND=1
    )
)
if %LO_FOUND%==1 (
    echo [OK] LibreOffice 已安装，无需重复安装。
    pause & exit /b 0
)

:: 方式一：尝试 winget 自动安装
echo [1/2] 尝试通过 winget 自动安装...
winget --version >nul 2>&1
if not errorlevel 1 (
    winget install TheDocumentFoundation.LibreOffice --accept-package-agreements --accept-source-agreements
    if not errorlevel 1 (
        echo.
        echo [OK] LibreOffice 安装完成！
        pause & exit /b 0
    )
    echo [WARN] winget 安装失败，尝试手动下载...
) else (
    echo [INFO] 未检测到 winget，尝试手动下载...
)

:: 方式二：PowerShell 下载官方安装包
echo [2/2] 正在下载 LibreOffice 安装包（约 350MB）...
echo       请耐心等待，下载完成后将自动运行安装程序。
echo.
set INSTALLER=%TEMP%\LibreOffice_installer.msi
powershell -Command "& {
  $url = 'https://www.libreoffice.org/donate/dl/win-x86_64/latest/msi/LibreOffice_latest_Win_x86-64.msi';
  $out = $env:TEMP + '\LibreOffice_installer.msi';
  Write-Host '  下载中...' ;
  (New-Object Net.WebClient).DownloadFile($url, $out);
  Write-Host '  下载完成，启动安装程序...' ;
  Start-Process msiexec.exe -ArgumentList ("/i", $out, "/passive") -Wait
}"
if errorlevel 1 (
    echo.
    echo [ERROR] 自动下载失败。请手动访问以下地址下载安装：
    echo         https://www.libreoffice.org/download/download-libreoffice/
    echo.
    start https://www.libreoffice.org/download/download-libreoffice/
) else (
    echo.
    echo [OK] LibreOffice 安装完成！
)

pause
