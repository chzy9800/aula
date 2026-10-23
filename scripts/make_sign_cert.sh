#!/bin/bash
# 生成 aula 自签代码签名证书（只需跑一次）。
#
# 为什么需要它：ad-hoc 签名没有显式 designated requirement，TCC 按 CDHash 认人，
# 每次重编译 CDHash 都变 → 屏幕录制权限作废。自签证书给出稳定的
# `identifier + certificate leaf` DR，重编译不换身份，权限只批一次。
# （实测依据见 docs/b-route-validation.md V4）
#
# 用法：bash scripts/make_sign_cert.sh
# 之后：build_app.sh 会自动找到身份名以 "aula" 开头的证书并优先使用。

set -euo pipefail

CN="aula-dev"
KEYCHAIN="$HOME/Library/Keychains/login.keychain-db"
W="$(mktemp -d)"
trap 'rm -rf "$W"' EXIT

echo "① 检查是否已存在…"
if security find-identity -v -p codesigning 2>/dev/null | grep -q "\"$CN\""; then
  echo "   已存在「${CN}」，无需重复生成。"
  security find-identity -v -p codesigning | grep "$CN"
  exit 0
fi

echo "② 生成自签证书（codeSigning EKU，10 年）…"
# 本机 openssl 是 LibreSSL：pkcs12 默认就是 legacy 算法，不要套 OpenSSL 3 的 -legacy/-keypbe
openssl req -x509 -newkey rsa:2048 -keyout "$W/key.pem" -out "$W/cert.pem" -days 3650 -nodes \
  -subj "/CN=$CN/O=aula/C=CN" \
  -addext "keyUsage=critical,digitalSignature" \
  -addext "extendedKeyUsage=critical,codeSigning" \
  -addext "basicConstraints=critical,CA:false" 2>/dev/null
openssl pkcs12 -export -inkey "$W/key.pem" -in "$W/cert.pem" -out "$W/cert.p12" \
  -passout pass:aulapass 2>/dev/null

echo "③ 导入 login keychain…"
security import "$W/cert.p12" -k "$KEYCHAIN" -P aulapass -T /usr/bin/codesign -T /usr/bin/security >/dev/null
if security set-key-partition-list -S apple-tool:,apple:,codesign: -s -k "" "$KEYCHAIN" >/dev/null 2>&1; then
  echo "   已设置 codesign 免提示"
else
  echo "   ⚠️ 设置免提示失败（需要 login 密码）；首次签名会弹一次「允许访问」，点「始终允许」即可"
fi

echo "④ 设为可信（自签证书不设信任就不算有效签名身份）…"
# 这一步会调 SecurityAgent 弹授权框，必须①有图形会话（Terminal.app 里跑）②允许改信任设置。
# 在无 GUI 的自动化环境里它会被系统直接杀掉（SIGTERM），不是参数错误。
cp "$W/cert.pem" "$HOME/Library/Keychains/aula-dev.pem" 2>/dev/null || true
if security add-trusted-cert -r trustRoot -p codeSign -k "$KEYCHAIN" "$W/cert.pem" 2>/dev/null; then
  echo "   信任设置完成（仅 codeSign 策略）"
else
  echo "   ⚠️ 自动设置被拒（多半是没有图形授权会话）。手动做一次，二选一："
  echo "      A. 打开「钥匙串访问」→ 登录 → 证书 → aula-dev → 双击 → 信任 →"
  echo "         「代码签名」选「始终信任」"
  echo "      B. 在 Terminal.app 里执行："
  echo "         security add-trusted-cert -r trustRoot -p codeSign -k ~/Library/Keychains/login.keychain-db ~/Library/Keychains/aula-dev.pem"
  echo "   做完重跑本脚本即可确认。"
fi

echo "⑤ 结果："
security find-identity -v -p codesigning | grep "$CN" || {
  echo "   ❌ 还没变成有效签名身份——按上面 ④ 的办法设一次信任"
  exit 1
}

echo
echo "✔ 完成。重新打包：bash scripts/build_app.sh --probe"
echo "  之后重编译只需批一次屏幕录制权限（系统设置 → 隐私与安全性 → 屏幕录制）。"
