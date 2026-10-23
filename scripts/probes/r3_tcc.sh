#!/bin/bash
# R3：验证「自签证书固定 designated requirement」能否从根上解决"重编译丢屏幕录制权限"。
# 原理：TCC 库按 code requirement(csreq) 认人。ad-hoc 签名的默认 DR 是 cdhash——内容一变就变，权限作废；
#      证书签名给出稳定 DR（identifier + 证书主体），重编译不换身份。
set -e
cd /tmp/aula-probe
W=/tmp/aula-probe/tcc
rm -rf "$W"; mkdir -p "$W"
KC="$W/test.keychain-db"
KCPASS=probe

echo "===== 1. 建自签代码签名证书（独立 keychain，不碰 login） ====="
openssl req -x509 -newkey rsa:2048 -keyout "$W/key.pem" -out "$W/cert.pem" -days 3650 -nodes \
  -subj "/CN=aula-probe-sign" \
  -addext "keyUsage=critical,digitalSignature" \
  -addext "extendedKeyUsage=critical,codeSigning" \
  -addext "basicConstraints=critical,CA:false" 2>/dev/null
# 本机是 LibreSSL 3.3.6：pkcs12 默认就是 legacy RC2/3DES 算法，多传 -legacy/-keypbe 会报参数错误
openssl pkcs12 -export -inkey "$W/key.pem" -in "$W/cert.pem" -out "$W/cert.p12" \
  -passout pass:p12pass 2>/dev/null
security create-keychain -p "$KCPASS" "$KC"
security unlock-keychain -p "$KCPASS" "$KC"
security import "$W/cert.p12" -k "$KC" -P p12pass -T /usr/bin/codesign -T /usr/bin/security >/dev/null
security set-key-partition-list -S apple-tool:,apple:,codesign: -s -k "$KCPASS" "$KC" >/dev/null 2>&1
echo "证书已导入独立 keychain（未改用户的 keychain 搜索列表）"

echo
echo "===== 2. 造一个最小 app bundle ====="
APP="$W/Probe.app"
mkdir -p "$APP/Contents/MacOS"
cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleIdentifier</key><string>com.aulaprobe.tcc</string>
  <key>CFBundleName</key><string>Probe</string>
  <key>CFBundleExecutable</key><string>probe</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
</dict></plist>
PLIST

build_with() { # $1 = sign identity
  printf '#!/bin/sh\necho probe %s\n' "$(date +%s%N)" > "$APP/Contents/MacOS/probe"
  chmod +x "$APP/Contents/MacOS/probe"
  if [ "$1" = "adhoc" ]; then
    codesign --force -s - "$APP" >/dev/null 2>&1
  else
    codesign --force -s "aula-probe-sign" --keychain "$KC" "$APP" >/dev/null 2>&1
  fi
}

dr_of() { codesign -d -r- "$APP" 2>&1 | sed -n 's/^designated => //p'; }

echo
echo "===== 3. ad-hoc 签名：重编译后 DR 是否变 ====="
build_with adhoc; DR1=$(dr_of)
echo "build#1 CDHash=$(codesign -d --verbose=4 "$APP" 2>&1 | sed -n 's/^CDHash=//p')"
echo "build#1 DR    : $DR1"
build_with adhoc; DR2=$(dr_of)
echo "build#2 CDHash=$(codesign -d --verbose=4 "$APP" 2>&1 | sed -n 's/^CDHash=//p')"
echo "build#2 DR    : $DR2"
if [ -z "$DR1" ] && [ -z "$DR2" ]; then
  echo ">>> ad-hoc 没有显式 DR：codesign 隐式按 CDHash 认身份，CDHash 每次都变 → 重编译必丢 TCC 权限（复现用户痛点）"
elif [ "$DR1" = "$DR2" ]; then
  echo ">>> ad-hoc DR 稳定（意外）"
else
  echo ">>> ad-hoc DR 变了 → 每次重编译 TCC 权限作废（复现痛点）"
fi

echo
echo "===== 4. 自签证书签名：重编译后 DR 是否变 ====="
build_with cert; SDR1=$(dr_of)
echo "build#1 CDHash=$(codesign -d --verbose=4 "$APP" 2>&1 | sed -n 's/^CDHash=//p')"
echo "build#1 DR    : $SDR1"
build_with cert; SDR2=$(dr_of)
echo "build#2 CDHash=$(codesign -d --verbose=4 "$APP" 2>&1 | sed -n 's/^CDHash=//p')"
echo "build#2 DR    : $SDR2"
if [ "$SDR1" = "$SDR2" ]; then echo ">>> 自签 DR 稳定 → 重编译保权限 ✅"; else echo ">>> 自签 DR 仍变 → 需显式 -r 固定 requirements ❌"; fi

echo
echo "===== 5. 验证签名可用性（codesign --verify） ====="
codesign --verify --strict "$APP" 2>&1 && echo "verify: OK（严格校验通过）"

echo
echo "===== 6. 清理独立 keychain ====="
security delete-keychain "$KC" 2>/dev/null || true
echo "== R3 DONE =="
