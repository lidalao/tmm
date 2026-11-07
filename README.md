# TM + TMM：一个 Compose，两个容器

`tm` 运行官方 TraffMonetizer 镜像；`tmm` 定时读取 GitHub Raw 上的明文 Token，变化时重建 TM，确认容器启动后保存新 Token。默认每天检查一次，无需 Worker、D1、WebSocket 或节点注册。

默认容器名称为 `tm` 和 `tmm`，在 `docker ps` 中直接显示这两个名称。

## 从 GitHub 下载并迁移旧 VPS（完整命令）

以下整段直接复制到**尚未迁移的 Linux VPS 的 root 终端**，不要在本地电脑执行。地址已填写，无需修改：

```bash
bash <<'TMM_MIGRATE'
set -euo pipefail

if [ "$(id -u)" != 0 ]; then
  echo "请先进入 root 终端，再执行本段命令。"
  exit 1
fi

cd /root
curl --fail --location --retry 3 \
  --header 'Cache-Control: no-cache' \
  https://raw.githubusercontent.com/lidalao/tmm/main/tmm-migration.zip \
  --output tmm-migration.zip

python3 -m zipfile -e tmm-migration.zip /root
cd /root/tmm

bash remove-old.sh
docker compose up -d

docker compose logs --tail=30 tmm
docker compose --profile managed ps -a
TMM_MIGRATE
```

如果已经下载并解压了部署包，首次切换的核心操作就是：

```bash
bash remove-old.sh
docker compose up -d
```

不需要手填初始 Token、节点名称或镜像架构。移除脚本会先准备依赖、生成 `.env`、验证远端 Token、构建新 TMM 镜像并准备 TM 镜像。这些检查失败时，旧 Agent 和旧 TM 保持原状；准备成功后才开始删除。

脚本会永久删除以下旧组件，不保留旧容器备份：

- 停止并禁用 `tm-agent.service`，删除其服务文件、drop-in 及开机启动链接。
- 删除 `/opt/tm-manage`、`/etc/tm-manage`、`/var/lib/tm-manage`，包括旧 Agent 私钥、身份和状态。
- 删除已识别为 TM 的 `tm`、`tm-before-tmm` 及 `tm-rollback-数字` 容器。

其他 Docker 容器、Docker 服务及镜像缓存不清理。当前 TM 的镜像缓存可以供新 TM 复用，设备名称和运行用户会写入新 `.env`；旧容器本身会删除。旧 Cloudflare Worker/D1 不在本脚本处理范围内。

新目录不能放在旧 Agent 的上述目录内，建议解压到 `/root/tmm`。如果已经存在新方案的 Compose TM/TMM 容器，脚本会拒绝执行，避免误删已经切换的新服务。移除脚本只需执行一次，以后不要拿它当重启或升级命令。

## 首次获取 Token

固定地址已经预填：

```text
https://raw.githubusercontent.com/lidalao/tmm/main/token.txt
```

执行 `docker compose up -d` 后，TMM 先启动，取得有效 Token 后再启动同一 Compose 项目里的 TM。首次获取失败会重试，不用空 Token 创建 TM。有已保存的本地 Token 时，TMM 启动会先恢复已保存的配置。

`tm` 使用 `managed` profile，TMM 会显式启动它；不要自己执行 `docker compose up tm` 或 `docker compose --profile managed up`，因为这些操作没有 TMM 注入的 Token。

## 配置

移除脚本在当前目录生成 `.env`。已有 `.env` 时保留它。没有旧 TM 时自动根据 AMD64/ARM64 选择官方镜像，并用 VPS 主机名作为设备名称。

| 配置 | 默认值 | 含义 |
|---|---|---|
| `TM_TOKEN_URL` | 本仓库 `main/token.txt` 的 Raw 地址 | 一行明文 Token |
| `TM_CHECK_INTERVAL` | `86400` | 正常检查间隔，秒，最小 10 |
| `TM_RETRY_INTERVAL` | `300` | 获取或更新失败后的重试间隔，秒，最小 10 |
| `TM_START_GRACE` | `10` | 创建后的额外观察时间，秒，0～300 |
| `TM_IMAGE` | 自动保留当前镜像；新 VPS 按架构选择 | TM 镜像 |
| `TM_USER` | 保留旧 TM 用户；没有旧 TM 时为 `0` | TM 的 UID 或 UID:GID |
| `TM_DEVICE_NAME` | 保留旧设备名；没有显式设备名时为主机名 | 设备名称 |
| `COMPOSE_PROJECT_NAME` | `tm-stack` | Compose 项目名 |
| `TM_CONTAINER_NAME` | `tm` | TM 容器名称 |

移除脚本支持 apt/dnf/yum 按需安装 curl、Python 3 和 CA 证书。缺少 Compose、Buildx 时下载固定版本的官方插件并校验 SHA256；Docker Engine 需已安装且正在运行。脚本不重装 Docker。已有旧 Agent 的 VPS 通常已具备 Docker 和 Python。

需要自定义挂载、网络、环境变量时请在启动前编辑 `compose.yaml` 的 `tm` 服务；脚本不会把这些旧容器配置自动搬到新容器。

## 日常使用

```bash
# 查看 TMM 的中文日志，不打印 Token
docker compose logs --tail=50 tmm

# 查看两个服务
docker compose --profile managed ps -a

# 立即检查 GitHub Token
docker compose restart tmm

# 修改 .env 后应用配置；单纯 restart 不会重读 .env
docker compose up -d

# 更新代码后重建 TMM
docker compose up -d --build

# 停止并移除两个服务，保留已保存的 Token
docker compose --profile managed down
```

以后直接修改 GitHub 仓库根目录 `token.txt` 并提交到 `main`，所有 VPS 在下次检查时自动跟随。修改 `TM_CHECK_INTERVAL=3600` 可改为每小时检查一次。不要再使用旧 `./tmctl update`。

数据保存在项目的 `tmm-data` 命名卷中，`/data/applied.json` 权限为 `600`；TMM 容器更新或 VPS 重启后保留。停止整个项目要带 `--profile managed`，否则默认只移除 TMM。`down -v` 会删除本地 Token 状态。

## 官方 TM 镜像更新

### 当前行为

当前 TMM **只自动同步 Token，不自动跟随官方镜像更新**。迁移时通常将正在使用的官方镜像保存为 `tmm-tm:local`，以保留每台 VPS 的架构和已安装版本；因此直接 `docker compose pull tm` 会尝试拉取这个本地标签，不能完成官方升级。TMM 平时使用 `--pull missing`，Token 变化也不会主动下载新版。

### 简单方案：手动拉取官方镜像，由 TMM 重建

在**已完成新 Compose 部署的 VPS 的 root 终端**执行下面整段。它会按当前 TM 镜像的架构选择官方标签，保留旧镜像为 `tmm-tm:previous`，更新 TMM 实际使用的镜像标签，然后重启 TMM。TMM 会从自己的数据卷读取已保存的 Token，并通过 Compose 重建 TM；不需要再次输入 Token，也不需要执行移除脚本。

脚本已保存在仓库根目录 `update-tm.sh`。每台 VPS 复制执行：

```bash
cd /root/tmm && curl -fL -H 'Cache-Control: no-cache' https://raw.githubusercontent.com/lidalao/tmm/main/update-tm.sh --output update-tm.sh && bash update-tm.sh
```

脚本通过 Compose 服务查找 TMM，兼容旧版的 `tm-stack-tmm-1` 和新版的 `tmm` 容器名称，无需重装或改名。若服务不存在或未运行，会提示检查或启动服务。

以后再次更新时，重新执行这段命令即可。下载失败不会执行脚本。该脚本更新官方 TM 镜像，不重新安装 Agent，也不升级 TMM 程序本身。

脚本执行后默认等待最多 120 秒，核对 TM 容器实际使用的镜像 ID 是否等于本次从官方拉取的镜像 ID，并确认同一个容器连续运行 10 秒、期间没有重启。有 Docker 健康检查时，还必须达到 healthy。成功后显示“更新成功”或“已是最新镜像”，并列出官方镜像、本地标签、目标和当前镜像 ID、容器状态及重启次数。失败或超时返回非零退出码，不会把“已经触发更新”当成成功。

可用 `TM_UPDATE_TIMEOUT=180 TM_UPDATE_STABLE_SECONDS=15 bash update-tm.sh` 调整等待时间和连续观察时间。这是执行当时的镜像及运行状态验证，不代表 TM 账号认证成功，也不保证容器以后不会退出。

这里重启的是 TMM。TMM 启动时的恢复流程会调用 Compose，Compose 检测到镜像 ID 变化后重建 TM，并带上已保存的 Token。不要直接执行 `docker compose up tm`，也不要认为单纯 `docker restart tm` 能换成新镜像。[Docker 文档](https://docs.docker.com/reference/cli/docker/compose/up/)

拉取失败时，上面的脚本会停止，现有 TM 不会因此停止。新镜像已经应用后，TMM 不会自动恢复旧镜像；需要回退时，在同一个目录执行：

```bash
bash <<'ROLLBACK_TM_IMAGE'
set -euo pipefail
cd /root/tmm
manager_id=$(docker compose ps -q tmm)
image_ref=$(docker exec "$manager_id" printenv TM_IMAGE)
docker image inspect tmm-tm:previous >/dev/null
docker tag tmm-tm:previous "$image_ref"
docker compose restart tmm
docker compose logs --tail=30 tmm
ROLLBACK_TM_IMAGE
```

`tmm-tm:previous` 只保留最近一次手动更新前的镜像；不要在新版本尚未确认正常时反复执行升级命令，否则会覆盖这个备份标签。镜像启动和 TM 账号认证仍需分别确认。

### 多台 VPS 后续自动更新的建议

如果希望二十多台 VPS 不用逐台执行命令，最简单的扩展是**让现有 TMM 在每日检查时顺便拉取对应架构的官方镜像，比较镜像 ID；只有 Token 或镜像发生变化才重建 TM**。仍保持 TM/TMM 两个容器，不需要增加 Watchtower 或云端控制面。自动镜像升级需要额外保存旧镜像 ID 来支持真正的镜像回退。

这是一项后续功能建议，当前版本尚未实现自动镜像升级；本节手动命令适用于当前版本。

## 更新及失败处理

- Token 未变且 TM 正常时不重建；配置变化时只重建本项目里的 TM。
- 下载超时、HTTP 错误、内容为空、多行或明显的 HTML/JSON 内容，不应用、不改已保存的 Token。
- 新 TM 启动失败时，尝试使用 TMM 上次成功保存的配置恢复。首次启动没有保存记录时，没有旧配置可恢复。
- 移除脚本删除的旧 Agent 和旧 TM 无法通过上述回退恢复；TMM 的恢复逻辑只针对新方案运行后保存的配置。
- 启动成功表示观察期间容器仍在运行，不验证账号认证和流量。请在 TraffMonetizer 后台确认实际设备状态。

TMM 挂载 Docker socket，具有宿主机 Docker 管理权限，不开放管理端口，不需要 privileged。Token 文件公开可读，不执行其中的内容，只将它作为 TM 的参数。

## 文件与测试

`remove-old.sh` 为一次性移除及准备入口，`prepare-migration.py` 为它调用的固定辅助脚本；`tmm.py` 为运行中的 Token 检查程序，`Dockerfile` 和 `compose.yaml` 用于构建和启动。保留整个目录，不要单独拷贝移除脚本。

```bash
python3 -m unittest -v test_tmm test_remove_old test_update_tm
bash -n remove-old.sh update-tm.sh
docker compose --env-file .env.example config --quiet
```

移除流程测试使用临时目录和假 Docker/systemd 命令，不会操作测试主机的旧 Agent 或真实容器。

参考：[Compose profiles](https://docs.docker.com/compose/how-tos/profiles/)、[Compose up](https://docs.docker.com/reference/cli/docker/compose/up/)、[官方 TM 镜像](https://hub.docker.com/r/traffmonetizer/cli_v2)。
