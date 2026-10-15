# 运行限制逐项审阅

日期：2026-10-15。下表保留修改前的清单；后续确认结果以本节和 [当前运行说明](RUNTIME_CAPACITY.md) 为准。

## 已确认的调整

- 保留并发、单次请求/响应超时和重试。现货定投资金预算明确保留，金额、权限、订单归属和幂等语义不变。
- 移除工具调用链路的内存/缓存字节与数量门槛、CPU 配额和 CPU 对 scheduler 并发数的进一步压缩。
- 移除归属索引单轮时间和总条数门槛、回执恢复共享预算、MCP 线程累计执行预算、管理发现子预算及 Paper 维护整轮预算。
- 成交/资金流水同步取消总调用页数预算；每页大小保留，完整扫描继续后续页。
- 移除回执格式化的 4 KiB/32 KiB 硬预算，避免操作完成后展示阶段再报资源错误。
- 缓存 TTL、账户请求节奏、分页接口、工具字段契约、诊断日志轮转保留，不扩展修改业务语义。
- Compose 已移除 CPU、内存和 swap 配额。服务器需同步 Compose 并重建容器才能解除旧配额；更新镜像不会替换 Compose。

旧 `MCP_*_BUDGET_SECONDS` 兼容为响应等待超时；新模板使用 `MCP_*_TIMEOUT_SECONDS`。响应超时不会再导致后台线程后续读取因同一截止时间立即失败。

数值来自当前代码默认值和 `.env.template`，不是生产进程环境的实时读数。部署记录显示服务器已设置 `RUNTIME_CAPACITY_ENABLED=false`，但仍有 Docker 配额、执行器线程数、整轮预算、分页和请求超时。这个开关没有承诺关闭所有限制。

## 修改前的逐项清单（历史参考）

每项可选择“保留”“移除”或“调整为具体数值”。没有选择的项目保持原样。建议单独列出，不作为已确认需求。

| 编号 | 限制 | 当前默认或部署记录 | 移除后的具体变化 | 入口 |
| --- | --- | --- | --- | --- |
| 01 | 应用总并发与排队准入 | 总 worker 8，其中关键任务保留 2；队列 16，等待 2 秒 | 不再因应用自定义总量和排队门槛拒绝任务；监控仍可保留 | `RUNTIME_CAPACITY_TOTAL_WORKERS/RESERVED_WORKERS/QUEUE_LIMIT/QUEUE_SECONDS` |
| 02 | 按工作类型和 MCP 的并发/队列准入 | Agent 2；MCP 读 4、执行 2、认证 2、恢复 2；MCP 队列一般为对应并发数的 2 倍 | 同时进入的任务更多；不等于解除下面的实际线程池限制 | `RUNTIME_CAPACITY_ROLE_LIMITS`、`MCP_*_CONCURRENCY/QUEUE_LIMIT` |
| 03 | 实际 scheduler 线程池 | Agent 默认 2 且受有效 CPU 数进一步约束；维护默认 2；成交同步和持仓保护各硬编码 2 | 可改变实际同时处理任务数；删除固定池需要选择替代执行方式 | `SCHEDULER_MAX_WORKERS`、`SCHEDULER_MAINTENANCE_WORKERS`、`backend/app/core/scheduler.py` |
| 04 | 应用内存、记录和响应体门槛 | 总估算 512 MiB；单原始记录 4 MiB；金融回执 1 MiB；HTTP 响应 8 MiB | 不再按自定义字节上限拒绝完整记录或响应；实际占用由输入和主机决定 | `RUNTIME_CAPACITY_BYTES/MAX_RECORD_BYTES/FINANCIAL_PAYLOAD_BYTES/HTTP_RESPONSE_BYTES` |
| 05 | 缓存、客户端、流的数量和内存门槛 | 目录 32 项/64 MiB/刷新 128 MiB；客户端 32；账户协调器 128；行情 256 项/8 MiB/刷新 16 MiB；新闻 64 项/16 MiB/工作 32 MiB；流 8 MiB | 取消资源准入门槛；缓存过期策略是另一项 | `RUNTIME_CATALOGUE_*`、`CLIENT_*`、`COORDINATOR_ENTRIES`、`QUOTE_*`、`NEWS_*`、`STREAM_BYTES` |
| 06 | Docker CPU/内存配额 | 部署记录：Web 2 GiB/1 CPU；scheduler 2 GiB/0.5 CPU；DB 1 GiB/0.5 CPU；无 swap | 取消容器 cgroup 配额，让进程共享宿主资源；需要改服务器 Compose 并重建，改镜像不能取消 | `compose.yaml` 的 `mem_limit/memswap_limit/cpus` |
| 07 | 后台回执整轮预算 | 默认 10 秒，一轮多个操作共享 | 后续操作不再因前面操作耗尽整轮时间而被截断；单次网络超时可独立保留 | `RUNTIME_RECOVERY_SECONDS`、`backend/utils/background_receipts.py` |
| 08 | 归属索引每轮时间片 | 默认 0.25 秒；另有分页 32 条 | 取消时间片后可以继续推进本页；是否取消分页需单独决定 | `RUNTIME_INDEX_SCAN_SECONDS`、`backend/utils/execution_plan_index.py` |
| 09 | MCP 请求端到端预算 | 读取 20 秒、执行 40 秒、认证 3 秒、恢复 20 秒；取调用链更早的截止时间 | 请求可等待更长或一直等待依赖完成；单次 HTTP 超时可独立保留 | `MCP_READ/EXECUTE/AUTH/RECOVERY_BUDGET_SECONDS` |
| 10 | 单次依赖和数据库超时 | MCP 依赖 HTTP 默认 8 秒；账户锁等待也受剩余预算约束；PG 连接池等待 10 秒，连接超时 5 秒；普通连接上下文默认预算 30 秒并据剩余时间设置 SQL/锁超时；聊天检查点 SQL 30 秒、锁 5 秒 | 删除单次超时会改变连接失效时的返回行为；与 07/09 的总预算不同 | `MCP_DEPENDENCY_TIMEOUT_SECONDS`、`MCP_DB_WAIT_SECONDS`、`DB_POOL_TIMEOUT_SECONDS`、`backend/storage/__init__.py`、`checkpoints.py` |
| 11 | PostgreSQL 连接数量 | 应用池每进程最多 8；聊天检查点池最多 2；等待队列最多为各自池大小；DB 服务端另有 `max_connections` | 增加连接并发；应用与数据库服务端需分别决定 | `DB_POOL_MAX_SIZE`、`DB_CHECKPOINT_POOL_MAX_SIZE`、PostgreSQL 启动参数 |
| 12 | 账户请求节流 | 按 CCXT 交易所 `rateLimit × endpoint cost` 计算，同一账户共享节奏 | 取消应用主动间隔；不会改变交易所服务端对请求频率的要求 | `backend/mcp/exchange_runtime.py::install_transport` |
| 13 | 账户串行锁、交易范围锁、幂等与版本检查 | 同账户网络锁；账户/标的/方向文件锁；数据库事务/范围锁；操作 ID 去重；计划版本比较 | 删除会改变同一账户的并发写入及重复请求处理语义，需要明确到具体机制 | `exchange_runtime.py`、`management_lock.py`、`backend/storage`、`trade_operations.py` |
| 14 | 扫描分页与每轮调用量 | 后台默认 32 条，接口接受 1–128；成交同步默认最多 100 次；成交抓取 Binance 每页 1000、其他适配器 100；流批次默认 1000 条 | 可以调整每轮工作量；分页并非停止后续同步，游标仍会续扫；上游接口页长不能仅靠本地删除 | `RUNTIME_SCAN_BATCH_SIZE/STREAM_ITEMS`、`ExecutionLedger.sync` |
| 15 | 同步间隔和重试退避 | 成交同步默认最短 30 秒；后台普通恢复间隔 5 秒，读失败指数退避最高 300 秒；订单证据退避最高 60 秒 | 同一事实会更频繁查询，影响请求量和恢复节奏 | `execution_ledger.py`、`background_receipts.py`、`independent_exits.py` |
| 16 | 缓存有效期/退休/失败等待 | 目录/空闲退休 300 秒；目录失败等待 2 秒；ticker 2 秒、candles 15 秒 | 改变数据复用和重新请求频率；不是并发准入限制 | `RUNTIME_*TTL/IDLE/RETRY_SECONDS`、`MCP_*CACHE*_SECONDS` |
| 17 | 工具契约的批量和标的数 | 每个交易批次 1–20 动作；现货任务 1–10 标的；MCP 配置最多 30 标的/管理范围 | 改变工具接受的请求规模和公开参数契约，需要同步模型 schema、测试及文档 | `trading_contracts.py`、`spot_portfolio.py`、`mcp/settings.py` |
| 18 | 模型回执展示预算 | 单项 4096 字节，批次 32768 字节；完整原始证据另存 | 模型收到的内容可以更长，增加输入量；与前端 JSON 美化无关 | `backend/utils/trade_summary.py` |
| 19 | 模型与记忆重试 | 模型超时配置默认 120 秒、网络重试默认 2；记忆模型单次任务最多 3 次调用 | 改变模型失败后等待及重试次数，需要区分读取请求和交易重放 | `llm_utils.py`、`memory_agent.py` |
| 20 | 新闻抓取与展示量 | 模板 `NEWS_MAX_ITEMS=6`、总抓取预算 5 秒；代码新闻数夹在 1–12 | 改变新闻输入范围/数量及等待行为，需独立选择 | `NEWS_MAX_ITEMS/TOTAL_TIMEOUT_SECONDS`、`news_context.py` |
| 21 | Paper 专用负载/扫描预算 | 模板 payload 1 MiB（代码未配时 256 KiB）；扫描批次 20、历史扫描 100、维护 30 秒 | 改变模拟交易扫描工作量和输入大小；不直接解除实盘同步预算 | `PAPER_MAX_PAYLOAD_BYTES/SCAN_BATCH_SIZE/HISTORY_SCAN_LIMIT/MAINTENANCE_SECONDS` |
| 22 | 资源诊断自身日志上限 | 单文件 1 MiB、4 份备份、阶段缓冲 64；采样间隔 3 秒 | 改变诊断文件增长和采样成本，不改变订单查询结果 | `RESOURCE_EVIDENCE_BYTES/FILES/STAGE_BUFFER/SAMPLE_SECONDS` |

## 修改前开关的边界（历史参考）

`RUNTIME_CAPACITY_ENABLED=false` 主要关闭 01、02、04、05 这类资源准入门槛，以及实现中明确受该开关控制的 Paper 负载门槛。它不关闭 03 的 scheduler 固定执行器、06 的 Docker 配额、07–19 的所有预算/契约/一致性检查。某个值仍出现在环境里，不代表对应容量门槛还在生效；应看具体调用点和 `limits_enabled`。

例如，本次日志里的 `account_rate_limit` 是预算耗尽时所处阶段：它可能只是整轮的 10 秒已耗尽后进入账户节流检查，不能只凭这个字符串判定是交易所拒绝，也不能认为把节流删除就一定解决。

## 最初建议（已由上方确认结果替代）

优先逐项决定 07、08、09，直接针对恢复被本地总时限中断；再决定 03、06、11 的实际并发和宿主资源安排。10 的单次网络超时、12 的账户节流、13 的一致性机制可独立保留。14 的续扫分页与 17 的单次业务请求上限需分开，不把一页 32 条解释成总共只同步 32 条。

本表梳理运行、同步、工具调用及相关产品边界；页面分页、字段字符串长度、订单数值合法性、授权范围等更细的业务校验不默认归入“移除运行限制”。若选择修改 13 或 17，需按其具体子项明确执行语义。

还有两类名称上容易混淆的边界：MCP 数值配置解析本身默认只接受 0.001–300 的有限数值，并发参数允许 1–32、队列允许 0–128，因此不能直接填写 `0` 或 `infinity` 来关闭预算；要移除须修改实现。聊天的 `CHAT_CONTEXT_RECENT_MESSAGES=10`、`CHAT_CONTEXT_COMPACTION_BATCH=6`、`CHAT_CONTEXT_SUMMARY_MAX_CHARS=8000`、`CHAT_CONTEXT_RECENT_MAX_CHARS=18000` 用于选择近期消息及触发压缩，不是删除完整聊天记录或直接截断工具原始证据。

## 代码依据

- [容量配置及监控模式](RUNTIME_CAPACITY.md#explicit-observation-mode)
- [现有部署记录](POSTGRES_DEPLOYMENT_20261015.md)
- [环境模板](../.env.template)
- [后台回执恢复](../backend/utils/background_receipts.py)
- [MCP 预算及执行池](../backend/mcp/resilience.py)
- [账户节流和网络等待](../backend/mcp/exchange_runtime.py)
- [归属索引](../backend/utils/execution_plan_index.py)
- [成交同步](../backend/utils/execution_ledger.py)
- [调度器](../backend/app/core/scheduler.py)
- [数据库连接池](../backend/storage/__init__.py)
