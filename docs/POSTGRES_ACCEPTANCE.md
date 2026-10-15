# PostgreSQL 升级验收记录

日期：2026-10-15。对应 `upgrade-postgresql-storage`。运行路径切换为 PostgreSQL 17，新库只导入 version=1 配置；不搬迁 SQLite 历史，不放宽交易规则。完整回归与最终镜像更新已通过；最终三容器容量验收已失败，当前为 **34/37 项完成**，7.6、8.2、8.3 仍未闭环。不能视为完整验收通过。用户随后明确要求“不需要隔离测试，直接部署”，已按这一授权发布 `latest` 并部署；实际步骤和边界见 [服务器部署记录](POSTGRES_DEPLOYMENT_20261015.md)。部署完成不把下述失败记录改为通过。

## 环境与依赖

- 本机 Windows + Docker Desktop Linux amd64；PostgreSQL 17.11。
- Psycopg 3.3.6，psycopg-pool 3.3.3，LangGraph 1.2.11，langgraph-checkpoint 4.3.0，langgraph-checkpoint-postgres 3.2.0，FastAPI 0.128.0；依赖由 `uv.lock` 锁定。
- 数据库镜像：`postgres:17-alpine@sha256:b0f9560a2de083e2cc7382e75f808c7381a32852a7ec49117deedb300e552b24`。
- 业务迁移 0001–0004，聊天使用独立 `checkpoints` schema。已有迁移文件校验和固定，后续变更追加新迁移。
- 下述回归与演练只使用隔离数据库、虚构密钥及模型/交易所替身，该阶段没有真实交易、Git push、Docker push 或生产更新。之后用户授权的镜像发布和生产部署单独记录。

## PostgreSQL 回归

完整运行命令（连接是明确创建的本地测试库）：

```powershell
$env:TEST_POSTGRES_URL='postgresql://crypto_test:isolated-test-only@127.0.0.1:55439/crypto_test'
$env:TEMP='E:\project\crypto-agent\data\test-temp'
$env:TMP=$env:TEMP
uv run pytest tests -q --tb=short --maxfail=10 --junitxml=data/test-temp/postgresql-full-final.xml
```

测试入口清除继承的运行库连接、禁止 dotenv 加载；普通用例使用随机 schema，完整启动、迁移、检查点和恢复用例使用随机数据库。所有清理按本轮创建的确切名称执行。没有使用 SQLite 测试代替 PostgreSQL 回归。

单进程完整运行结果：**2449 passed、2 skipped、26 subtests passed，1602.46 秒**。原始输出 `data/test-temp/postgresql-full-final.txt`，JUnit 为上述 XML。两项跳过分别是 Windows 没有真实 POSIX SIGUSR1、同一跨进程 PG reservation/文件锁已有 Binance scope 覆盖的重复 OKX 参数；没有因缺 PostgreSQL 而跳过。

运行期间审查追加的最小修复和新用例，随后以最终冻结代码独立复测以下六个完整模块：`test_database_maintenance.py`、`test_config_store.py`、`test_config_write_atomicity.py`、`test_postgres_deployment_pause.py`、`test_postgres_startup.py`、`test_postgres_update.py`。结果 **50 passed、9 subtests passed，99.97 秒**，日志及 JUnit 为 `data/test-temp/postgresql-review-fixes-final.*`。该批与全量批存在重叠，不把两次数字相加当作唯一测试数。

此前分模块回归发现并修复原生 SQL、事务与 fixture 接线问题；重叠批次的通过数不相加作为完整回归数字。故障记录原样保留在 `data/test-temp/`。覆盖内容包括：

- 显式提交/回滚、坏连接丢弃、连接池耗尽、锁与 statement timeout、独立范围并发推进。
- 配置包目标密钥重新加密、脱敏包、错误版本/引用/事务 hook 回滚、文件写入失败的真实阶段。
- 原操作 ID 幂等、资金预留、计划版本竞争、ACK 后实际 PG 连接终止、不自动重放金融回调。
- 较小 identity 迟提交、旧行更新/删除、来源 revision、投影原子回滚、未完成重建保留原完整历史。
- 字节分页、UTF-8 边界、大 payload、原始 JSON 文本、公开 DTO 不暴露内部 storage_id。
- 假模型聊天 stream、interrupt/resume、重启恢复、定向删除与 dump/restore。

额外的维护启动回归：`tests/test_postgres_deployment_pause.py` 与 `tests/test_postgres_startup.py` 共 8 项通过（40.97 秒）。导入包包含 Paper Key，维护模式重启不创建模拟账户或事件；解除暂停后沿用原初始化行为。维护开关的各入口采用同一解析。

最终审查后的存储回归：`test_database_maintenance.py`、`test_config_store.py`、`test_config_write_atomicity.py` 共 38 项及 9 个子测试通过（25.88 秒）。持久密钥解密失败时运行配置加载明确失败，不回退旧 env；保留低层诊断接口原契约，严格加载不依赖可被并发清除的全局错误变量。清理令牌加入 PostgreSQL 行版本 `xmin`，备份期间同一 ID 的内容更新也会拒绝删除；仅读取身份及版本，不把大 payload 拉入内存。

## 实际 Compose 演练

隔离项目 `crypto-pg-smoke-c5f8ce1924`，独立 PG/应用卷，仅 Web 映射宿主回环 55440，数据库不开放宿主端口。使用包含 `: @ / % $` 的虚构密码验证 URL 编码与 Compose 环境处理。

已通过：

1. 空库正式迁移、只启动 Web、健康检查；维护模式下导入 enabled 任务，未生成调度/交易维护记录。
2. version=1 配置导入且 `write_env=false`，目标密钥不变；重启后 MCP issuer/resource 使用导入地址。
3. 自定义中文 Prompt、配置密钥、模拟余额 10123 和真实 PostgreSQL checkpoint 在 Web 容器替换后保留，PG 容器身份不变。
4. 实际运行更新脚本：停止两应用、生成备份、迁移、Web 健康、唯一 scheduler 启动。由于本轮未授权发布镜像，仅 registry pull 子命令替换为已构建本地镜像；其他 Docker 操作真实执行。
5. 使用实际生产备份函数生成的 `database.dump`，恢复到另一个随机空数据库。正式结构校验、配置解密、模拟余额、聊天 checkpoint、Prompt 文件归档全部核对一致；临时恢复库精确删除。scheduler 通过实际 Prompt 消费者读取同一自定义内容。
6. 两容器共享应用卷：同范围互斥、其他范围可推进；调用者超时后 worker 仍持锁，实际 kill worker 后锁释放。原操作仍为 unknown，重启后的同 ID 请求没有重发回调。
7. 同时冷启动 Web/scheduler，开启 PG `log_statement=ddl` 观测到 0 条 DDL，健康检查通过；恢复日志设置后停止演练应用。

本地证据：`data/test-temp/compose-rehearsal/state.json`、`restore-verification.json`、`cold-start-verification.json`，以及 `compose-update-final-r2.log`、`container-locks.log`。备份在演练目录的 `backups/20261015T073457Z/`，包含 COMPLETE、数据库 dump、应用文件、环境及镜像身份。备份包含虚构测试凭据，仍按私密文件保存。

最终冻结镜像再次执行同一更新脚本成功，Web/scheduler 实际 image ID 一致、PG 容器未变，配置、Prompt、checkpoint、模拟余额均保留。记录 `data/test-temp/compose-final-image-update.log`，新备份 `backups/20261015T075807Z/`。仅 registry pull 仍使用本地替身；演练应用已停止以免影响容量测试。

实际演练发现并修复两个备份工具问题：libpq 的 PGDATABASE 必须接收数据库名，不能直接塞完整 URL；旧镜像 manifest 被清理后，镜像元数据改为读取仍存在的容器身份。备份客户端凭据仅通过环境或临时私密 service 文件传递，失败不继续迁移或启动应用。

## 构建、容量与边界

前端生产构建已通过。最终源码冻结后的两份 Dockerfile 均构建成功；260 个生产文件逐字节匹配工作区，各自使用新网络/PG 卷完成新空库迁移、Web/前端健康及实际 cgroup 检查，临时资源全部清理。Python 3.11.17，PG 客户端/服务端 17.11。

| 本地镜像 | 最终镜像身份（未发布） |
| --- | --- |
| `crypto-agent:postgres-test`（Dockerfile.prebuilt） | `sha256:8ba6e035d98b0860b0119007847b0bd1d0c28e2b8a4e7000b878d12cdc64bddb` |
| `crypto-agent:postgres-full-test`（Dockerfile） | `sha256:c05e9f1eb52862ecb2198fab394e269cd6b38276ea78898c3495458358e79786` |

指纹、版本、烟测和资源清理证据位于 `data/test-temp/final-image-verification/`；它们不能代替完整三容器容量验收。

容量使用预声明 `scripts/capacity/postgresql-local-v1.json` 全部阶段及阈值。早期失败、Docker Desktop 无响应和原生 core 调查保留在 [结构与验证记录](POSTGRES_SCHEMA.md)，不得将部分峰值改写为验收通过。用户授权的本机 Docker Desktop 重启已经完成；没有重置 Docker 数据或执行全局 prune。

最终容量运行 `20261015T081109Z-44430ca1` 使用上述 `8ba6e035` 镜像，profile SHA256 为 `73f4ed8f0e69ac8fedc308989e8070bb16ffeccd5620bea43f5df1d3c1634e74`。宿主内存余量预检通过，阶段时长和通过阈值没有缩短或修改。两角色完成 cold、idle、typical、concurrent_refresh、slow_dependencies；key_retirement 中 Web 发生原生 SIGSEGV，退出码 139、`OOMKilled=false`，HTTP 返回 `RemoteDisconnected`。退休和重启阶段没有完整结果，整轮判定 **failed**。已观测 Web/scheduler/PG 内存峰值为 402,182,144 / 364,527,616 / 108,867,584 字节，PG 连接峰值 6；这些部分结果不能替代未完成的验收。

原始报告 `data/capacity-reports/20261015T081109Z-44430ca1/report.json` 保持原样，同目录保存 manifest、进程日志、资源采样、`failure-analysis.json` 和 `cleanup-confirmation.json`。最后有效资源样本的 cgroup 限制均与声明匹配，OOM 增量为 0；原报告中的限制不匹配、终点 OOM 等拒绝项包含中断导致的最终字段缺失，不能单独据此认定配置不符，也不能把缺失的终点证据补写为通过。确切的三容器、PG 卷和网络已清理；没有自动重跑、降低阈值、推送镜像或更新生产。

本轮新 core `wsl-crash-1792052091-1-_usr_local_bin_python3.11-11.dmp`（2,000,220,160 字节）已完整保留到 `data/test-temp/wsl-crashes-20261015/`，C 盘空闲从崩溃后的约 1.05 GB 恢复至约 3.04 GB；旧 09:31 core 未处理。静态分析使用的 Python 可执行文件和 libpython 与实际故障镜像的二进制 SHA256 一致。`data/pg-native-diagnostics-20261015/core-final-python.txt` 显示崩溃位于 `_PyEval_EvalFrameDefault`，正在处理 `SystemError('error return without exception set')`，作为值栈地址使用的 `rbp`/`r13` 却为 `0x9`。这证明解释器当时状态异常，尚不能证明是谁更早破坏了状态。

`core-final-state.txt` 记录当前线程的 trace/profile 函数、tracing/use_tracing、替代 eval_frame、async_exc 均为 0，解释器 `gc.collecting=0`。Python 日志中的驱动线程等待 `Future.result`、诊断 Timer 等待 `Event.wait`。这些只排除相应机制在该快照中处于活动状态，不能排除更早的影响。只读源码审查未发现项目线程异步异常注入或解释器帧写入；最终镜像无 pytest 和测试目录，Windows 专用 ctypes RSS 分支不会在 Linux 执行。当前故障目录是单次 OfflineExchange 私有数据，不能将值栈地址损坏简单解释成普通字典并发修改。根因仍未确定，之前把嵌套 generator 等价改为 `itertools.chain` 没有消除崩溃。

本轮不替目标服务器作容量承诺。发布烟测与容量测试均使用隔离资源；发布脚本的 Git/Docker push 顺序由 mock 测试验证，本轮没有实际执行发布入口。

### 有界诊断与 Python 3.12 对照候选

原 Python 3.11 完整验收仍为失败。随后三个单次诊断均未重现原生崩溃，但覆盖范围、分配器和时长与完整验收不同，不能据此确认修复：

| 诊断 | 实际覆盖与限制 |
| --- | --- |
| v1：标准库及提取的原函数 | 约 40 秒，至少 120 轮、1200 次遍历；末尾诊断检查把标准库生成模块 `_sysconfigdata__linux_x86_64-linux-gnu` 误判为第三方模块，进程退出 1。保留该错误，不记为通过 |
| v2：真实应用导入与目录缓存 | `-X dev`，90.267 秒、178 次成功加载和 178 次预期预算拒绝；尝试了 32 个 key，但成功加载仅覆盖其中 31 个。未启动 lifespan、PG、调度器或 HTTP，不覆盖完整应用路径 |
| v3：真实 worker、PG 与假外部依赖 | Python 3.11 原失败镜像，`PYTHONMALLOC=debug`、psycopg binary；仅 cold 10 秒和 key_retirement 60 秒。两角色完成，分别覆盖 32 个退休 key，无原生崩溃或分配器错误；不是原 profile 的完整 acceptance |

诊断均使用隔离资源和禁外部访问边界，执行前核对禁 core，未自动重试。v3 的三容器、卷和网络均已清理。原日志、误判和覆盖不足不作改写，证据在 `data/pg-native-diagnostics-20261015/` 的 `minimal-stdlib-v1-assessment.md`、`minimal-cache-v2-assessment.md` 和 `full-debug-v3-20261015T083718Z-bba9cf90/report.json`。

容量测试 worker 随后新增 Linux `RLIMIT_CORE=0` 和 `PR_SET_DUMPABLE=0` 校验，在应用导入和工作线程启动前执行；失败就拒绝启动探针。这仅防止再次生成约 2 GB 的 core 填满宿主磁盘，保留 faulthandler 的致命错误及线程栈输出，不修复崩溃、不修改业务服务或容量门槛。相关 Windows 回归 **14 passed、2 skipped**，Linux 对实际 setup/helper 的 **4 项检查通过**。此前保留的原生 core 不删除。

另仅通过显式 build arg 构建 Python 3.12 对照候选，生产 Dockerfile 默认值尚未变更：`crypto-agent:postgres-py312-candidate`，image ID `sha256:226827940be86b67d8f17e98f3a44c950f4228094effa8b338b772ecc9e08d6a`，OCI revision 为 `postgresql-py312-candidate`。Python 3.12.15 / GCC 14.2.0、PGO/LTO、glibc 2.41；psycopg 3.3.6 binary、PG 客户端 17.11。114 个运行依赖名称及版本保持锁定，213 个 backend 文件及合计 260 个镜像内生产文件均与工作区逐字节一致。构建前后 C 盘空闲约 2.51 GB，E 盘约 90.79 GB，未清理其他镜像或缓存。

构建及只读身份核验记录为 `py312-candidate-build.log`、`py312-candidate-identity.json`、`py312-candidate-summary.json`；指纹位于 `data/test-temp/final-image-verification/crypto-agent-postgres-py312-candidate-fingerprint.json`。原 3.11 失败镜像和 core 均保留。

3.12 候选随后使用正常分配器执行原冻结 profile 的完整验收，运行 `20261015T084504Z-fb5cc800` 也判定 **failed**，profile SHA256 仍为 `73f4ed8f0e69ac8fedc308989e8070bb16ffeccd5620bea43f5df1d3c1634e74`。两角色完成前五阶段；key_retirement 中 Web 原生 SIGSEGV，于 08:47:42 UTC 退出，退出码 139、`OOMKilled=false`。faulthandler 保留的当前栈是 `copy.deepcopy` → `public_catalogue.py:342` 的目录复制；驱动线程仍在等 Future，诊断 Timer 在等待。scheduler 在失败采证时仍为 running，之后由清理流程删除。退休与 restart 没有完整结果，不能从不同 Python 栈认定根因不同或已经定位。

该轮已观测 Web/scheduler/PG cgroup 内存峰值为 365,625,344 / 379,375,616 / 109,056,000 字节，PG 连接峰值 6；391 个 Web 延迟样本 p95 为 205.24 ms，另有一次探测错误。最后有效限制与声明匹配、已观测 OOM 计数为 0，但缺少整轮终点证据，这些部分数据不构成通过。原始 report、manifest、进程日志及 Docker 状态保留在 `data/capacity-reports/20261015T084504Z-fb5cc800/`，未改写原报告。

只读逐项 inspect 确认前缀 `crypto-capacity-20261015t084504z-fb5cc800` 的 `-web`、`-scheduler`、`-db` 三容器、`-pgdata` 卷和同名网络均不存在。WSL core 目录仍只有此前 09:31 的旧文件，本轮未新增 core；C 盘剩余约 2.51 GB。禁 core 达到了避免巨型转储的目的，未避免崩溃。**3.12 候选未通过，也未选为生产默认**；总进度仍为 34/37，保留剩余任务及发布阻断，没有再次运行压力或构建。

### 纯 Python 驱动对照的最新结果

从同一 3.11 镜像仅增加 `PSYCOPG_IMPL=python` 的候选 `crypto-agent:postgres-pure-driver-candidate`，image ID 为 `sha256:17dbcd27396d75f37202e00189886015559ed1ba8fb9c99da0f2b5ba1bdc5d39`。生产文件和 RootFS 保持一致；实际驱动实现为 python，未加载 psycopg_binary，同时 libpq 从 bundled 18.6 切换到系统 17.11，因此不是仅改变一个底层变量。

正常分配器、同一冻结 profile 的第一次完整运行 `20261015T085604Z-8371c55e` 通过全部阶段，`verdict=accepted`，706 个 HTTP 样本、0 错误，p95 311.52 ms、最大 424.25 ms。但为验证重复性明确执行的第二次独立运行 `20261015T090249Z-133f0f33` 在 cold 阶段再次失败：Web 退出码 139、`OOMKilled=false`，驱动报告 `RemoteDisconnected`。两份原始报告均保留在 `data/capacity-reports-pure-driver/`，不能用第一次通过覆盖第二次失败，也不能据此认定驱动切换已经修复问题。

截至此次核对，正式 Dockerfile 尚未设置该候选驱动实现，Python 默认仍为 3.11；候选没有发布或部署，Linux 候选全量回归尚未执行。任务仍为 **34/37**。数据库功能实现和本地镜像构建已完成，稳定性验收与正式交付尚未完成。

最终严格 OpenSpec 校验、SQLite 引用审查及 diff 检查结果随最终回归补充。运行 SQLite 驱动仅保留在旧上传副本的只读分析中；`sqlite_blob.py` 只是兼容模块名，内容已是原生 PG 字节查询。

服务器首次切换及以后 push 后的操作见 [部署指南](DEPLOYMENT_GUIDE.md)。镜像发布与服务器更新是两个独立步骤。
