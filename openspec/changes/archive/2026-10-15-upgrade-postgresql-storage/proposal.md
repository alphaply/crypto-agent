# Proposal

## Why

当前 Web、MCP 和 scheduler 共享 SQLite，持久化实现散布于交易、配置、统计和聊天检查点路径。用户决定升级为容器化 PostgreSQL，并从现有配置 JSON 导入重新开始，不迁移历史数据库；需要完整替换存储依赖并给出可操作的服务器更新流程。

## What Changes

- **BREAKING**：生产运行存储切换至 PostgreSQL，包括配置、交易/Paper/MCP 状态、记忆和聊天检查点；不提供 SQLite 自动降级或双写。
- 新库从空白初始化；兼容现有 version=1 配置包和 `/api/config/full-import`，恢复原接口支持的配置、密钥、Prompt、计价及 MCP 设置。历史交易、记忆、聊天及执行责任不导入，不自动删除旧 SQLite 文件。
- 使用显式版本迁移和统一连接管理，替换 SQLite SQL、隐式 rowid、触发器、BLOB 读取及事务语义；保留现有查询范围、交易防重和提交结果含义。
- 提供 Web、scheduler、PostgreSQL 的 Compose 部署及首次切换、备份恢复、日常更新操作文档；区分镜像 push、服务器 pull 和容器重建。
- 改造当前数据库诊断、备份、维护及发布烟测以使用 PostgreSQL，保留现有 SQLite 上传副本的只读分析功能。
- 本变更不处理正在开发的校验/同步精简，不迁移历史数据，不新增 MySQL、在线迁移、双写或跨交易所查询。用户已批准实施；归档、发布和服务器更新仍分别按后续请求执行。

## Capabilities

### New Capabilities

- `postgresql-storage`: PostgreSQL 运行存储、配置包兼容、持久化语义与容器运维。

### Modified Capabilities

无。既有交易、MCP 预算和容量要求继续适用；本变更替换其存储实现，不放宽规则。

## Impact

- `backend/database*.py`、`backend/config_store.py`、`backend/agent/chat_graph.py`；各直接使用 SQLite 的服务及 MCP/Paper 存储调用者。
- `backend/utils/execution_ledger.py`、`execution_plan_index.py`、`protection_scan.py`、`sqlite_blob.py` 等依赖事务、行身份及分段读取的模块。
- 数据库维护/导出 API、前端数据库维护展示、配置导入导出与 Prompt 文件持久化。
- `pyproject.toml`、`uv.lock`、Dockerfile、Compose、`.env.template`、发布脚本、README 与部署指南；新增 PostgreSQL 集成测试。
- 与 `simplify-memory-and-ordered-execution` 存在文件重叠，实施按其届时确认的接口集成，不覆盖工作区已有开发。
