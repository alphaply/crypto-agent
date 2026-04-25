# 配置指南

## 1. 先分清两类配置

当前项目不是“完全不用环境变量”，而是把配置拆成了两层：

- 启动级 / 安全级配置：放在 `.env` 或系统环境变量里
- 运行期配置：放在 `trading_data.db`，通过 `/console/config` 的表单维护

如果你把交易参数继续写回 `.env`，通常只会在“首次兼容导入”阶段生效，不再是日常维护入口。

## 2. `.env` 应该放什么

推荐只保留这些：

- `CHAT_PASSWORD`
- `ADMIN_PASSWORD`
- `JWT_SECRET`
- `JWT_EXPIRE_HOURS`
- `CONFIG_MASTER_KEY`
- `PORT`
- `RUN_SCHEDULER_IN_WEB`
- `TIMEZONE`

它们分别负责：

- 控制台登录口令
- JWT 签名与过期时间
- SQLite 中敏感字段的加密
- Web 服务端口
- 是否随着 Web 进程启动调度器
- 时区

## 3. `/console/config` 里维护什么

运行期配置统一在 WebUI 中编辑，主要包括：

- 全局参数：默认杠杆、调度开关、LLM 超时、重试次数、全局 summarizer
- Agent 参数：`config_id`、`symbol`、`mode`、`prompt_file`、`run_interval`、DCA 参数等
- 模型配置：`model`、`api_base`、`temperature`
- 交易所配置：交易所类型、市场类型、Agent 级密钥
- 高级覆盖：`extra_body`
- Prompt 内容
- 模型定价

这些内容会保存到 `trading_data.db`，不是 `.env`。

## 4. 密钥放在哪里

- 交易所 API Key / Secret / Passphrase
- LLM API Key
- Summarizer API Key

这些都通过 `/console/config` 表单录入，最终保存在 SQLite 的 `secret_store` 表中，并使用 `CONFIG_MASTER_KEY` 加密后落库。

## 5. 初始密码与改密码

控制台登录密码不在 `/console/config` 里改。

- 初始密码优先读取 `CHAT_PASSWORD`
- 如果 `CHAT_PASSWORD` 为空，则回退到 `ADMIN_PASSWORD`
- 修改密码时，编辑 `.env` 后重启后端

如果你想让所有已登录用户立即重新登录，再额外更换 `JWT_SECRET`。

## 6. 旧版 `.env` 怎么迁移

为了兼容老版本，系统保留了一次性导入逻辑：

- 如果 SQLite 里的配置表还是空的
- 且旧 `.env` 中仍然存在 `SYMBOL_CONFIGS`、旧版交易所密钥、旧版 LLM 配置

那么首次启动时会尝试导入一次。

注意：

- 这只是迁移，不是双向同步
- 一旦导入完成，后续运行期配置以数据库为准
- 后面再改 `.env` 里的旧运行期字段，通常不会再覆盖数据库

## 7. 常见误区

- “我把 `SYMBOL_CONFIGS` 改了，为什么前台没变？”
  现在应该去 `/console/config` 改，而不是继续改 `.env`

- “交易所密钥是不是还要写 `.env`？”
  新流程下不需要，直接走 WebUI 表单即可

- “密码为什么不能在配置页里改？”
  因为它属于启动级和安全级配置，当前仍由环境变量控制
