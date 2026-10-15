# 工具调用链路：并发与资源观测

2026-10-15 起，工具调用链路移除内存/缓存准入门槛、CPU 配额对线程数的进一步压缩、归属索引时间片和累计执行预算。保留并发、请求/响应超时、重试、账户锁、订单归属校验和现货定投资金预算。确认范围见 [限制清单](LIMITS_REVIEW_20261015.md)。

## 并发与请求超时

`RUNTIME_CAPACITY_ENABLED=true` 控制进程总并发、角色和队列准入。顺序子工作复用父任务名额，并行子工作单独申请。关键恢复保留名额包含在总量内。物理线程在响应超时或客户端断开后仍持有名额，直到完成；不得因调用方离开而重放金融操作。

`RUNTIME_CAPACITY_ENABLED=false` 保持原有仅观测并发的行为。两个模式下，字节数与内存压力都不拒绝任务。scheduler 使用 `SCHEDULER_MAX_WORKERS`，不再按 CPU 配额缩小。

MCP 响应等待默认读取 20 秒、执行 40 秒、认证 3 秒、恢复 20 秒，变量名为 `MCP_READ/EXECUTE/AUTH/RECOVERY_TIMEOUT_SECONDS`。旧 `MCP_*_BUDGET_SECONDS` 兼容为响应超时；新变量优先。响应超时不会令物理线程后续读取和落盘立即失败。

每次交易所 HTTP 请求独立应用 `MCP_DEPENDENCY_TIMEOUT_SECONDS`（默认 8 秒）及客户端传输超时。队列、锁和数据库请求仍有各自超时。超时不代表订单未提交或已成交，仍需按原操作身份核验。

## 完整证据与分页

归属索引在一次调用里继续所有数据库页，`RUNTIME_SCAN_BATCH_SIZE` 仅控制每页读取量，不限制总记录数或执行时间。全部来源扫描完成后再处理 algo 别名冲突，保留跨页普通订单证据。真实失败后的持久游标仍可恢复。无效证据和并发身份变化如实报告。

迁移 `0005` 避免仅观察字段变化反复重置归属扫描；`0006` 删除计划大小导致 UNKNOWN 的条件，并重新推进旧活动索引。无效 JSON 仍为 UNKNOWN，原始记录保留。

成交和资金流水同步持续到请求范围完整或真实依赖/数据错误，不再设置总调用次数和页数预算。交易所每页长度及原有查询范围不变。回执恢复和 Paper 维护不再共享整轮截止时间；后台分页、游标和重试退避保留。

HTTP 正文、存储记录、市场目录、报价、成交流批次、新闻缓存及 Paper payload 不因字节或缓存数量门槛拒绝或跳过。Paper 订单承诺完整读取。模型回执保留事实投影，但不再因 4 KiB/32 KiB 硬预算拒绝或删除已投影字段。完整原始证据仍持久保存。

## 缓存与观测

市场目录键仍区分交易所、市场类型、端点和公开选项。匿名公开加载、单次共享刷新、紧凑标准字段和独立可变客户端保持不变。账户凭证区分账户协调器，不进入公开目录身份。失败刷新保留旧观察时间，过期信息不会成为新的账户证据。

TTL、空闲退休和显式关闭释放缓存。活动锁与用户引用保持固定。字节 lease 仅记录估算占用，不作准入；诊断显示 `byte_limit=null`、`memory_limits_enabled=false`。估算不是 RSS。RSS/cgroup 与内存压力保持观测。

| 配置 | 默认 | 含义 |
| --- | ---: | --- |
| `RUNTIME_CAPACITY_ENABLED` | true | 并发/排队准入开关 |
| `RUNTIME_CAPACITY_TOTAL_WORKERS` | 8 | 总物理工作承诺 |
| `RUNTIME_CAPACITY_RESERVED_WORKERS` | 2 | 包含在总数内的恢复保留名额 |
| `RUNTIME_CAPACITY_QUEUE_LIMIT` | 16 | 等待队列 |
| `RUNTIME_CAPACITY_QUEUE_SECONDS` | 2 | 排队超时 |
| `RUNTIME_CAPACITY_ROLE_LIMITS` | `{}` | 固定角色并发配置 |
| `RUNTIME_SCAN_BATCH_SIZE` | 32 | 数据库每页大小 |
| `RUNTIME_CACHE_IDLE_SECONDS` | 300 | 空闲退休 |
| `RUNTIME_CATALOGUE_TTL_SECONDS` | 300 | 目录有效期 |
| `RUNTIME_CATALOGUE_RETRY_SECONDS` | 2 | 刷新失败退避 |

旧 `RUNTIME_*_BYTES/ENTRIES/ITEMS`、`RUNTIME_INDEX_SCAN_SECONDS`、`RUNTIME_RECOVERY_SECONDS`、`MCP_MANAGEMENT_DISCOVERY_BUDGET_SECONDS`、`PAPER_MAX_PAYLOAD_BYTES`、`PAPER_MAINTENANCE_SECONDS` 不再设置工具门槛。无需继续增大这些环境值解决未同步。字段契约、权限、交易金额/精度与现货定投资金预算语义不变。

## 部署与历史证据

仓库 Compose 不再设置 CPU/内存/swap 配额。推镜像不会修改服务器 Compose 或已运行容器配额；需同步该修改并重建相应容器，见 [部署指南](DEPLOYMENT_GUIDE.md)。数据库迁移仍须在新程序启动前完成。

旧容量 profile 和 [验收记录](CAPACITY_VERIFICATION.md) 保留为历史测量，不代表本版本强制阈值。事务规则见 [PostgreSQL 并发](POSTGRES_CONCURRENCY.md)，监控见 [资源诊断](RESOURCE_DIAGNOSTICS.md)。
