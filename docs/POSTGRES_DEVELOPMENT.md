# PostgreSQL 开发与测试

运行数据库为 PostgreSQL 17，驱动 Psycopg 3；应用读写使用原生 `%s` SQL，不在运行期翻译 SQLite SQL。`DATABASE_URL` 必填，连接失败不切回文件数据库。Web 与 scheduler 各有进程内连接池；聊天检查点单独使用 `checkpoints` schema 与连接池。

本地启动数据库示例（以下密码只用于隔离开发）：

```bash
docker run -d --name crypto-agent-pg-dev \
  -e POSTGRES_USER=crypto_dev -e POSTGRES_PASSWORD=local-development-only \
  -e POSTGRES_DB=crypto_dev -p 127.0.0.1:55438:5432 \
  -v crypto_agent_pg_dev:/var/lib/postgresql/data postgres:17-alpine
```

本地 `.env` 设置 `DATABASE_URL=postgresql://crypto_dev:local-development-only@127.0.0.1:55438/crypto_dev`，然后执行：

```bash
uv sync
uv run python -m backend.storage.migrate
uv run python -m backend.storage.migrate --check
uv run python -m backend.app
```

正常应用连接使用 `public` 业务 schema。启动只核对版本；缺版本或校验和不一致必须先处理部署迁移，不允许临时启动修改表。每个迁移文件事务性提交，校验和固定，已应用文件不能改写；新增结构追加编号迁移。聊天迁移单独记录阶段，失败不能报总体成功。详见 [表结构与迁移](POSTGRES_SCHEMA.md)。

`get_connection()` 默认显式提交：没有 `commit()` 的上下文退出会回滚，包括正常退出；错误事务不会归还为可用脏连接。不捕获数据库失败当作“空结果”。`storage.record(row)` 去掉仅供内部扫描的 `storage_id`，原业务 ID 和 JSON/text API 格式保持。范围锁与扫描 revision 见 [并发说明](POSTGRES_CONCURRENCY.md)。

数据库已有配置但主密钥解密失败时，启动/重载明确报错，不使用旧 `.env` 任务替代。应恢复该数据库对应的 `CONFIG_MASTER_KEY`。清理预览同时核对记录身份和行版本，备份期间内容被改写也须重新预览。

## 隔离测试

不要使用运行库或生产连接。测试只接受显式 `TEST_POSTGRES_URL`，库名必须含 `test`，全局 fixture 禁止 `.env` 加载并清除继承的 `DATABASE_URL`。普通测试建立随机 schema，迁移/聊天/恢复测试建立随机数据库，结束只删除其记录的确切名称；需要测试用户有 CREATEDB 权限。

```bash
docker run -d --name crypto-pg-upgrade-test --label crypto-agent.task=upgrade-postgresql-storage \
  --memory 512m -e POSTGRES_USER=crypto_test -e POSTGRES_PASSWORD=isolated-test-only \
  -e POSTGRES_DB=crypto_test -p 127.0.0.1:55439:5432 postgres:17-alpine
export TEST_POSTGRES_URL=postgresql://crypto_test:isolated-test-only@127.0.0.1:55439/crypto_test
uv run pytest tests -q
```

PowerShell 设置变量：

```powershell
$env:TEST_POSTGRES_URL='postgresql://crypto_test:isolated-test-only@127.0.0.1:55439/crypto_test'
uv run pytest tests -q
```

测试中模型和交易所使用离线替身，不调用真实交易。dump/restore 用本机 PostgreSQL 17 客户端，缺少时使用固定 PG17 Docker 客户端；Docker 不可用时真实恢复测试不会被当作通过。Linux 更新脚本的失败边界测试使用假的 Docker 命令，可单独运行：

```bash
uv run pytest tests/test_postgres_update.py tests/test_postgres_backup.py -q
```

先核对容器 label，再清理这个测试容器；不执行全局 prune，也不删除生产卷：

```bash
docker inspect crypto-pg-upgrade-test --format '{{index .Config.Labels "crypto-agent.task"}}'
# 以上必须输出 upgrade-postgresql-storage
docker rm -fv crypto-pg-upgrade-test
```

`crypto-agent-pg-dev` 和 `crypto_agent_pg_dev` 是单独的开发数据，不属于测试自动清理范围。失败留下的随机 schema/数据库、容器或卷需按验收记录中的确切名称确认后清理。

## SQLite 引用归属

- `backend/app/services/sqlite_analysis.py` 与数据库上传 API 的 `sqlite3.Error`：只读分析上传副本，不打开运行库。
- `backend/utils/sqlite_blob.py`：为保留现有模块调用名称而保留文件名；内部使用 PostgreSQL `octet_length`、`convert_to`、`substring`，无 SQLite 驱动。
- SQL 的 `storage_id AS rowid`、`plan_rowid` 与分页响应里的 `rowid`：保留既有内部接口/游标字段名，真实值来自显式稳定身份，不依赖 PostgreSQL 物理行位置。
- `DB_NAME` / `TRADING_DB_PATH` / `CHAT_CHECKPOINT_DB` 的历史测试和文档引用：不是运行库选择入口。旧 SQLite 升级/导出测试逐项改成 PostgreSQL 基线/备份契约；历史运维记录原样留存。
- `storage_paths.data_file()`：非数据库文件的旧路径兼容帮助函数；运行数据库与聊天不调用它生成 SQLite 文件。

## 配置与聊天

旧 `version=1` 配置包继续由 `/api/config/full-import` 导入；使用 `write_env=false`，目标数据库地址和主密钥由部署 `.env` 提供。旧包导入保留业务 ID 和关系，不生成历史账本。Prompt 保存到共享 `DATA_DIR` 下目录，镜像内默认模板不覆盖用户自定义文件。计价或文件写入发生在数据库提交之后，失败信息明确该阶段，不能把已提交数据库描述为全部回滚。

LangGraph 使用 `langgraph-checkpoint-postgres`。模块导入不连接数据库，首次聊天才编译/借用检查点池；服务关闭时清理池，重新启动读取已有检查点。线程删除只删除指定 thread_id 对应的 checkpoints、blobs 和 writes。不再创建本地聊天 `.db`。
