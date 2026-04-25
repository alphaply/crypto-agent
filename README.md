# Crypto Agent v2

Crypto Agent 现在采用“双层配置”：

- `.env` 或系统环境变量：只负责启动级、鉴权级、安全级参数
- `trading_data.db`：负责运行期配置，由 `/console/config` 的表单维护

这意味着交易对、模型、交易所密钥、Prompt、定价等内容，不再把 `.env` 当作日常配置入口。

如果你不想使用 `.env` 文件本身，也可以直接把同名变量配置到系统环境中；只是这些启动级变量仍然需要存在。本地开发最省事的方式仍然是保留 `.env`。

## 路由

- `http://localhost:7860/`: 公开 Dashboard
- `http://localhost:7860/usage`: 公开用量统计
- `http://localhost:7860/console/chat`: 登录后的控制台
- `http://localhost:5173/`: Vite 前端开发服务器

## 快速开始

1. 安装依赖

```bash
uv sync
npm install --prefix frontend
```

2. 初始化启动级配置

```bash
# Windows PowerShell
Copy-Item .env.template .env

# macOS / Linux
cp .env.template .env
```

3. 至少修改这些变量

- `CHAT_PASSWORD` 或 `ADMIN_PASSWORD`
- `JWT_SECRET`
- `CONFIG_MASTER_KEY`

4. 启动后端

```bash
uv run python -m backend.app
```

5. 启动前端开发环境（可选）

```bash
npm run dev --prefix frontend
```

6. 访问 `http://localhost:7860/console/chat`，使用你在 `.env` 里设置的密码登录

## `.env` 现在还负责什么

推荐保留在 `.env` 里的只有这些：

- `CHAT_PASSWORD`: 控制台登录密码，优先使用
- `ADMIN_PASSWORD`: 兼容旧逻辑的回退密码
- `JWT_SECRET`: JWT 签名密钥
- `JWT_EXPIRE_HOURS`: 登录态有效期
- `CONFIG_MASTER_KEY`: SQLite 中密钥字段的加密主密钥
- `PORT`: FastAPI 监听端口
- `RUN_SCHEDULER_IN_WEB`: 是否随 Web 进程启动调度器线程
- `TIMEZONE`: 时区

不再建议继续放在 `.env` 里日常维护的内容：

- `SYMBOL_CONFIGS`
- 交易所 API Key / Secret / Passphrase
- Agent 的 LLM 配置
- Prompt 选择
- 定价配置
- 杠杆、调度周期、DCA 运行参数

这些现在都应该在 `/console/config` 中维护。

## 初始密码怎么设置

- 首次登录密码优先读取 `CHAT_PASSWORD`
- 如果 `CHAT_PASSWORD` 为空，则回退到 `ADMIN_PASSWORD`
- `.env.template` 默认给了 `123456`，只是为了本地首次启动方便，非本地环境请立即修改

## 密码怎么更改

1. 修改 `.env` 中的 `CHAT_PASSWORD`，或同步修改 `CHAT_PASSWORD` 与 `ADMIN_PASSWORD`
2. 重启后端进程
3. 重新登录控制台

如果你还想让已经登录的用户立即失效，再一并更换 `JWT_SECRET`。只改密码时，已有 JWT 一般会在过期前继续有效。

## 运行期配置现在放哪里

- 入口：`/console/config`
- 存储：`trading_data.db`
- 敏感字段：使用 `CONFIG_MASTER_KEY` 进行加密后落库
- 编辑方式：表单编辑，不再要求手写 JSON 或手改 `.env`

兼容旧版本的迁移逻辑：

- 如果配置表还是空的，而你的旧 `.env` 里仍然有 `SYMBOL_CONFIGS`、旧版交易所密钥或旧版 LLM 配置，系统会在首次启动时尝试导入一次
- 一旦导入完成，后续运行期配置就以 SQLite 为准，再去改 `.env` 里的这些旧字段不会继续覆盖数据库

## 验证

```bash
uv run python -m unittest discover -s tests
npm run build --prefix frontend
```

## 目录

- `backend/app/`: FastAPI、鉴权、路由、服务层
- `backend/app/core/scheduler.py`: 调度器
- `backend/agent/`: Agent 图、Prompt、工具
- `backend/config.py`: 运行期配置门面
- `backend/config_store.py`: SQLite 配置存储与密钥加密
- `backend/database.py`: SQLite 数据与建表
- `frontend/`: React + Vite 前端
- `docs/`: 安装、配置、FAQ 等文档
