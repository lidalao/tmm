#!/usr/bin/env bash
# Update the official TM image, preserving the Token managed by TMM.
set -euo pipefail
cd /root/tmm

image_ref=$(docker exec tmm printenv TM_IMAGE)
case "$image_ref" in
  sha256:*|*@sha256:*)
    echo "当前配置固定了镜像摘要，请先将 TM_IMAGE 改成可更新的镜像标签。"
    exit 1
    ;;
esac

old_id=$(docker inspect --format '{{.Image}}' tm)
architecture=$(docker image inspect --format '{{.Architecture}}' "$old_id")
case "$architecture" in
  amd64) official=traffmonetizer/cli_v2:latest ;;
  arm64) official=traffmonetizer/cli_v2:arm64v8 ;;
  arm) official=traffmonetizer/cli_v2:arm32v7 ;;
  *) echo "不支持当前 TM 镜像架构：$architecture"; exit 1 ;;
esac

docker pull "$official"
new_id=$(docker image inspect --format '{{.Id}}' "$official")

if [ "$old_id" = "$new_id" ]; then
  echo "当前 TM 已是该官方标签的最新镜像，无需重建。"
  exit 0
fi

docker tag "$old_id" tmm-tm:previous
docker tag "$new_id" "$image_ref"
docker compose restart tmm
echo "已触发镜像更新。请等待启动检查完成，再查看下方日志与容器状态。"
docker compose logs --tail=30 tmm
docker compose --profile managed ps -a
