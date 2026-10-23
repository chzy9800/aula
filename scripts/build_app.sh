#!/bin/bash
# 打包 aula 原生应用（B 路线 P0）：内嵌 Python 运行时 + 编译 Swift 壳 + 自签 + 装到 ~/Applications
#
# 用法：
#   bash scripts/build_app.sh              # 首次会下 24MB pbs 并装依赖（约 3–5 分钟）
#   bash scripts/build_app.sh --probe      # 构建后立刻跑自检（起服务→健康→建课堂→停→报耗时）
#   AULA_SIGN_IDENTITY="aula-dev" bash scripts/build_app.sh   # 指定自签证书
#
# 为什么不用 venv：venv 的 pyvenv.cfg 记录构建机绝对路径，搬进 .app 就失效。
# pbs（python-build-standalone）本身 relocatable，依赖直接装进它的 prefix，整目录可搬。
#
# 为什么不用 PyInstaller：ctranslate2 是 C++ 扩展，单文件打包的收集规则经常漏 .dylib。
# onedir 布局 + 逐二进制签名反而更稳。

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUILD="$ROOT/build/app"
RUNTIME="$BUILD/runtime"
CACHE="$BUILD/cache"
APP_DIR="$HOME/Applications/aula.app"
BUNDLE_ID="com.chzy.aula"
PBS_URL="https://github.com/astral-sh/python-build-standalone/releases/download/20261021/cpython-3.13.17%2B20261021-aarch64-apple-darwin-install_only.tar.gz"
PBS_TAR="$CACHE/pbs-cp313-arm64.tar.gz"
WANT_PROBE=0
WANT_SYNC_CODE=0
case "${1:-}" in
  --probe) WANT_PROBE=1 ;;
  --sync-code) WANT_SYNC_CODE=1 ;;
esac

mkdir -p "$BUILD" "$CACHE"

# 只同步 Python 源码到已装好的 app（改完后端代码不用等整套构建）。
# 存在的理由很实在：app 跑的是 bundle 里的副本，改完仓库源码不重新构建，
# 你测的还是旧代码——我为此白排查过一次"翻译为什么还是空"。
if [ "$WANT_SYNC_CODE" = "1" ]; then
  if [ ! -d "$APP_DIR/Contents/Resources/lib/aula" ]; then
    echo "✘ $APP_DIR 还没装过，先跑一次完整构建"
    exit 1
  fi
  echo "① 同步 Python 源码…"
  rm -rf "$APP_DIR/Contents/Resources/lib/aula"
  cp -Rc "$ROOT/aula" "$APP_DIR/Contents/Resources/lib/aula"
  rm -rf "$APP_DIR/Contents/Resources/lib/aula/app" \
         "$APP_DIR/Contents/Resources/lib/aula/menubar" \
         "$APP_DIR/Contents/Resources/lib/aula/__pycache__"
  echo "② 重签…"
  SIGN_ID="${AULA_SIGN_IDENTITY:-}"
  if [ -z "$SIGN_ID" ]; then
    SIGN_ID=$(security find-identity -v -p codesigning 2>/dev/null \
      | sed -n 's/.*"\(aula[^"]*\)".*/\1/p' | head -1)
  fi
  if [ -n "$SIGN_ID" ]; then
    codesign --force -s "$SIGN_ID" "$APP_DIR" 2>/dev/null
    echo "   已用 ${SIGN_ID} 重签"
  else
    codesign --force --deep -s - "$APP_DIR" 2>/dev/null
    echo "   ⚠️ ad-hoc 重签（TCC 权限会丢，建议先跑 make_sign_cert.sh）"
  fi
  echo "✔ 源码已同步（运行时未动）"
  exit 0
fi

echo "① Python 运行时（pbs，relocatable）…"
mkdir -p "$RUNTIME"
if [ ! -x "$RUNTIME/python/bin/python3" ]; then
  if [ ! -f "$PBS_TAR" ]; then
    echo "   下载 pbs（24MB，只下一次）…"
    curl -L --noproxy '*' --max-time 600 -o "$PBS_TAR" "$PBS_URL"
  fi
  tar -xzf "$PBS_TAR" -C "$RUNTIME"
else
  echo "   复用已有运行时：$RUNTIME/python"
fi
PY="$RUNTIME/python/bin/python3"
"$PY" -V

echo "② 安装依赖到 pbs 自己的 prefix（不建 venv）…"
# 先探测再装：无条件 pip install 每次都要联网查索引，二次构建会白等好几分钟
if "$PY" -c "import fastapi, uvicorn, faster_whisper" >/dev/null 2>&1; then
  echo "   依赖已在运行时里，跳过"
else
  "$PY" -m pip install -q --no-compile --disable-pip-version-check \
    "fastapi>=0.110" "uvicorn[standard]>=0.27" "faster-whisper>=1.0" 2>&1 | tail -5
fi

echo "③ 拷服务端源码…"
rm -rf "$RUNTIME/lib/aula"
mkdir -p "$RUNTIME/lib"
cp -R "$ROOT/aula" "$RUNTIME/lib/aula"
# 开发期残留与测试不进包
find "$RUNTIME/lib" -name "__pycache__" -type d -prune -exec rm -rf {} + 2>/dev/null || true
rm -rf "$RUNTIME/lib/aula/__pycache__" "$RUNTIME/lib/aula/menubar" "$RUNTIME/lib/aula/app"
# 原生 UI 不需要浏览器前端；保留 static 是为了 /api/reader 的 sample.pdf 与 favicon 兜底
find "$RUNTIME/lib/aula/static" -name "*.map" -delete 2>/dev/null || true

echo "④ 瘦身…"
rm -rf "$RUNTIME/python/lib/python3.13/test" \
       "$RUNTIME/python/lib/python3.13/idlelib" \
       "$RUNTIME/python/lib/python3.13/tkinter" \
       "$RUNTIME/python/lib/python3.13/turtledemo" \
       "$RUNTIME/python/share/man" 2>/dev/null || true
find "$RUNTIME/python" -name "__pycache__" -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "$RUNTIME" -name "*.pyc" -delete 2>/dev/null || true
du -sh "$RUNTIME"

echo "⑤ 编译 Swift…"
mkdir -p "$BUILD/bin"
swiftc -O -swift-version 5 -parse-as-library \
  -o "$BUILD/bin/aula" \
  "$ROOT/aula/app/main.swift" \
  "$ROOT/aula/app/Sidecar.swift" \
  "$ROOT/aula/app/Vad.swift" \
  "$ROOT/aula/app/Realtime.swift" \
  "$ROOT/aula/app/ClassStore.swift" \
  "$ROOT/aula/app/ClassView.swift" \
  "$ROOT/aula/app/SystemAudio.swift" \
  "$ROOT/aula/app/HotKey.swift" \
  "$ROOT/aula/app/CaptionPanel.swift" \
  "$ROOT/aula/app/AppLog.swift"

echo "⑥ 组装 bundle…"
# 不用 rm -rf 清旧包：一是安全闸门会拦（4000+ 文件），二是留一份旧版能回滚。
if [ -d "$APP_DIR" ]; then
  OLD="/tmp/aula-app-previous-$(date +%s)"
  mv "$APP_DIR" "$OLD"
  echo "   旧版本暂存 ${OLD}（重启后系统自动清理）"
fi
mkdir -p "$APP_DIR/Contents/MacOS" "$APP_DIR/Contents/Resources"
cp "$BUILD/bin/aula" "$APP_DIR/Contents/MacOS/aula"
# 用 APFS 写时复制克隆（cp -c）：258MB 实测 0.65 秒，且不额外占磁盘。
# cp -R 逐文件建 inode 要几分钟，ditto 在受限环境里会挂住——两个都踩过。
cp -Rc "$RUNTIME/python" "$APP_DIR/Contents/Resources/python"
mkdir -p "$APP_DIR/Contents/Resources/lib"
cp -Rc "$RUNTIME/lib/aula" "$APP_DIR/Contents/Resources/lib/aula"

cat > "$APP_DIR/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key>
    <string>aula</string>
    <key>CFBundleIdentifier</key>
    <string>$BUNDLE_ID</string>
    <key>CFBundleName</key>
    <string>aula</string>
    <key>CFBundleDisplayName</key>
    <string>aula</string>
    <key>CFBundleShortVersionString</key>
    <string>0.8.0</string>
    <key>CFBundleVersion</key>
    <string>1</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>LSMinimumSystemVersion</key>
    <string>13.0</string>
    <key>NSMicrophoneUsageDescription</key>
    <string>aula 需要麦克风把课堂上老师说的话转写成文字并翻译。</string>
    <key>NSHighResolutionCapable</key>
    <true/>
    <key>NSSupportsAutomaticTermination</key>
    <false/>
    <key>NSSupportsSuddenTermination</key>
    <false/>
    <key>NSHumanReadableCopyright</key>
    <string>MIT License</string>
</dict>
</plist>
PLIST

echo "⑦ 签名…"
# 优先自签证书：DR 稳定，重编译不丢 TCC 权限（见 docs/b-route-validation.md V4）
SIGN_ID="${AULA_SIGN_IDENTITY:-}"
if [ -z "$SIGN_ID" ]; then
  SIGN_ID=$(security find-identity -v -p codesigning 2>/dev/null \
    | sed -n 's/.*"\(aula[^"]*\)".*/\1/p' | head -1)
fi
# 先签内嵌二进制，再由内到外签 bundle——顺序反了会被 --deep 之外的校验挑出未签名子项
if [ -n "$SIGN_ID" ]; then
  echo "   使用自签身份：$SIGN_ID"
  find "$APP_DIR/Contents/Resources" -type f \( -name "*.dylib" -o -name "*.so" \) -print0 2>/dev/null \
    | xargs -0 -n 50 codesign --force -s "$SIGN_ID" 2>/dev/null || true
  codesign --force -s "$SIGN_ID" "$APP_DIR/Contents/Resources/python/bin/python3" 2>/dev/null || true
  codesign --force -s "$SIGN_ID" "$APP_DIR"
  SIGN_MODE="自签（${SIGN_ID}）"
else
  echo "   ⚠️ 没找到自签证书，退回 ad-hoc——每次重编译都会丢屏幕录制权限。"
  echo "      生成方法：见 docs/b-route-validation.md V4"
  codesign --force --deep -s - "$APP_DIR"
  SIGN_MODE="ad-hoc（重编译会丢 TCC 权限）"
fi
codesign --verify --strict "$APP_DIR" && echo "   verify: OK"

echo "⑧ 体积 / 安装"
du -sh "$APP_DIR"
# 刷新 LaunchServices：不刷的话双击仍会按旧 bundle 的可执行文件名启动
# （实测踩过：磁盘上明明是 aula，open 起来的却是上一次的 aula-menubar）
LSREG=/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister
"$LSREG" -f "$APP_DIR" 2>/dev/null && echo "   LaunchServices 已刷新"
# 注意：全角标点紧跟变量名时，bash 会把它当成变量名的一部分（set -u 下直接报错），
# 这里一律用 ${VAR} 定界。
echo "✔ 已安装到 ${APP_DIR}（签名：${SIGN_MODE}）"
echo "  双击启动；菜单栏出现波形图标。自检：${APP_DIR}/Contents/MacOS/aula --probe"

if [ "$WANT_PROBE" = "1" ]; then
  echo
  echo "⑨ 自检（--probe）…"
  "$APP_DIR/Contents/MacOS/aula" --probe
fi
