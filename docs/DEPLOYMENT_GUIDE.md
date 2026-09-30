# 生产部署与升级指南

当前发布使用 Docker Hub 镜像 `alphaply712/crypto-agent:latest`。宝塔及已有 Compose 中的镜像名应保持一致；Web 和独立调度容器必须更新到同一镜像。

## 宝塔镜像更新

先拉取 `alphaply712/crypto-agent:latest`，再用新镜像重新创建原容器。保留原端口、数据卷、环境变量和 `CONFIG_MASTER_KEY`。推送或拉取镜像本身不会替换正在运行的容器。

```bash
docker pull alphaply712/crypto-agent:latest
docker image inspect alphaply712/crypto-agent:latest --format '{{json .RepoDigests}}'
```

发布时记录推送所得的 digest；需要精确回滚时使用之前记录的 `alphaply712/crypto-agent@sha256:...`，避免可变的 `latest` 标签指向新版本。

## 首次部署

```bash
mkdir -p crypto-agent
cd crypto-agent
curl -O https://raw.githubusercontent.com/alphaply/crypto-agent/beta/.env.template
cp .env.template .env
```

编辑 `.env`，至少设置：

```env
ADMIN_PASSWORD=your-strong-password
JWT_SECRET=your-long-random-jwt-secret
CONFIG_MASTER_KEY=your-long-stable-config-master-key
PORT=7860
RUN_SCHEDULER_IN_WEB=true
SCHEDULER_MAX_WORKERS=2
TIMEZONE=Asia/Shanghai
DAILY_SUMMARY_TIME=00:05
DAILY_SUMMARY_RETRY_MINUTES=15
SHORT_MEMORY_RETRY_MINUTES=15
```

其中 `RUN_SCHEDULER_IN_WEB=true` 适用于单进程运行方式；下面分容器部署显式覆盖为 false，并单独启动 `crypto-agent-scheduler`。仓库不提供 Compose 文件，已有自维护 Compose 可沿用相同镜像、数据卷和配置。
每日总结默认在所配置时区的 `00:05` 汇总前一天数据；若调度器错过该时刻，会在恢复后补跑。总结模型调用失败时不会保存 Prompt 回显，并会按 `DAILY_SUMMARY_RETRY_MINUTES` 重试。
短期记忆按四小时窗口生成（`00:00`、`04:00`、`08:00`、`12:00`、`16:00`、`20:00`）；若调度器在整点后启动，会自动补生成最近一个已结束的窗口。生成失败时默认每 15 分钟重试，可通过 `SHORT_MEMORY_RETRY_MINUTES` 调整。

启动服务：

```bash
docker pull alphaply712/crypto-agent:latest
docker run -d --name crypto-agent --restart unless-stopped \
  --env-file .env -e RUN_SCHEDULER_IN_WEB=false \
  -p 31421:7860 -v crypto_agent_data:/app/data \
  alphaply712/crypto-agent:latest
docker run -d --name crypto-agent-scheduler --restart unless-stopped --no-healthcheck \
  --env-file .env -e RUN_SCHEDULER_IN_WEB=false \
  -v crypto_agent_data:/app/data \
  alphaply712/crypto-agent:latest uv run --no-sync python -m backend.app.core.scheduler
```

以上命令启动两个容器，且只有一个调度器：

- `crypto-agent`：仅提供 FastAPI + 前端页面。
- `crypto-agent-scheduler`：仅执行定时 agent 任务。

这样可以避免 scheduler 在同一进程里执行大模型/市场分析时把 Web 请求拖慢。调度器不提供 HTTP 服务，所以关闭其继承的 Web 健康检查。对 2 核 2G 机器，建议把 `SCHEDULER_MAX_WORKERS` 保持在 `1` 或 `2`。

验证健康状态：

```bash
curl http://localhost:31421/health
```

访问地址：

- 公开看板：`http://your-server:31421/`
- 控制台：`http://your-server:31421/console/chat`
- 配置页：`http://your-server:31421/console/config`

## 数据与密钥

两个容器都将 named volume `crypto_agent_data` 挂载到 `/app/data`。SQLite 运行数据、聊天检查点和日志都保存在该数据卷中。

`CONFIG_MASTER_KEY` 用于解密 SQLite 中保存的敏感配置。恢复或升级同一个数据卷时必须继续使用原来的 `CONFIG_MASTER_KEY`，否则已保存的交易所密钥和模型密钥无法解密，`/health` 会显示 degraded。

## 升级

记录当前容器镜像 ID / 已保存 digest，并先拉取新镜像。停止写入后备份数据，再删除旧容器并按「首次部署」命令重建两个容器；删除容器不删除命名数据卷：

```bash
docker inspect crypto-agent --format '{{.Image}}'
docker pull alphaply712/crypto-agent:latest
docker stop crypto-agent crypto-agent-scheduler
docker cp crypto-agent:/app/data ./crypto_agent_data_backup
docker rm crypto-agent crypto-agent-scheduler
```

重建完成后检查两边日志和 Web 健康状态：

```bash
docker logs --tail 100 crypto-agent
docker logs --tail 100 crypto-agent-scheduler
curl http://localhost:31421/health
```

使用新的备份目录名保留每次快照；备份目录包含密钥和交易数据，不要提交或打包到镜像。升级不要删除 `crypto_agent_data`。已有自维护 Compose 也不要执行 `docker compose down -v`。

本次版本启动时自动创建交易规则/版本及操作记录等附加表，保留历史订单与记忆。旧 REAL 默认可选 TP/SL、旧 STRATEGY 默认必填；新任务默认必填，已有任务不会自动切换成独立退出。需要独立退出时，在账户持仓、挂单和待核验操作全部处理完后，到任务设置中选择。服务器 Web 与调度器都必须使用同一版本，避免新旧退出维护逻辑并行。

## 回滚

回滚时将「首次部署」中的镜像替换为已记录的旧 digest，并保持同一 `.env` 和数据卷。新版本独立退出周期未结束前，不应直接让旧版本接管；先处理活动仓位和委托，或继续运行能识别这些计划的维护进程。只有确需恢复数据且已处理交易所当前状态后，才考虑使用备份，不能用旧数据库掩盖已发生的成交。

```bash
docker pull alphaply712/crypto-agent@sha256:替换为已记录的旧版本摘要
```

单进程部署也可显式开启 Web 内调度器；不要同时运行独立调度器：

```bash
docker run -d --name crypto-agent \
  --restart unless-stopped \
  -p 31421:7860 \
  -v crypto_agent_data:/app/data \
  -e ADMIN_PASSWORD=your-strong-password \
  -e JWT_SECRET=your-long-random-jwt-secret \
  -e CONFIG_MASTER_KEY=your-long-stable-config-master-key \
  -e RUN_SCHEDULER_IN_WEB=true \
  alphaply712/crypto-agent:latest
```

## 生产安全检查

- 修改默认 `ADMIN_PASSWORD`。
- 使用强随机 `JWT_SECRET` 和 `CONFIG_MASTER_KEY`。
- 记录并安全保存当前 `CONFIG_MASTER_KEY`。
- 只向可信来源开放对外映射端口（示例为 `31421`）。
- 推荐通过 Nginx 或 Caddy 配置 HTTPS 反向代理。
- 定期备份 `/app/data` 或 Docker named volume。
- 关注 `/health` 状态及两个容器的 `docker logs -f` 输出。

## 本地构建验证

先完成后端与前端回归，再重新生成前端产物。推荐使用预编译前端的构建入口，不能复用旧 dist：

```bash
npm run build --prefix frontend
docker build -f Dockerfile.prebuilt -t alphaply712/crypto-agent:latest .
docker run --rm -p 31421:7860 \
  -e ADMIN_PASSWORD=local-password \
  -e JWT_SECRET=local-jwt-secret \
  -e CONFIG_MASTER_KEY=local-config-master-key \
  -e RUN_SCHEDULER_IN_WEB=false \
  alphaply712/crypto-agent:latest
```

检查：

```bash
curl http://localhost:31421/health
```

本地烟测使用镜像内空数据目录，不挂载生产数据库，关闭调度器。也可用 `docker build -t alphaply712/crypto-agent:latest .` 执行多阶段完整构建；`NODE_IMAGE` / `PYTHON_IMAGE` 可调整基础镜像来源，`NPM_REGISTRY` 可调整前端依赖源。

验证后提交并推送 `beta`，再发布镜像并记录 Git commit 与镜像 digest：

```bash
docker push alphaply712/crypto-agent:latest
docker image inspect alphaply712/crypto-agent:latest --format '{{json .RepoDigests}}'
```

推送镜像不会自动重建服务器容器。`.env`、`pricing.json`、SQLite 数据库及 WAL 文件由 `.dockerignore` 排除，不能作为镜像发布内容。
