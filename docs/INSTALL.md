# 安装与运行

## 环境要求

- Python `3.10+`
- Node.js `18+`
- npm `9+`
- 可访问交易所 API 和模型 API

## 获取项目

```bash
git clone https://github.com/alphaply/crypto-agent.git
cd crypto-agent
```

## 安装依赖

```bash
uv sync
npm install --prefix frontend
```

如果没有安装 `uv`：

```bash
# Windows PowerShell
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"

# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh
```

## 初始化配置

```bash
# Windows PowerShell
Copy-Item .env.template .env

# macOS / Linux
cp .env.template .env
```

至少需要配置：

- `ADMIN_PASSWORD` 或 `CHAT_PASSWORD`
- `SYMBOL_CONFIGS`
- 每个配置对应的 `model`、`api_key`、`api_base`
- 实盘模式下需要交易所 API 凭证

## 启动方式

启动 Web 服务：

```bash
uv run dashboard.py
```

单独启动调度器：

```bash
uv run main_scheduler.py
```

前端开发模式：

```bash
npm run dev --prefix frontend
```

访问地址：

- 生产构建 / API：`http://localhost:7860`
- 前端开发：`http://localhost:5173`

## 验证

```bash
uv run backend/utils/test_agent_connection.py
```

如需验证前端是否可打包：

```bash
npm run build --prefix frontend
```
