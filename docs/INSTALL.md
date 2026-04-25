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

如果本机还没有 `uv`：

```bash
# Windows PowerShell
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"

# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh
```

## 初始化 `.env`

```bash
# Windows PowerShell
Copy-Item .env.template .env

# macOS / Linux
cp .env.template .env
```

至少要设置这些变量：

- `CHAT_PASSWORD` 或 `ADMIN_PASSWORD`
- `JWT_SECRET`
- `CONFIG_MASTER_KEY`

可选变量：

- `JWT_EXPIRE_HOURS`
- `PORT`
- `RUN_SCHEDULER_IN_WEB`
- `TIMEZONE`

说明：

- `.env` 现在只负责启动级和安全级配置
- 交易对、模型、交易所密钥、Prompt、定价等运行期配置，不再要求写在 `.env`
- 这些运行期配置统一在 `/console/config` 中编辑，并存储到 `trading_data.db`

## 启动方式

后端统一启动命令：

```bash
uv run python -m backend.app
```

前端开发模式：

```bash
npm run dev --prefix frontend
```

访问地址：

- FastAPI / 构建后的前端：`http://localhost:7860`
- Vite 开发服务器：`http://localhost:5173`
- 控制台登录页：`http://localhost:7860/console/chat`

## 首次登录

- 控制台密码优先读取 `CHAT_PASSWORD`
- 如果 `CHAT_PASSWORD` 为空，则回退到 `ADMIN_PASSWORD`
- 模板默认值是 `123456`，仅用于本地第一次启动，正式环境请务必修改

## 修改控制台密码

1. 编辑 `.env`
2. 修改 `CHAT_PASSWORD`，或同步修改 `CHAT_PASSWORD` 与 `ADMIN_PASSWORD`
3. 重启后端

如果你希望当前已登录会话立刻失效，再额外更换 `JWT_SECRET`。

## 运行期配置入口

启动后进入 `/console/config`，可以通过表单编辑：

- 全局运行参数
- Agent 配置
- 交易所凭证
- LLM 凭证
- Prompt
- 定价

敏感字段会在 SQLite 中加密保存。

## 兼容旧版 `.env`

如果你是从旧版本升级：

- 首次启动时，如果 SQLite 配置表还是空的，系统会尝试从旧 `.env` 中导入 `SYMBOL_CONFIGS` 和相关运行期字段
- 导入完成后，运行期配置以数据库为准
- 后续再改 `.env` 中的旧字段，不会继续覆盖数据库内容

## 验证

```bash
uv run python -m unittest discover -s tests
npm run build --prefix frontend
```
