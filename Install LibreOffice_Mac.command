#!/bin/bash
# LibreOffice 安装脚本 — Compass PDF 导出所需

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"
xattr -d com.apple.quarantine "$0" 2>/dev/null || true
export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:$PATH"

echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║         LibreOffice 安装程序                 ║"
echo "║   Compass PDF 导出所需              ║"
echo "╚══════════════════════════════════════════════╝"
echo ""

# 先检查是否已安装
if [ -x "/Applications/LibreOffice.app/Contents/MacOS/soffice" ] || command -v soffice &>/dev/null; then
    echo "  ✅ LibreOffice 已安装，无需重复安装。"
    read -p "  按任意键退出..."
    exit 0
fi

# 方式一：Homebrew
if command -v brew &>/dev/null; then
    echo "  [ 1/2 ] 检测到 Homebrew，正在安装（约 350MB）..."
    echo "          请耐心等待..."
    echo ""
    brew install --cask libreoffice
    if [ $? -eq 0 ]; then
        echo ""
        echo "  ✅ LibreOffice 安装完成！"
        read -p "  按任意键退出..."
        exit 0
    fi
    echo "  ⚠️  Homebrew 安装失败，尝试直接下载..."
else
    echo "  [ 1/2 ] 未检测到 Homebrew，尝试直接下载..."
fi

# 方式二：直接下载 DMG
echo ""
echo "  [ 2/2 ] 正在下载 LibreOffice 安装包（约 350MB）..."
echo "          下载完成后请手动打开 DMG 并拖入 Applications 文件夹。"
echo ""
DMG="$HOME/Downloads/LibreOffice_latest.dmg"
curl -L --progress-bar \
    "https://www.libreoffice.org/donate/dl/mac-x86_64/latest/dmg/LibreOffice_latest_MacOS_x86-64.dmg" \
    -o "$DMG"
if [ $? -eq 0 ] && [ -f "$DMG" ]; then
    echo ""
    echo "  ✅ 下载完成：$DMG"
    echo "  正在打开安装包..."
    open "$DMG"
else
    echo ""
    echo "  ❌ 下载失败，请手动访问官网下载："
    echo "     https://www.libreoffice.org/download/download-libreoffice/"
    open "https://www.libreoffice.org/download/download-libreoffice/"
fi

echo ""
read -p "  按任意键退出..."
