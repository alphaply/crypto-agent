# FAQ

## 1. 现在到底还需不需要 `.env`？

需要“环境变量”，但不一定非得是 `.env` 这个文件本体。

你可以：

- 本地开发时使用 `.env`
- 部署时改为系统环境变量、容器环境变量或 CI/CD 注入

不过当前仍然需要一小组启动级变量，例如：

- `CHAT_PASSWORD` / `ADMIN_PASSWORD`
- `JWT_SECRET`
- `CONFIG_MASTER_KEY`

## 2. 为什么看起来很多配置已经不走 `.env` 了？

因为运行期配置已经迁移到 SQLite 了。

现在下面这些内容都应该在 `/console/config` 中编辑：

- 交易对与 Agent 配置
- LLM 配置
- 交易所密钥
- Prompt
- 定价

`.env` 只保留启动级和安全级配置。

## 3. 初始登录密码怎么设置？

优先读取 `CHAT_PASSWORD`，如果它为空，则回退到 `ADMIN_PASSWORD`。

本地第一次启动时，直接在 `.env` 里设置即可。

## 4. 控制台密码怎么修改？

1. 编辑 `.env`
2. 修改 `CHAT_PASSWORD`，或同步修改 `CHAT_PASSWORD` 与 `ADMIN_PASSWORD`
3. 重启后端

如果希望当前已登录会话马上失效，再额外更换 `JWT_SECRET`。

## 5. 为什么我改了 `.env` 里的 `SYMBOL_CONFIGS` 或交易所密钥却没生效？

这是现在最常见的误解。

新版本里，运行期配置以 SQLite 为准。旧 `.env` 字段只会在首次兼容导入时尝试导入一次，不会持续和数据库双向同步。

也就是说：

- 首次迁移后，请改 `/console/config`
- 不要把 `.env` 当成运行期配置中心继续维护

## 6. 配置最终存在哪里？

- 启动级配置：环境变量
- 运行期配置：`trading_data.db`
- 敏感字段：写入 SQLite 前会使用 `CONFIG_MASTER_KEY` 加密

## 7. 交易所或模型密钥应该放哪里？

放在 `/console/config` 表单里。

当前推荐流程是：

1. 先在 `.env` 配好登录密码、JWT 和加密主密钥
2. 启动服务
3. 登录 `/console/chat`
4. 到 `/console/config` 录入 Agent、交易所和模型配置
