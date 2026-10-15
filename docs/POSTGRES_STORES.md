# PostgreSQL 存储开发说明

运行数据库只使用 `DATABASE_URL`。`backend.database.get_db_conn(timeout=30)` 保留调用方的显式提交约定，连接归还前回滚尚未提交的内容；MCP 请求的剩余等待预算也会传给连接池。配置仓库 `_get_conn()` 保留原先“成功退出时提交”的约定。

## SQL 与结构

- `database*.py`、`config_store.py`、统计、看板、新闻、计价与记忆服务使用原生 Psycopg `%s` / `%(name)s` 参数绑定，没有 SQL 翻译层。
- 启动时 `database_schema.initialize_schema()` 调用版本检查。表、字段、索引由 `backend/storage/migrations/` 安装；原来的 `initialize_*` / `ensure_*` 方法保留调用接口，但不再执行 DDL 或从旧 SQLite 导入数据。
- 批量参数写入使用 `conn.cursor().executemany()`，不依赖 SQLite 的连接级批量方法。
- 自增记录使用 `INSERT ... RETURNING id`。需要稳定内部身份的表使用 `storage_id`；它不代表提交先后顺序。公共返回值使用 `storage.record()` 排除该字段，接口仍保留原业务 ID。复制记录形成新业务记录时必须省略 `storage_id`，例如模拟条件退出单的撤旧建新。
- 冲突处理使用明确键的 `ON CONFLICT`。新闻计分缓存更新保留原内部身份；交易观察按标的、成交 ID、原配置归属键去重。单个坏交易输入通过保存点隔离，数据库故障仍明确失败。
- `storage.table_names()` / `table_columns()` 读取 PostgreSQL catalog。列元数据仍可按名称或位置读取；没有模拟 `sqlite_master` 或 `PRAGMA`。
- 文本 JSON 保留原字节编码语义，浮点采用 DOUBLE PRECISION，时间戳和计数采用 BIGINT。历史文本时间的日分组使用日期部分，不把原先北京时间文本当 UTC 二次偏移。可空时间的降序查询显式使用 `NULLS LAST`。
- 原 SQL 中 `LIKE` 的百分号若与绑定参数一起出现，使用 `%%`；空集合过滤使用 `ANY(%s)` 或既有空集合分支，不能生成 `IN ()`。

## 事务锁范围

所有下列锁都通过 `storage.lock_scope()` 获得 PostgreSQL 事务级 advisory lock，提交、回滚或断开会释放，不建立运行期全库写锁。

| 写入单元 | 锁键 | 原因 |
| --- | --- | --- |
| 配置整包替换、首次环境引导 | `runtime-config` | 配置是一个完整快照，避免交错替换；延迟启动者不能覆盖已导入配置 |
| 模拟账户资金与独立交易 | `mock-account, config_id` | 一个配置的不同标的共用余额，必须共享锁 |
| 策略总结保存 | `summary, config_id` | 同一 run ID 的查询与插入只执行一次 |
| 短期记忆保存、修改 | `memory, config_id` | 版本计算、来源检查与写入保持同一事务 |
| 长期规则批量修改 | `trading-rules, config_id` | 修订号和操作 ID 保持原幂等语义 |
| 新闻/计价流水线租约 | `pipeline, name` | 同名流水线互斥，不同流水线独立推进 |
| 成交观察归属与去重 | `trade-observation, symbol, trade_id` | 无归属观察的认领和有归属插入不互相重复 |

模拟交易先取账户锁，再读取操作身份、账户、订单和仓位。规则、新闻、记忆与配置单元只取其自身范围锁。交易所请求不因存储失败而自动重放。

独立读取配置的连接使用事务级 `REPEATABLE READ READ ONLY`；一份运行快照不能混合配置替换前后的不同表。配置写入过程中读取候选快照使用当前连接，能看到未提交的密钥和引用，校验或 MCP hook 失败会回滚整个候选配置。

## version=1 配置导入

首次切换应在旧版导出配置，在新实例使用 `write_env=false` 导入。目标 `DATABASE_URL`、`CONFIG_MASTER_KEY`、管理员和 JWT 设置单独配置。配置包中的明文密钥使用目标主密钥重新加密，不需要旧 SQLite 或旧主密钥；脱敏包不会生成不存在的密钥。

导入阶段依次为：原子配置事务（包含 MCP hook 和候选校验）、共享 Prompt 文件、模型计价、可选环境文件、运行快照重载。配置校验失败时，不改写 Prompt。配置事务完成后的文件或计价写入失败会明确提示“配置数据库已提交”及失败阶段；这些外部写入不伪装成跨数据库/文件系统原子事务。

Prompt 导入、编辑与导出使用同一 `prompt_dir()`；容器部署使用共享应用数据卷的 `prompts` 目录。镜像默认文本只在新目录初始化时提供，不覆盖已有用户文本。

`write_env=true` 仍只写已有导出白名单中的字段，不接受包内 `DATABASE_URL` 替换当前连接。它可能写入旧 `CONFIG_MASTER_KEY`、`DATA_DIR`、`TRADING_DB_PATH` 等引导值，因此首次数据库切换使用 `false`。旧路径值不选择 PostgreSQL 运行存储。OAuth 授权状态不在配置包里，切换后需要重新授权；改变 MCP `public_url` 后需要重启 Web。

## 验证

测试必须显式设置 `TEST_POSTGRES_URL`，数据库名称包含 `test`。存储用例使用独立随机 schema；完整启动和 checkpoint 用例使用独立随机数据库并运行正式迁移，测试账号需要 `CREATEDB` 权限。禁用 dotenv 加载，清理时只删除自身 schema 或数据库。不能把生产 `DATABASE_URL` 当作测试连接。

```powershell
$env:TEST_POSTGRES_URL = 'postgresql://crypto_test:isolated-test-only@127.0.0.1:55439/crypto_test'
uv run --no-sync pytest tests/test_postgres_stores.py tests/test_config_store.py tests/test_config_write_atomicity.py -q
```

原有配置、存储、记忆、新闻、行情统计、资产组合与历史接口测试已改为每例独立的真实 PostgreSQL schema，使用生产迁移结构替代测试中的简化建表。配置回滚比较包括应用行、密文与修订元数据，不比较自增序列。PostgreSQL 序列分配本来就不会随事务回滚，不能据此认定发生业务写入。

`tests/test_postgres_stores.py` 覆盖配置包与目标密钥、脱敏包、校验/hook 回滚、文件失败阶段、并发快照、启动竞争、模拟资金与操作幂等、规则、新闻租约、记忆、会话、计价、大计数、成交去重及统计。模拟模型与交易所测试使用假服务，不发送真实交易。原有计费用例直接写入 `durable_usage`，并验证 `token_usage` 镜像行不会重复计费；不再依赖旧 SQLite 历史回填。

`tests/test_postgres_startup.py` 使用独立进程检查导入不连接数据库、不复制旧 SQLite 文件或创建 SQLite 路径；空数据库启动必须拒绝，并保持无表。完整 API 用例导入 version=1 的 Prompt、模型和交易所密钥，在实际 PostgreSQL checkpoint 上进行假模型聊天，验证后续重启保留聊天、模拟余额、挂单和模型计价。连续生命周期必须读取更新后的 MCP public URL，保持路由不重复，关闭时释放连接池。维护模式下，即使调度配置开启，也不启动 scheduler 或 Paper 后台任务。

## SQLite 残留分类

本模块范围内不再导入 `sqlite3` 或使用 SQLite SQL。`database.DB_NAME` 与 `config_store.DB_NAME` 仅保留为旧调用方/测试的路径符号，不再连接、探测或创建该文件；旧配置包环境字段仅为旧格式兼容。仓库中允许的 SQLite 实现只属于旧上传副本的只读分析、旧格式样本或尚待迁移并明确标出的测试。不得把这些路径接回运行数据库，也不能用旧 SQLite 测试通过代替 PostgreSQL 验证。
