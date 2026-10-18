# Proposal

## Why

数据库访问代码分散在 `backend/` 顶层的 `database.py` 与 18 个 `database_*.py` 文件中，查找相关代码和辨认模块职责不够直观。用户已确认要整理这些 Python 代码文件，并在验证后推送 Git 与 Docker。

## What Changes

- 将数据库访问代码集中到 `backend/database/` 包，按现有职责保留 `balance.py`、`chat.py`、`orders.py` 等模块。
- 将原 `backend/database.py` 迁至包入口 `backend/database/__init__.py`，保持既有 `backend.database` 导入、函数签名与行为。
- 更新仓库中实际执行的子模块导入、测试引用和项目结构说明，删除迁移后的顶层重复文件。
- 通过现有隔离测试与构建验证后，按仓库现有流程归档、提交到 `beta` 并发布 Git/Docker。

本次只调整代码位置和引用；不迁移实际数据库，不改 SQL、业务逻辑、表结构、数据库连接配置或数据保存位置。

## Capabilities

### New Capabilities

无。本次为代码组织重构，`.openspec.yaml` 设置 `skip_specs: true`。

### Modified Capabilities

无。`postgresql-storage` 等现有规范的行为约定保持不变。

## Impact

- 数据库代码：`backend/database.py`、`backend/database_*.py` 迁入 `backend/database/`。
- 调用方：相关后端模块、脚本和测试的内部子模块导入路径更新。
- 文档：`README.md`、`AGENTS.md` 的项目结构说明和 `docs/POSTGRES_SCHEMA.md` 的当前模块路径更新。
- 不新增依赖、数据库迁移或 API 字段；镜像发布遵循既有归档发布规范，生产容器更新仍为单独动作。
