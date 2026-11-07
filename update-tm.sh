#!/usr/bin/env bash
# Update the official TM image, preserving the Token managed by TMM.
set -euo pipefail
cd /root/tmm

official=未获取
new_id=未获取
image_ref=未获取
current_id=未知
container_state=未知
health=未知
tm_name=tm
command -v python3 >/dev/null || {
  echo "缺少 python3，尚未执行更新；请先安装 python3。"
  exit 1
}
summary() {
  local snapshot
  # Health is optional in Docker inspect; parse JSON without evaluating missing
  # fields in a Go template (which can make the entire inspection fail).
  snapshot=$(docker inspect --format '{{json .}}' "$tm_name" 2>/dev/null | python3 -c '
import json, sys
c = json.load(sys.stdin)
s = c["State"]
h = s.get("Health")
health = h.get("Status", "unknown") if isinstance(h, dict) else "none"
print("|".join(str(v) for v in (c["Id"], c["Image"], s["Status"],
      c["RestartCount"], s["StartedAt"], health)))
' 2>/dev/null) || snapshot='未知|未知|状态读取失败|0|未知|未知'
  IFS='|' read -r container_id current_id container_state restarts started health <<< "$snapshot"
}
report() {
  echo
  echo "更新结果：$1"
  echo "官方镜像：$official"
  echo "目标镜像 ID：$new_id"
  echo "当前镜像 ID：$current_id"
  echo "本地镜像标签：$image_ref"
  echo "容器状态：${container_state}；健康检查：${health}；重启次数：$restarts"
  echo "此结果验证镜像及容器运行状态，TM 账号认证请在官方后台确认。"
}
trap 'summary; report "失败，更新命令未完成"' ERR

wait_timeout=${TM_UPDATE_TIMEOUT:-120}
stable_seconds=${TM_UPDATE_STABLE_SECONDS:-10}
[[ "$wait_timeout" =~ ^[1-9][0-9]*$ && "$stable_seconds" =~ ^[1-9][0-9]*$ ]] || {
  echo "等待时间和观察时间必须是正整数秒。"; exit 1;
}

# Compose service names remain stable even when older containers have generated names.
manager_id=$(docker compose ps -a -q tmm)
if [ -z "$manager_id" ]; then
  echo "未找到当前 Compose 项目的 TMM 服务，尚未执行镜像更新。"
  echo "请在 /root/tmm 执行 docker compose ps -a 检查部署状态。"
  exit 1
fi
if [ "$(docker inspect --format '{{.State.Running}}' "$manager_id")" != true ]; then
  echo "TMM 服务未运行，尚未执行镜像更新。"
  echo "请先执行 docker compose start tmm，再重新运行本脚本。"
  exit 1
fi
image_ref=$(docker exec "$manager_id" printenv TM_IMAGE)
tm_name=$(docker exec "$manager_id" printenv TM_CONTAINER_NAME)
if [ -z "$image_ref" ] || [ -z "$tm_name" ]; then
  echo "TMM 缺少镜像或容器名称配置，尚未执行镜像更新。"
  exit 1
fi
case "$image_ref" in
  sha256:*|*@sha256:*)
    echo "当前配置固定了镜像摘要，请先将 TM_IMAGE 改成可更新的镜像标签。"
    exit 1
    ;;
esac

old_id=$(docker inspect --format '{{.Image}}' "$tm_name")
architecture=$(docker image inspect --format '{{.Architecture}}' "$old_id")
case "$architecture" in
  amd64) official=traffmonetizer/cli_v2:latest ;;
  arm64) official=traffmonetizer/cli_v2:arm64v8 ;;
  arm) official=traffmonetizer/cli_v2:arm32v7 ;;
  *) echo "不支持当前 TM 镜像架构：$architecture"; exit 1 ;;
esac

docker pull "$official"
new_id=$(docker image inspect --format '{{.Id}}' "$official")

changed=false
if [ "$old_id" != "$new_id" ]; then
  changed=true
  docker tag "$old_id" tmm-tm:previous
  docker tag "$new_id" "$image_ref"
  docker compose restart tmm
fi

echo "正在核对实际镜像并观察容器运行状态（最多 $wait_timeout 秒）……"
deadline=$((SECONDS + wait_timeout))
stable_since=-1
last_instance=''
while [ "$SECONDS" -lt "$deadline" ]; do
  summary
  instance="$container_id|$restarts|$started"
  if [ "$current_id" = "$new_id" ] && [ "$container_state" = running ] &&
     { [ "$health" = none ] || [ "$health" = healthy ]; }; then
    if [ "$instance" != "$last_instance" ] || [ "$stable_since" -lt 0 ]; then
      stable_since=$SECONDS
    fi
    if [ "$((SECONDS - stable_since))" -ge "$stable_seconds" ]; then
      if [ "$changed" = true ]; then
        report "成功，已使用本次拉取的最新镜像，连续运行 $stable_seconds 秒"
      else
        report "已是最新镜像，无需更新，连续运行 $stable_seconds 秒"
      fi
      exit 0
    fi
  else
    stable_since=-1
  fi
  last_instance=$instance
  sleep 1
done
summary
report "失败或超时，未确认目标镜像持续正常运行"
echo "排查命令：docker compose logs --tail=50 tmm"
echo "如需恢复旧镜像，请按 README 的镜像回退步骤操作。"
exit 1
