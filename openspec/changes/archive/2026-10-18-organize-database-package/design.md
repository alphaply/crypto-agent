# Design

## Context

动机见 `proposal.md`。当前 `backend/database.py` 汇总连接入口、存储对象和公共函数，18 个 `backend/database_*.py` 承担独立数据访问职责。多数调用方使用 `backend.database`，另有调用方、测试与工具直接引用子模块。底层 PostgreSQL 连接与版本迁移已位于 `backend/storage/`，数据目录由 `backend/storage_paths.py` 明确管理。

## Goals / Non-Goals

**Goals:**

- 让数据库访问代码在一个目录中可查找，并保持各模块现有职责。
- 保留 `backend.database` 作为原有调用入口，模块内部及仓库调用方统一引用新的子模块位置。
- 通过导入检查与现有隔离数据库回归验证重构没有改变业务行为。

**Non-Goals:**

- 不合并或拆分已有 SQL、类、函数和事务，不引入 repository/ORM 等新抽象。
- 不调整 `backend/storage/`、`backend/config_store.py` 等其他持久化模块的职责，不新增数据迁移或存储配置。
- 不迁移、清空、导入运行数据，不更新生产容器。

## Decisions

### 1. 保留公共入口的包化整理

原 `backend/database.py` 移至 `backend/database/__init__.py`；`database_<name>.py` 对应移至 `backend/database/<name>.py`。因此 `from backend.database import get_db_conn` 等公共导入保持原样。包内引用使用新的子模块路径，原函数实现与初始化顺序保持不变。

原文件末尾的模块运行入口迁至 `backend/database/__main__.py`，使 `python -m backend.database` 继续执行既有初始化行为；普通导入仍不执行该入口。

继续使用顶层 `database.py` 加旁边另一名称的目录会把数据库代码留在两个位置；本次选择单一 `backend/database/` 包。此次不额外拆小公共入口，避免把目录整理扩展成业务重写。

### 2. 库内子模块引用随迁移更新

搜索 Python 导入、动态导入、测试替换目标以及脚本中的路径引用，并更新到新位置。无已知外部调用方需要旧 `backend.database_*` 路径，因此不默认添加顶层兼容文件或 `sys.modules` 别名；迁移前审计若发现真实依赖则再评估。历史归档和历史审计报告保留原始记录。

### 3. 与底层存储保持边界

`backend/database/` 继续承载业务数据访问，`backend/storage/` 继续负责 PostgreSQL 连接和版本化迁移；不为文件迁移改变实际数据位置、查询语义或错误处理。`backend.database` 中已有公开名称和兼容常量保持原含义。

## Risks / Trade-offs

- [遗漏直接导入或测试替换字符串] → 迁移前后全仓检索旧路径，运行新包导入检查与受影响的现有回归测试。
- [包入口与子模块互相导入导致循环] → 保留原有导入时机和顺序，验证公共入口及直接子模块导入，不新增导入时数据库连接。
- [测试误用运行库] → 遵循现有 `TEST_POSTGRES_URL` 隔离约束，使用一次性测试环境，交易所和模型调用使用离线替身。
- [纯移动中意外修改 SQL 或行为] → 对照迁移前内容审查差异，仅允许路径引用及必要的包描述改动。

## Migration Plan

1. 迁移文件并更新引用及项目结构文档。
2. 核对旧路径引用、公共导入和 Python 语法，完成相关隔离回归及前端构建。
3. 任务全部完成后校验 OpenSpec，保留 `skip_specs: true` 并归档；本次没有需要同步的增量业务规范。
4. 按仓库既有流程提交到 `beta`，运行发布脚本重建前端与 Docker、执行隔离烟测并推送 Git 和镜像，记录提交与镜像 digest。

本次无数据结构变更，代码回退可通过后续反向提交恢复原目录；镜像发布失败按现有流程报告具体阶段。生产更新需单独执行。
