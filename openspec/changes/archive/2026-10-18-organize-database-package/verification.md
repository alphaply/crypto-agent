# Verification

日期：2026-10-18（Asia/Shanghai）。本次目录整理的实现与验证已完成，未发现由本次修改新增的回归；既有测试失败及验证范围如下。构建通过不代表 Git/Docker 已发布。

## 已完成的代码与文档检查

- 原 `backend/database.py` 与 18 个 `backend/database_*.py` 共 19 个文件已迁入 `backend/database/`。新目录包含 `__init__.py`、18 个业务子模块及单独的 `__main__.py`。
- 原公共导入 `backend.database` 保持不变；原模块运行 guard 移入 `__main__.py`，继续支持 `python -m backend.database`。
- 98 个 Python 文件的内部引用随路径迁移更新；`rg -n 'backend\.database_|backend/database_' backend tests scripts -g '*.py'` 没有发现旧路径引用。
- 独立审查对比 114 个 Python 文件的语法树：除模块引用、包说明文档字符串和入口 guard 的位置外，结构完全一致；旧 guard 与新 `__main__.py` guard 的操作一致。SQL、函数签名及业务实现保持不变。
- `README.md`、`AGENTS.md` 已说明数据库访问包与 PostgreSQL 存储基础设施的职责；`docs/POSTGRES_SCHEMA.md` 中 45 处当前模块路径已与实际文件核对。历史归档、历史崩溃栈和运行数据保留原样。

## 已完成的执行检查

| 检查 | 结果 |
| --- | --- |
| 新增 `tests/test_database_package_imports.py` 导入与 CLI 入口回归 | 6 passed |
| 明确以 `tests/` 为范围的 pytest collect-only | 2649 tests collected，0 collection errors |
| `npm run build --prefix frontend` | 通过；保留原有单个产物大于 500 kB 的构建提示 |
| `openspec validate organize-database-package --strict` | 通过；`skip_specs: true`，无需增量业务规范 |
| `openspec validate --specs --strict` | 21/21 主规范通过 |
| 全部变更 `git diff --check` | 通过 |

最初未指定 `tests/` 范围的 pytest 收集将 `data/` 下历史源码备份也当作测试，产生 409 个收集错误。改为明确收集当前 `tests/` 后成功；没有删除历史文件，也未将失败的首次收集计为通过。

## 隔离回归与基线复现

本次验收目标是确认目录整理没有引入新的回归。测试并非全部通过；以下失败在整理前的干净基线 `24237c0` 中也能复现，因此保留现有失败证据，不通过修改旧业务或旧测试使结果看似通过。

| 检查范围 | 结果 | 说明 |
| --- | --- | --- |
| 88 个相关模块的广泛隔离回归 | 165 passed、2 skipped、5 failed，139.41 秒 | 达到预设 5 个失败上限后停止，未执行完全部模块 |
| 干净基线 `24237c0` 的 `test_exclusive_config_lifecycle.py` | 7 passed、14 failed、1 error，34.43 秒 | 包含上述全部 5 个失败节点；旧独占管理预期与既有共享管理实现不符，另有测试清理时未释放连接的错误 |
| 20 个模块的专项隔离回归（实际 191 项） | 187 passed、4 failed，203.01 秒 | 4 个失败节点均与下行的干净基线复现结果相同；未发现本次新增失败 |
| 干净基线 `24237c0` 的 `test_postgres_stores.py` | 12 passed、4 failed，12.33 秒 | 与当前专项中的 4 个失败节点一致，均为 `UndefinedTable: agent_run_queue` |

广泛回归与专项回归的用例有重叠，不能把 165 和 187 个通过项相加表示独立覆盖数；前述 6 个导入/CLI 测试也已包含在专项的 187 个通过项中。专项实际执行 191 项，先前预计的 193 项不是最终数量。当前测试集并非全绿，也未执行完成全部 2649 项。

`test_postgres_stores.py` 的自定义 fixture 仅安装 `0001` 结构，而基线配置保存逻辑已依赖 `0010` 的队列表，因而出现缺表错误。该原因存在于本次目录整理之前，本次未调整迁移文件或配置保存逻辑。

本地原始日志保留在 `data/database-package-verification/`（不随代码提交）：

- `collection-tests.log`：当前 `tests/` 的 2649 项收集结果。
- `regression.log`：88 模块回归及停止时的 5 个失败节点。
- `baseline-exclusive.log`：干净基线的独占管理测试完整结果。
- `focused.log`：20 模块、191 项专项回归最终结果。
- `focused-modules.txt`：本次专项实际使用的 20 个模块清单。
- `baseline-stores.log`：干净基线的 PostgreSQL stores 复现结果。

在按 `docs/POSTGRES_DEVELOPMENT.md` 准备一次性 PostgreSQL 测试库，并显式设置 `TEST_POSTGRES_URL` 后，可在 PowerShell 重跑同一专项：

```powershell
$databasePackageModules = @(Get-Content data/database-package-verification/focused-modules.txt | Where-Object { $_.Trim() })
uv run pytest -q @databasePackageModules
```

## 验证结论与后续发布

- 迁移及引用检查、导入/CLI 测试、隔离回归与干净基线对照均已完成；对照结果与业务代码语法树一致性审查共同支持本次只调整代码组织、没有新增业务回归的结论。
- 前端构建、全部差异检查、变更与主规范严格校验通过；已如实保留既有失败，不将它们计为通过，不改动业务或测试来消除这些历史失败。
- 已核对 5/5 任务并归档到 `2026-10-18-organize-database-package`。Git/Docker 发布将在本记录提交后执行，最终状态、commit、标签和 digest 以发布脚本输出及交付回复为准。
