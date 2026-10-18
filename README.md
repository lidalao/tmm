# TM + TMM：一个 Compose，两个容器

`tm` 运行官方 TraffMonetizer 镜像；`tmm` 定时下载 GitHub Raw 上的一行明文 Token，发生变化时重建 `tm`，确认启动后保存新 Token。无需 Worker、数据库、节点注册或宿主机 Python。TMM 使用 Python 标准库，通过容器内的 Docker CLI 和 Compose 操作同一个项目的 `tm`。

## 文件

```text
tmm/
├── compose.yaml
├── Dockerfile
├── tmm.py
├── .dockerignore
├── .env.example
├── token.txt
├── test_tmm.py
└── README.md
```

将整个目录放到每台 VPS。需要 Docker Engine 和 Docker Compose v2，宿主机 Docker socket 位于 `/var/run/docker.sock`。TMM 镜像支持 AMD64、ARM64；TM 镜像标签须自行选对架构。无需开放入站端口。

## 1. 准备 GitHub Token 文件

本项目的公开仓库为 `https://github.com/lidalao/tmm`，根目录 `token.txt` 只放一行真实 Token，不加引号、不写 JSON：

```text
YOUR_TM_TOKEN
```

固定读取地址为 `https://raw.githubusercontent.com/lidalao/tmm/main/token.txt`，`.env.example` 已预填。以后修改仓库根目录 `token.txt` 并提交到 `main` 即可。使用分支地址，固定提交 SHA 的地址不会跟随更新。公开文件及其提交历史中的 Token 均公开可读，这是本方案按要求采用的明文分发方式。

## 2. 配置 .env

在 VPS 下载仓库并配置：

```bash
git clone https://github.com/lidalao/tmm.git
cd tmm
cp .env.example .env
nano .env
```

至少修改地址，建议也修改设备名称：

```dotenv
TM_TOKEN_URL=https://raw.githubusercontent.com/lidalao/tmm/main/token.txt
TM_CHECK_INTERVAL=86400
TM_DEVICE_NAME=你的VPS名称
```

其他配置：

| 配置 | 默认值 | 含义 |
|---|---|---|
| `TM_CHECK_INTERVAL` | `86400` | 正常检查间隔，单位秒，最小 10 秒 |
| `TM_RETRY_INTERVAL` | `300` | 获取或应用失败后的重试间隔，最小 10 秒 |
| `TM_START_GRACE` | `10` | 创建后额外观察容器的秒数，0～300 |
| `TM_IMAGE` | `traffmonetizer/cli_v2:latest` | AMD64 默认镜像；ARM64 改为 `traffmonetizer/cli_v2:arm64v8` |
| `TM_USER` | `0` | TM 运行用户，按需要设置 UID 或 UID:GID |
| `COMPOSE_PROJECT_NAME` | `tm-stack` | Compose 项目名，同机多套时分别设置 |
| `TM_CONTAINER_NAME` | `tm` | TM 容器名称，同机多套时分别设置 |

所有 VPS 可以使用同一个 Raw 地址。间隔改为 `3600` 就是每小时检查一次。修改 `.env` 后运行 `docker compose up -d --build`，让 TMM 使用新配置；`docker compose restart` 不会重新读取 `.env`。

## 3. 已有 tm / 旧 Agent 的 VPS：先迁移一次

先停用旧 Agent，避免它继续按照旧控制面的配置重建容器。仅在安装过旧 Agent 的 VPS 执行：

```bash
sudo systemctl disable --now tm-agent.service
```

按旧容器的参数填写 `.env` 中的镜像、运行用户和设备名。旧容器由 `docker run --name tm ...` 创建时，先停止并改名备份，给新 Compose 容器让出名称：

```bash
docker stop tm
docker update --restart=no tm
docker rename tm tm-before-tmm
```

若备份名称已存在，请换一个备份名称。旧容器保留用于人工恢复。本方案不会自动接管或删除不属于本项目的同名容器，不迁移旧容器的自定义挂载、网络、环境变量；需要这些设置时请先补充到 `compose.yaml` 的 `tm` 服务。没有旧 `tm` 的新 VPS 直接跳过本节。

## 4. 启动

在含有 `compose.yaml` 的目录执行：

```bash
docker compose up -d --build
docker compose logs -f tmm
```

日志示例：

```text
tmm 已启动：每 86400 秒检查一次，失败后 300 秒重试
首次启动或配置发生变化，正在创建 tm
tm 已启动，新 Token 已保存（未验证 TM 账号认证）
Token 未变化，tm 运行正常，无需重建
```

`tm` 和 `tmm` **都声明在同一个 compose.yaml 里**。`tm` 使用 `managed` profile，普通启动先启动 TMM；TMM 获取有效 Token 后显式执行 `docker compose up ... tm`，才创建 TM。这样首次启动不需要手填 Token，也不会用空 Token 启动官方镜像。不要自己执行 `docker compose up tm` 或 `docker compose --profile managed up`，这些命令没有 TMM 注入的 Token。

TMM 会在首次启动时下载缺失的 TM 镜像；已有镜像不会因为更新 Token 被自动拉取最新版。一次重建会短暂停止 TM。TMM 自身保持运行，不重建其他服务。

## 5. 更新 Token、检查和停止

以后只修改 GitHub 的 `token.txt` 并提交。各 VPS 在下次检查时自动应用。

```bash
# 看 TMM 的检查、更新及失败原因（不打印 Token）
docker compose logs --tail=50 tmm

# 查看两个服务的状态
docker compose --profile managed ps -a

# 立即检查一次，不等正常间隔；复用保存的 Token 后马上检查远端
docker compose restart tmm

# 升级 TMM 源码后，重新构建并启动
docker compose up -d --build

# 停止并移除两个服务，保留保存的 Token 数据
docker compose --profile managed down
```

停止整个项目要带 `--profile managed`，否则 Compose 默认只移除 TMM。不要加 `-v`，它会删除保存 Token 的数据卷。单独停 `tm` 而让 TMM 继续运行，TMM 会在下次检查时恢复它。

Token、上次成功应用的镜像/用户/设备名保存在项目的 `tmm-data` 命名卷中，文件为 `/data/applied.json`，权限 `600`。TMM 重建、VPS 重启后均保留。启动时会先按已保存的配置恢复 TM，因此 GitHub 临时不可用不会妨碍恢复已保存的 TM 配置。

## 失败处理

- 下载超时、HTTP 错误、内容为空、多行、HTML/JSON 等异常内容：不应用，不改本地 Token；按失败间隔重试。
- Token 未变化且 TM 正常：不重建。
- 新 Token 或本机镜像/用户/设备名配置变化：只重建本项目的 TM，启动检查通过后再原子保存新状态。
- 新容器无法创建或很快退出：尝试用已保存的旧配置重新创建，旧状态文件不改。首次运行没有旧配置时无法回退。
- 更新过程中 TMM 意外退出：下次启动根据保存的旧配置恢复，再获取远端重试。
- “启动成功”只表示容器在观察期间运行，不验证 Token 对应账号是否有效、流量是否产生，也不承诺应用级回滚；账号认证结果在 TraffMonetizer 后台确认。

TMM 挂载 Docker socket，具有管理宿主机 Docker 的权限；无需 `privileged`，不开放管理端口。不执行远端文件中的任何命令，只把下载到的一行文本作为 TM 的 Token 参数。

## 代码验证

在含有 `compose.yaml` 的目录运行：

```bash
python3 -m unittest -v test_tmm
docker compose --env-file .env.example config --quiet
docker compose --env-file .env.example build tmm
```

实现依据：[Compose 显式启动 profile 中的服务](https://docs.docker.com/compose/how-tos/profiles/)、[Compose up 的配置变化重建行为](https://docs.docker.com/reference/cli/docker/compose/up/)、[官方 TM 镜像与启动参数](https://hub.docker.com/r/traffmonetizer/cli_v2)。
