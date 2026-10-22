#!/bin/bash
# 打包 aula 菜单栏应用：编译 → 搭 .app bundle → ad-hoc 签名 → 装到 ~/Applications
#
# 用法：bash scripts/build_menubar.sh
# 产物：~/Applications/aula.app（双击启动，菜单栏出现波形图标）
#
# 注意：重新编译后 ad-hoc 签名会变化，macOS 可能要求重批「屏幕录制」权限
# （系统设置 → 隐私与安全性 → 屏幕录制，把 aula 关掉再打开一次）。

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SOURCE="$ROOT/aula/menubar/main.swift"
BUILD_DIR="$(mktemp -d)"
APP_DIR="$HOME/Applications/aula.app"
BUNDLE_ID="com.chzy.aula.menubar"

echo "① 编译…"
swiftc -O -swift-version 5 -parse-as-library \
  -o "$BUILD_DIR/aula-menubar" "$SOURCE"

echo "② 组装 bundle…"
rm -rf "$APP_DIR"
mkdir -p "$APP_DIR/Contents/MacOS"
cp "$BUILD_DIR/aula-menubar" "$APP_DIR/Contents/MacOS/aula-menubar"

cat > "$APP_DIR/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key>
    <string>aula-menubar</string>
    <key>CFBundleIdentifier</key>
    <string>$BUNDLE_ID</string>
    <key>CFBundleName</key>
    <string>aula</string>
    <key>CFBundleShortVersionString</key>
    <string>0.7.0</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>LSUIElement</key>
    <true/>
    <key>NSHighResolutionCapable</key>
    <true/>
    <key>NSHumanReadableCopyright</key>
    <string>MIT License</string>
</dict>
</plist>
PLIST

echo "③ ad-hoc 签名…"
codesign --force --deep -s - "$APP_DIR"

echo "✔ 已安装到 $APP_DIR"
echo "  双击启动（或 open ~/Applications/aula.app），菜单栏会出现波形图标。"
echo "  首次「开始听课」需要批一次「屏幕录制」权限（系统设置 → 隐私与安全性）。"
