# 安装与运行

## 环境要求

- Python `3.10+`
- Node.js `18+`
- npm `9+`

## 安装依赖

```bash
uv sync
npm install --prefix frontend
```

如果本机还没有安装 `uv`：

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
- 如果使用实盘，还需要交易所 API 凭证

## 启动方式

后端统一启动命令：

```bash
uv run python -m backend.app
```

这个命令会同时启动：

- FastAPI API 服务
- 后台 scheduler 线程

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
npm run build --prefix frontend
```
