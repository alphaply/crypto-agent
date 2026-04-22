# Crypto Agent v2

这个分支已经完成前后分离：

- 后端：FastAPI + JWT
- 前端：React + Vite + Ant Design
- 交易、调度、配置、数据库逻辑：统一收口到 `backend/`

## 项目结构

- `backend/app/`: FastAPI 应用、路由、service、运行时
- `backend/app/core/scheduler.py`: 调度器主实现
- `backend/agent/`: agent graph、chat graph、tools、prompts
- `backend/utils/`: 市场数据、指标、日志、LLM 工具
- `backend/config.py`: 配置加载与 `SYMBOL_CONFIGS` 管理
- `backend/database.py`: SQLite 数据访问
- `frontend/`: React 前端
- `docs/`: 安装和说明文档

## 快速开始

```bash
uv sync
npm install --prefix frontend
uv run python -m backend.app
```

前端单独开发时：

```bash
npm run dev --prefix frontend
```

默认访问：

- API / 已构建前端页面：`http://localhost:7860`
- 前端开发服务器：`http://localhost:5173`

## 常用命令

```bash
uv run python -m backend.app
uv run backend/utils/test_agent_connection.py
npm run build --prefix frontend
```

## 说明

- 后端启动命令已经统一为 `uv run python -m backend.app`
- 这个命令会同时启动 FastAPI API 服务和后台 scheduler
- 不再保留单独的 `dashboard.py` 和 `main_scheduler.py` 启动入口
- `.env` 仍然是运行时配置来源，`trading_data.db` 仍然位于项目根目录
