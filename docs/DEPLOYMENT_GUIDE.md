# PostgreSQL 部署步骤

`docker push` 只是把新程序上传到仓库。服务器还要拉取新镜像、备份数据库、执行迁移、重建应用容器。数据库容器和数据卷继续保留。

本轮实际构建、配置导入、更新和恢复的范围及测试结果见 [PostgreSQL 验收记录](POSTGRES_ACCEPTANCE.md)。

2026-10-15 已按用户明确跳过隔离测试的要求部署到指定服务器。该实例已配置应用 `latest`，以后直接执行 `sudo /opt/crypto-agent-pg/update.sh` 更新，无需修改标签；配置、备份位置和未完成的稳定性验收见 [实际部署记录](POSTGRES_DEPLOYMENT_20261015.md)。

本次从 SQLite 切换采用**空白 PostgreSQL + 导入配置包**。不迁移历史交易、聊天、记忆、Paper 余额和 OAuth 授权；旧 SQLite 和原密钥留存。配置包中的任务 ID、模型、交易所、Prompt 与配置关系保留。

## 首次切换

1. 在旧系统配置页导出 version=1 全量配置包；需要搬迁 API Key 时选择包含密钥，私密保存 JSON。导出不包括真实交易所持仓的管理责任。
2. 停止旧 Web 和 scheduler，确认没有其他副本执行策略。保存旧镜像 digest、旧 `.env`、原数据卷和 SQLite。已有真实持仓/挂单需先明确其继续维护方式；空数据库不保留旧操作与管理账本。暂停期间应用内保护维护也停止。
3. 服务器新建目录，例如 `/opt/crypto-agent-pg`。放入同一已验证版本的 `compose.yaml`、`.env.template`、`scripts/backup-postgresql.sh` 和 `scripts/update-postgresql.sh`。复制模板为 `.env` 并私密保存：

   ```bash
   cd /opt/crypto-agent-pg
   cp .env.template .env
   chmod 600 .env
   ```

4. 编辑 `.env`：设置强 `ADMIN_PASSWORD`、`JWT_SECRET`、稳定的 `CONFIG_MASTER_KEY`；设置 `POSTGRES_USER`、`POSTGRES_DB`、`POSTGRES_PASSWORD` 和匹配的 `DATABASE_URL`。Compose 内数据库主机是 `db`。密码在 `POSTGRES_PASSWORD` 用原文，在 URL 中使用百分号编码；含 `$` 的原文在 `.env` 用单引号包住。不要在聊天、日志或命令参数中打印 URL。目标环境独立设置，不从旧包覆盖。
5. 当前 Compose 不设置 CPU、内存或 swap 配额，不再需要填写此前九个资源值。`APP_IMAGE` 建议使用发布的不可变标签或 digest。首次保持 `DEPLOYMENT_MAINTENANCE=true`，Compose 固定 Web 内调度关闭。
6. 启动数据库、运行一次迁移、只启动 Web：

   ```bash
   docker compose config --quiet
   docker compose up -d --wait db
   docker compose --profile tools run --rm migrate
   docker compose up -d web
   docker compose logs --tail 80 web
   curl --fail http://127.0.0.1:31421/health
   ```

   `/health` 必须有 `"success": true`。迁移失败停止后续步骤，修复后重跑迁移；不要绕过结构检查。Docker 镜像启动 Web/scheduler 前会先执行待应用迁移，迁移失败则不启动服务；使用现有数据库锁串行执行。仍建议通过更新脚本先停应用、备份并显式迁移，便于在升级前保存恢复点。应用运行期只检查结构。`scheduler` 是显式 profile，以上命令不会启动它。部署暂停也阻止交易工具和 Paper 后台维护，不修改导入任务的 enabled 值。
7. 登录 `/console/config`，导入旧配置 JSON，**关闭“写入 .env”**（API 为 `write_env=false`）。旧包的明文密钥会使用目标 `CONFIG_MASTER_KEY` 重新加密；无密钥包需补填 Key。检查任务 ID、模型/交易所引用、自定义 Prompt、计价和 MCP `public_url`。导入不会创建历史成交。
8. 保持暂停，重启 Web，让 OAuth 发行地址和内存配置使用导入结果：

   ```bash
   docker compose restart web
   curl --fail http://127.0.0.1:31421/health
   curl --fail https://YOUR_DOMAIN/.well-known/oauth-authorization-server/oauth
   curl --fail https://YOUR_DOMAIN/.well-known/oauth-protected-resource/mcp
   ```

   检查返回域名，重新连接首次切换后的 OAuth 客户端；旧授权未迁移。只读核对账户权限、行情和 Key 解密，不用真实下单测试。
9. 核对完成后，将 `.env` 的 `DEPLOYMENT_MAINTENANCE=false`，重新创建 Web，再启动唯一 scheduler：

   ```bash
   docker compose up -d --no-deps --force-recreate web
   # 等待 /health success=true 后再执行：
   docker compose --profile scheduler up -d --no-deps scheduler
   docker compose ps
   ```

数据库保存在 `crypto_agent_pg17_data`，自定义 Prompt 等文件保存在 `crypto_agent_pg_app_data`。Web/scheduler 必须共享同一应用卷，其中 `.management-locks` 是同宿主 OS 金融锁目录。不能拆分锁目录，也不能运行跨宿主应用副本。旧 `crypto_agent_data` 卷与 SQLite 不删除。

## 日常更新：push 后服务器做什么

2026-10-15 工具调用链路调整包含迁移 `0005` 和 `0006`，下方更新脚本会执行迁移。要同时解除原有容器配额，需先移除服务器 `compose.yaml` 的 `cpus`、`mem_limit`、`memswap_limit` 配置，参考仓库同名文件，其余端口、卷名、环境和密钥沿用实际部署值。脚本重建 Web/scheduler 后其新资源配置生效；脚本不重建数据库，DB 的旧配额需另行安排重建或运行参数更新。仅更新镜像无法改容器配额。本次发布不自动操作生产容器。

已有 PostgreSQL 实例只需在部署目录执行：

```bash
cd /opt/crypto-agent-pg
# 若使用不可变标签，先修改 .env 的 APP_IMAGE 为本次发布的 git-<sha>
bash scripts/update-postgresql.sh
docker compose logs --tail 80 web scheduler
```

脚本拉取三个应用服务的同一镜像，停止 Web/scheduler，保存备份，执行迁移，重建 Web 并检查健康，再启动 scheduler。它不拉取或重建 db，不做 PG 大版本升级。失败会报告阶段，应用保持停止；数据库和备份不会被自动覆盖。完成后仍需核对公网 HTTPS、OAuth 和只读业务接口。

脚本对应的手动步骤：

```bash
docker compose --profile scheduler --profile tools pull web scheduler migrate
docker compose --profile scheduler stop web scheduler
bash scripts/backup-postgresql.sh
docker compose --profile tools run --rm --no-deps migrate
docker compose up -d --no-deps web
curl --fail http://127.0.0.1:31421/health
# 确认 JSON success=true；失败就停在这里
docker compose --profile scheduler up -d --no-deps scheduler
```

不要执行 `docker compose down -v`；它会删除数据库卷。不要在每天更新应用时更换 PostgreSQL 主版本或 `POSTGRES_PASSWORD`，初始化后的 PG 密码不会因修改环境变量自动更新。

## 备份和恢复

备份脚本要求 Web/scheduler 已停止，以得到相互匹配的数据库和文件：

```bash
docker compose --profile scheduler stop web scheduler
bash scripts/backup-postgresql.sh
```

`backups/<时间>/COMPLETE` 表示全部完成。目录含 `database.dump`（PostgreSQL custom 格式）、`application-data.tar.gz`（Prompt 等文件）、`environment.env` 和镜像记录。数据库 dump 自带配置、业务表与 `checkpoints` schema，但不包含 `.env`、Prompt 文件或 Docker 卷配置。备份目录权限默认私密，仍须复制到独立可靠存储。

Web 的数据库导出仍要求登录或 120 秒下载 ticket，下载 `.dump`，不是 SQLite。在线 dump 是数据库一致快照；要恢复完整部署仍需匹配文件和密钥。Docker 镜像包含 PostgreSQL 17 的 `pg_dump`/`pg_restore`。

先在**新建的隔离 PostgreSQL 数据库/卷**恢复验证，保持 Web/scheduler 停止；以下 `restored_database` 必须是新空库，不能填生产当前库：

```bash
# 与新空目标 PG 容器连接，用户名按其独立设置替换
docker exec -i RESTORE_DB_CONTAINER pg_restore --exit-on-error --no-owner --no-privileges \
  -U RESTORE_USER -d restored_database < backups/TIMESTAMP/database.dump
```

把归档文件恢复到新应用卷，再使用匹配的 `environment.env` 中 `CONFIG_MASTER_KEY`，改 `DATABASE_URL` 指向恢复库。先执行迁移 `--check`、健康和只读核对，确认版本/配置/聊天/账本均完整。恢复操作不得覆盖更新后已产生的新成交；失败优先修复当前版本。旧 SQLite 程序不能直接读取 PostgreSQL，第一次切换不能靠回滚应用镜像自动还原旧系统。需要恢复旧 SQLite 时旧新系统都须停止并核对期间交易责任，不能让两个 scheduler 同时管理同一账户。

数据库维护页使用 PG 表大小与记录数；SQLite 空闲页/WAL 指标显示“不适用”。“回收空间”执行普通 `VACUUM (ANALYZE)`，使空间可复用，不保证磁盘文件变小，不运行阻塞式 `VACUUM FULL`。上传 SQLite 副本仅做只读分析，不能用它替换运行数据库。

## 故障定位

```bash
docker compose ps
docker compose logs --tail 100 db web scheduler
docker compose stats --no-stream
curl --max-time 10 http://127.0.0.1:31421/health
```

数据库断连、迁移失败和锁超时都不能当作“没有数据”。记录失败阶段与脱敏日志，检查数据库健康、连接池和原交易身份；不重放未确认交易。详见 [并发说明](POSTGRES_CONCURRENCY.md)、[资源诊断](RESOURCE_DIAGNOSTICS.md) 和 [MCP 分层排障](MCP.md#mcp-分层排障)。


## OpenSpec 归档后的自动发版

### v3 交易数据与兼容回滚

升级 v3 前停止旧 worker，保留数据库 dump、应用卷与原交易证据；部署期执行版本迁移，运行期只核对结构，不重算旧 fingerprint 或补发批次尾项。新请求停止后，当前运行时继续核验已尝试项。完整保留 `visible_management_*`、`management_*`（包括 retirement 与 recovery grants）、`unified_trade_batches`、`unified_trade_children`、`unified_native_intents`、`paper_batches`、`paper_batch_children`、`spot_management_intents`、`spot_budget_reservations` 及原交易/MCP/订单账本。

拟回滚时，对一致性备份运行 `uv run python scripts/check-trading-compatibility.py --database-url-env RESTORED_DATABASE_URL --supported-contract-version 2`。只读预检发现持久 owner、活跃 v3 worker、未决原操作、原生客户端身份或接受的恢复责任时返回 `blocked` 和退出码 2；不能让旧运行时管理这些数据。当前支持 v3 的运行时传版本 3。预检不清理资源、不释放 owner、不下单；通过只表示已检查的新契约阻塞项不存在，还须遵守上文既有 v2/all 与条件单的回滚要求。不得覆盖代码或旧备份来消除未决证据。

本项目将 archive 请求视为完整交付：先同步主规范、核对任务与验收结果，再归档并发版。用户明确要求“仅归档”时按该次范围执行。持久规则记录在 `AGENTS.md`、`openspec/config.yaml` 和 `openspec/specs/archive-release-workflow/spec.md`；归档技能读取这些规则后执行下列流程。

1. 将选定变更的增量规范合并到主规范，保留未修改要求与场景，运行 `openspec validate --specs --strict` 和该变更的严格校验。
2. 将已完成变更移动到 `openspec/changes/archive/YYYY-MM-DD-<change-name>`，保留验收记录和元数据；已有目标不得覆盖。
3. 仅暂存本次实现、测试、文档、主规范和归档，在 `beta` 提交。无关工作（包括未跟踪的本地 `.agents/`）保留，不能直接用 `git add .` 混入。
4. 在仓库根目录运行发布入口，归档名替换为实际目录名：

   ```powershell
   powershell -NoProfile -File scripts/release-archived-change.ps1 -ArchiveName 2026-10-13-refine-decision-state-context
   ```

该入口检查已提交的归档和干净的镜像输入，重新安装锁定的前端依赖、构建前端，再通过 `Dockerfile.prebuilt` 构建带 Git revision 的镜像。烟测使用一次性隔离 PostgreSQL 网络/卷与应用容器、关闭调度器、不挂载生产数据，检查 `/health` 与前端入口后自动清理。成功后推送 `origin/beta`，依次发布 `alphaply712/crypto-agent:git-<short-sha>` 和 `:latest`，输出 commit 与镜像 digest。

任一步失败都停止后续步骤，并报告已推送的阶段。修复后可对同一归档重新运行入口；不能将“目录已归档”当成“发布成功”。该流程由归档 Agent 按项目规则调用，直接运行裸 `openspec archive` 命令不会触发 Git 或 Docker 钩子。生产容器更新继续使用上面的部署流程，在用户请求部署时执行。

三容器 PostgreSQL profile、预先声明的校准/acceptance、memory+swap 总量与安全回退见 [容量验证](CAPACITY_VERIFICATION.md)。目标主机需分别设置预期memory/swap/CPU环境并核验实际cgroup；本地 Docker Desktop 报告不代表生产验收。

## 用户指定的无容量门槛主机测试

当用户明确要求测试整台主机的实际承载能力时，Web 和 scheduler 同时设置 `RUNTIME_CAPACITY_ENABLED=false` 并重建。这会关闭应用资源准入门槛，保留真实资源采集和交易正确性校验；具体范围见 [运行容量](RUNTIME_CAPACITY.md#explicit-observation-mode)。原有数值预算可以保留作为参考，诊断会标记 `observe_only`，不能称它们仍在限额。

Docker Compose 同时移除这两个服务的 `mem_limit`、`memswap_limit`、`cpus` 和 `deploy.resources.limits`；清除旧的 `RESOURCE_EXPECTED_MEMORY_BYTES`、`RESOURCE_EXPECTED_MEMORY_SWAP_TOTAL_BYTES`、`RESOURCE_EXPECTED_CPU_CPUS` 及 `CAPACITY_PROFILE_SHA256` 环境声明。不使用巨大哨兵数值冒充无限额。保留原凭据、数据卷、端口、账户锁、请求超时和唯一 scheduler。通过 `docker inspect` 和进程内 cgroup 读取确认配额确实未设置。

这种测试不再以固定1GiB容器或RSS平台阈值作为部署门槛。旧的失败/中断验收报告原样保留；发布仍须完成回归、前端构建、隔离空数据烟测、Git提交推送及带revision的不可变镜像发布。生产验收只调用账户、订单及行情等只读工具，记录并发量、耗时、进程RSS峰值、主机可用内存、swap和OOM证据，结论限定于实际验证的负载。
