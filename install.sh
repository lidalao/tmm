#!/usr/bin/env bash
# Standalone fresh-install entry point; never removes an existing deployment.
set -euo pipefail
umask 077
trap 'echo "安装未完成，请处理上方错误；未执行旧服务移除。" >&2' ERR

[ "$(id -u)" = 0 ] || { echo "请在 VPS 的 root 终端执行。"; exit 1; }
[ "$(uname -s)" = Linux ] || { echo "仅支持 Linux VPS。"; exit 1; }
command -v docker >/dev/null || { echo "请先安装并启动 Docker，再执行本脚本。"; exit 1; }
docker info >/dev/null
[ ! -e /root/tmm ] || { echo "/root/tmm 已存在，全新安装不会覆盖它；已有部署请使用 update-tm.sh。"; exit 1; }

if ! command -v curl >/dev/null || ! command -v python3 >/dev/null; then
  if command -v apt-get >/dev/null; then
    apt-get update
    apt-get install -y curl python3 ca-certificates
  elif command -v dnf >/dev/null; then
    dnf install -y curl python3 ca-certificates
  elif command -v yum >/dev/null; then
    yum install -y curl python3 ca-certificates
  else
    echo "请先安装 curl、Python 3 和 CA 证书。"; exit 1
  fi
fi

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
curl -fL --retry 3 -H 'Cache-Control: no-cache' \
  "https://raw.githubusercontent.com/lidalao/tmm/main/tmm-migration.zip?t=$(date +%s)" \
  -o "$work/bundle.zip"
python3 -m zipfile -e "$work/bundle.zip" "$work"
# Check for existing services before creating the installation directory.
(cd "$work/tmm" && python3 prepare-migration.py check-fresh)
mv "$work/tmm" /root/tmm
bash /root/tmm/remove-old.sh --fresh-install
