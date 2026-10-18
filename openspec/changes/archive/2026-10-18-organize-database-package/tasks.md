# Tasks

## 1. 数据库代码集中整理

- [x] 1.1 将 `database.py` 和 18 个 `database_*.py` 迁入 `backend/database/`，保留公共入口和模块运行入口；核对文件清单、导入行为及迁移前后实现差异。
- [x] 1.2 更新后端、脚本及测试的内部子模块引用与替换目标；全仓检索确认实际执行代码不再引用旧子模块路径，并通过隔离回归与干净基线对照确认没有本次新增失败（既有失败及覆盖范围见 `verification.md`）。
- [x] 1.3 更新 `README.md`、`AGENTS.md` 的数据库代码结构说明和 `docs/POSTGRES_SCHEMA.md` 的当前模块路径，说明 `backend/database/` 与 `backend/storage/` 的职责；对照实际目录检查路径与描述。

## 2. 集成验证

- [x] 2.1 在隔离环境完成数据库相关回归和应用导入检查，记录执行命令及结果，确认未改 SQL、数据库配置、表结构和业务行为。
- [x] 2.2 完成前端构建、变更差异检查及 OpenSpec 严格校验，记录通过的检查项，确认归档前全部任务已完成。

## Workflow follow-up

- 任务验证完成后归档 `organize-database-package`；本次为 `skip_specs: true` 的纯重构，没有需要同步的增量规范，仍校验既有主规范。
- 仅提交本次代码、测试、文档和归档到 `beta`，保留无关本地文件。
- 执行 `scripts/release-archived-change.ps1` 完成前端与镜像重建、隔离烟测、Git 推送及 Docker 标签发布，记录 commit 和镜像 digest。
- 发布结果与生产部署分开报告；未另行请求时不更新生产容器。
