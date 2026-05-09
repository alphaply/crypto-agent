# 生产部署与升级指南

本文档对应预览版本 `v0.2.0-preview.1`，推荐使用 Docker Hub 镜像 `alphaply/crypto-agent:v0.2.0-preview.1` 部署。

## 首次部署

```bash
mkdir -p crypto-agent
cd crypto-agent
curl -O https://raw.githubusercontent.com/alphaply/crypto-agent/v0.2.0-preview.1/docker-compose.yml
curl -O https://raw.githubusercontent.com/alphaply/crypto-agent/v0.2.0-preview.1/.env.template
cp .env.template .env
```

编辑 `.env`，至少设置：

```env
ADMIN_PASSWORD=your-strong-password
JWT_SECRET=your-long-random-jwt-secret
CONFIG_MASTER_KEY=your-long-stable-config-master-key
PORT=7860
RUN_SCHEDULER_IN_WEB=true
TIMEZONE=Asia/Shanghai
```

启动服务：

```bash
docker compose pull
docker compose up -d
docker compose logs -f
```

验证健康状态：

```bash
curl http://localhost:7860/health
```

访问地址：

- 公开看板：`http://your-server:7860/`
- 控制台：`http://your-server:7860/console/chat`
- 配置页：`http://your-server:7860/console/config`

## 数据与密钥

Compose 使用 named volume `crypto_agent_data` 挂载到容器内 `/app/data`。SQLite 运行数据、聊天检查点和日志都保存在该数据卷中。

`CONFIG_MASTER_KEY` 用于解密 SQLite 中保存的敏感配置。恢复或升级同一个数据卷时必须继续使用原来的 `CONFIG_MASTER_KEY`，否则已保存的交易所密钥和模型密钥无法解密，`/health` 会显示 degraded。

## 升级

同一预览线升级：

```bash
docker compose pull
docker compose up -d
docker compose logs -f
```

升级前建议备份：

```bash
docker compose exec crypto-agent python - <<'PY'
from backend.config_store import export_full_snapshot
import json
print(json.dumps(export_full_snapshot(include_secrets=True), ensure_ascii=False, indent=2))
PY
docker compose cp crypto-agent:/app/data ./crypto_agent_data_backup
```

不要在正常升级时运行 `docker compose down -v`，它会删除 `crypto_agent_data` 数据卷。

## 回滚

回滚时使用旧镜像 tag，并保持同一 `.env` 和同一 `crypto_agent_data` 数据卷：

```bash
docker compose pull
docker compose up -d
```

如果手动运行镜像：

```bash
docker run -d --name crypto-agent \
  --restart unless-stopped \
  -p 7860:7860 \
  -v crypto_agent_data:/app/data \
  -e ADMIN_PASSWORD=your-strong-password \
  -e JWT_SECRET=your-long-random-jwt-secret \
  -e CONFIG_MASTER_KEY=your-long-stable-config-master-key \
  alphaply/crypto-agent:v0.2.0-preview.1
```

## 生产安全检查

- 修改默认 `ADMIN_PASSWORD`。
- 使用强随机 `JWT_SECRET` 和 `CONFIG_MASTER_KEY`。
- 记录并安全保存当前 `CONFIG_MASTER_KEY`。
- 只向可信来源开放 `7860` 端口。
- 推荐通过 Nginx 或 Caddy 配置 HTTPS 反向代理。
- 定期备份 `/app/data` 或 Docker named volume。
- 关注 `/health` 状态和 `docker compose logs -f` 输出。

## 本地构建验证

从源码构建预览镜像：

```bash
docker build -t alphaply/crypto-agent:v0.2.0-preview.1 .
docker run --rm -p 7860:7860 \
  -e ADMIN_PASSWORD=local-password \
  -e JWT_SECRET=local-jwt-secret \
  -e CONFIG_MASTER_KEY=local-config-master-key \
  alphaply/crypto-agent:v0.2.0-preview.1
```

检查：

```bash
curl http://localhost:7860/health
```
