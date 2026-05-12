# Crypto Agent

Crypto Agent 是一个基于 FastAPI、React 和 LangGraph 的加密货币交易代理项目。它提供多策略 Agent 调度、行情分析、K 线可视化、交易记录、成本统计、聊天控制台和 Web 化运行配置。

项目适合用于本地研究、策略复盘和自托管部署。默认不会把交易所、模型 API Key 等敏感配置写进代码，运行后的策略、模型、交易所密钥、提示词和价格配置主要在 WebUI 中维护，并保存到 SQLite 数据库。

## 功能概览

- FastAPI 后端和 React + Vite 前端
- 多 Agent 策略配置和定时调度
- K 线、均线、持仓、订单和盈亏展示
- 聊天控制台、运行配置页、公开用量统计页
- SQLite 本地状态存储
- Docker Compose 部署，Web 服务和调度器分容器运行

## 项目结构

```text
backend/app/              FastAPI 应用、认证、API 路由和服务层
backend/app/core/         调度器、运行时和安全相关逻辑
backend/agent/            Agent 图、工具和提示词模板
backend/utils/            行情、指标、日志和 LLM 工具
backend/config.py         运行配置入口
backend/database.py       SQLite 数据访问层
frontend/                 React + Vite 前端
docs/                     部署和产品文档
```

运行状态文件主要包括 `.env`、`trading_data.db`、日志和本地价格配置。不要提交真实密钥和运行数据库。

## 普通安装

### 环境要求

- Python 3.10+
- Node.js 20+
- uv
- npm

### 安装依赖

```bash
uv sync
npm install --prefix frontend
```

### 初始化配置

```bash
cp .env.template .env
```

Windows PowerShell:

```powershell
Copy-Item .env.template .env
```

至少修改 `.env` 中的这些值：

```env
ADMIN_PASSWORD=your-strong-password
JWT_SECRET=your-long-random-jwt-secret
CONFIG_MASTER_KEY=your-long-stable-config-master-key
HOST_PORT=31421
APP_PORT=7860
RUN_SCHEDULER_IN_WEB=true
SCHEDULER_MAX_WORKERS=2
TIMEZONE=Asia/Shanghai
```

`ADMIN_PASSWORD` 用于登录控制台，`JWT_SECRET` 用于会话签名，`CONFIG_MASTER_KEY` 用于加密 SQLite 中保存的密钥。已有数据库继续使用时，不要更换 `CONFIG_MASTER_KEY`。

### 启动开发环境

启动后端和调度器：

```bash
uv run python -m backend.app
```

启动前端开发服务器：

```bash
npm run dev --prefix frontend
```

常用地址：

- `http://localhost:7860/`：后端服务和生产静态页面入口
- `http://localhost:7860/health`：健康检查
- `http://localhost:7860/console/chat`：聊天控制台
- `http://localhost:7860/console/config`：运行配置
- `http://localhost:5173/`：Vite 开发服务器

### 构建前端

```bash
npm run build --prefix frontend
```

构建完成后，FastAPI 会用于生产静态文件服务。

## Docker 安装

### 准备配置

```bash
mkdir crypto-agent
cd crypto-agent
curl -O https://raw.githubusercontent.com/alphaply/crypto-agent/beta/docker-compose.yml
curl -O https://raw.githubusercontent.com/alphaply/crypto-agent/beta/.env.template
cp .env.template .env
```

编辑 `.env`：

```env
ADMIN_PASSWORD=your-strong-password
JWT_SECRET=your-long-random-jwt-secret
CONFIG_MASTER_KEY=your-long-stable-config-master-key
HOST_PORT=31421
APP_PORT=7860
SCHEDULER_MAX_WORKERS=2
TIMEZONE=Asia/Shanghai
```

### 启动服务

```bash
docker compose pull
docker compose up -d
docker compose logs -f crypto-agent crypto-agent-scheduler
```

默认访问地址：

```text
http://localhost:31421/
```

健康检查：

```bash
curl http://localhost:31421/health
```

Docker Compose 会启动两个容器：

- `crypto-agent`：Web API 和前端静态资源
- `crypto-agent-scheduler`：后台调度器

运行数据保存在 `crypto_agent_data` volume 的 `/app/data` 中。升级或重启时保留该 volume，并保持 `CONFIG_MASTER_KEY` 不变，否则已保存的加密密钥无法解密。

### 常用 Docker 命令

```bash
docker compose logs -f
docker compose pull
docker compose up -d
docker compose down
```

不要在正常升级时执行 `docker compose down -v`，它会删除数据库和运行状态。

### 从源码构建镜像

```bash
docker build -t alphaply712/crypto-agent:local .
docker run --rm -p 31421:7860 \
  -e ADMIN_PASSWORD=local-password \
  -e JWT_SECRET=local-jwt-secret \
  -e CONFIG_MASTER_KEY=local-config-master-key \
  alphaply712/crypto-agent:local
```

## WebUI 配置

首次启动后进入 `/console/config` 维护运行配置，包括：

- Agent、交易对、模式、调度周期和提示词
- LLM 模型、API Base、API Key、temperature 和扩展参数
- 交易所 API Key、Secret 和 Passphrase
- 汇总提示词、短期记忆、模型价格和统计配置

密钥会通过 `CONFIG_MASTER_KEY` 加密后保存在 SQLite 中。

## 验证

```bash
uv run python -m unittest discover -s tests
npm run build --prefix frontend
uv run backend/utils/test_agent_connection.py
```

其中模型连通性测试需要先配置可用的模型 API Key。

## 安全提示

- 不要提交真实 API Key、`.env`、`pricing.json`、`trading_data.db` 或日志文件
- 生产环境务必设置强 `ADMIN_PASSWORD`
- 泄露后应立即轮换 `JWT_SECRET` 和相关 API Key
- 同一份数据库必须长期使用同一个 `CONFIG_MASTER_KEY`
