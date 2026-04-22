# Crypto Agent v2

基于 LLM + LangGraph 的加密交易系统，当前分支已经完成 Web 层前后分离：

- 后端：FastAPI + JWT
- 前端：React + Vite + Ant Design
- 核心交易与调度：统一收拢到 `backend/`

## 项目结构

- `backend/app/`: FastAPI 应用、JWT、路由、service 层
- `backend/agent/`: Agent graph、chat graph、tool wiring、prompt 模板
- `backend/utils/`: 市场数据、指标、日志、LLM 工具
- `backend/config.py`: 配置加载与 `SYMBOL_CONFIGS` 管理
- `backend/database.py`: SQLite 数据访问与迁移
- `backend/main_scheduler.py`: 调度器主实现
- `frontend/`: React 前端
- `dashboard.py`: 新 Web 启动入口，运行 FastAPI 服务
- `main_scheduler.py`: 根目录兼容入口，转发到 `backend.main_scheduler`

## 快速开始

```bash
uv sync
npm install --prefix frontend
uv run dashboard.py
```

开发前端时可单独启动 Vite：

```bash
npm run dev --prefix frontend
```

默认访问：

- API / 生产构建页面：`http://localhost:7860`
- 前端开发服务器：`http://localhost:5173`

## 鉴权

- 管理端、聊天端、历史页、配置页使用 JWT 鉴权
- 登录密码读取 `.env` 中的 `CHAT_PASSWORD` 或 `ADMIN_PASSWORD`
- 公开统计页 `/public` 可直接访问

## 常用命令

```bash
uv run dashboard.py
uv run main_scheduler.py
uv run backend/utils/test_agent_connection.py
npm run build --prefix frontend
```

## 说明

- `.env` 仍然是运行时配置源，`SYMBOL_CONFIGS` 继续由后端读写
- `trading_data.db` 仍然是主数据库
- 这个分支已经移除旧 Flask/Jinja Web 层，新的页面全部来自 `frontend/`
