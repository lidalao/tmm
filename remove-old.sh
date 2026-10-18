#!/usr/bin/env bash
# Run once as root from the extracted bundle, then: docker compose up -d
set -euo pipefail
umask 077
trap 'echo "执行失败，脚本已停止。请先处理上方错误，再继续。" >&2' ERR

if [ "$(id -u)" != 0 ]; then
  echo "请在 VPS 的 root 终端执行：bash remove-old.sh" >&2
  exit 1
fi
[ "$(uname -s)" = Linux ] || { echo "本脚本只用于 Linux VPS。"; exit 1; }
bundle_dir="$(cd "$(dirname "$0")" && pwd -P)"
cd "$bundle_dir"
for old_dir in /opt/tm-manage /etc/tm-manage /var/lib/tm-manage /etc/systemd/system/tm-agent.service.d; do
  case "$bundle_dir/" in "$old_dir/"*)
    echo "请先把新目录移到 /root/tmm 或 /opt/tmm，不能放在旧 Agent 目录内。"
    exit 1 ;;
  esac
done

echo "正在准备新方案。检查和构建成功后，才会永久删除旧 Agent 与旧 TM 容器。"
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
python3 -c 'import sys; assert sys.version_info >= (3, 8), "需要 Python 3.8 或更新版本"'
command -v systemctl >/dev/null
docker info >/dev/null

case "$(uname -m)" in
  x86_64) compose_arch=x86_64; buildx_arch=amd64 ;;
  aarch64|arm64) compose_arch=aarch64; buildx_arch=arm64 ;;
  *) echo "仅支持 AMD64 和 ARM64 VPS。"; exit 1 ;;
esac

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
plugin_dir="${DOCKER_CONFIG:-/root/.docker}/cli-plugins"
install_plugin() {
  local repository="$1" version="$2" asset="$3" target="$4"
  local base="https://github.com/docker/$repository/releases/download/$version"
  curl --fail --silent --show-error --location "$base/$asset" -o "$work/$asset"
  curl --fail --silent --show-error --location "$base/checksums.txt" -o "$work/checksums.txt"
  python3 - "$work" "$asset" <<'PY'
import hashlib
from pathlib import Path
import sys
directory, name = Path(sys.argv[1]), sys.argv[2]
matches = [line.split()[0] for line in (directory / 'checksums.txt').read_text().splitlines()
           if len(line.split()) == 2 and line.split()[1].lstrip('*') == name]
if len(matches) != 1 or hashlib.sha256((directory / name).read_bytes()).hexdigest() != matches[0]:
    raise SystemExit('Docker 插件 SHA256 校验失败，未安装。')
PY
  mkdir -p "$plugin_dir"
  install -m 755 "$work/$asset" "$plugin_dir/$target"
}

version_at_least() {
  python3 - "$1" "$2" <<'PY'
import re
import sys
match = re.search(r'(\d+)\.(\d+)\.(\d+)', sys.argv[1])
raise SystemExit(0 if match and tuple(map(int, match.groups())) >= tuple(map(int, sys.argv[2].split('.'))) else 1)
PY
}
if ! version_at_least "$(docker compose version --short 2>/dev/null || true)" 2.20.0; then
  echo "安装或更新 Docker Compose 插件……"
  install_plugin compose v2.39.2 "docker-compose-linux-$compose_arch" docker-compose
fi
if ! version_at_least "$(docker buildx version 2>/dev/null || true)" 0.17.0; then
  echo "安装或更新 Docker Buildx 插件……"
  install_plugin buildx v0.26.1 "buildx-v0.26.1.linux-$buildx_arch" docker-buildx
fi
docker compose version
docker buildx version

# Generate .env only once. Existing user configuration is never overwritten.
python3 prepare-migration.py configure
docker compose --profile managed config --format json > "$work/compose.json"
python3 prepare-migration.py check "$work/compose.json"

echo "正在构建新 TMM 镜像，旧服务此时继续运行……"
docker compose build tmm
python3 - "$work/compose.json" <<'PY'
import json
import subprocess
import sys
image = json.load(open(sys.argv[1]))['services']['tm']['image']
if subprocess.run(['docker', 'image', 'inspect', image], capture_output=True).returncode:
    subprocess.run(['docker', 'pull', image], check=True)
PY

echo "检查和镜像准备已完成。现在移除旧 Agent 和旧 TM。"
if systemctl cat tm-agent.service >/dev/null 2>&1 || systemctl is-active --quiet tm-agent.service; then
  systemctl stop tm-agent.service
  systemctl disable tm-agent.service
fi
if systemctl is-active --quiet tm-agent.service; then
  echo "旧 Agent 尚未停止，未继续删除。"; exit 1
fi

# Re-enumerate after stopping the Agent so no rollback container can be missed.
python3 prepare-migration.py remove-containers

rm -f -- /etc/systemd/system/tm-agent.service
rm -f -- /etc/systemd/system/multi-user.target.wants/tm-agent.service
rm -rf -- /etc/systemd/system/tm-agent.service.d
rm -rf -- /opt/tm-manage /etc/tm-manage /var/lib/tm-manage
systemctl daemon-reload
systemctl reset-failed tm-agent.service >/dev/null 2>&1 || true

echo "旧 Agent、身份、状态及旧 TM 容器已删除。"
echo "新方案已准备完成。请在当前目录执行：docker compose up -d"
