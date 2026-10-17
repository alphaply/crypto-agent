# 定时维护报错修复（2026-10-17）

## 已定位的原因

- `protection_tick` 根据管理策略补充待维护任务时，没有限定为 `REAL`。模拟任务和现货任务随后被只接受实盘合约的分支误报为缺少实盘配置。只限制策略补充的任务集合；数据库中真正存在的孤立实盘保护计划仍需报警。
- 已授权“全部币种”的 Binance MCP 账户会使用无 symbol 的挂单查询进行发现。CCXT 默认在发送请求前拒绝这种调用。只为采用 `visible_first_claim` 且范围为 `all` 的运行时客户端确认该调用；选定币种的客户端保持原有限制。没有修改保存的账户范围、交易所或管理策略。
- MCP 合约账户的 `symbol` 可以是 `None`，但 `ExecutionLedger` 必须使用具体币种。原来同步线程在遍历账户已有币种前启动监听，导致 `exchange.market(None)` 抛出 `TypeError`。在生产同版本 CCXT 上已离线取得完整调用栈：`ExecutionLedger.__init__` → `binance.market` → 字符串与 `None` 拼接失败。

## 修复范围

成交监听与原有定期同步共用同一份已有币种列表，逐币种启动；没有已有币种时不创建无效监听。普通单币种实盘任务保留监听；模拟和现货任务不启动合约成交监听。创建账本成功后再创建异步连接，避免账本初始化失败时遗留连接。

本次不包含数据库迁移、不导入或清理配置、不删除或重写交易历史。部署需备份，重建 web 和 scheduler，并核对两个服务的实际镜像 ID。服务恢复后正常自动任务仍会继续产生运行记录和更新当前状态。

## 回归验证

针对性用例位于 `tests/test_scheduler_runtime_regressions.py`，覆盖任务模式分派、真实孤立计划报警、Binance 现货/合约全账户挂单请求与选定币种限制、多币种成交监听以及空币种不启动监听。

与 `test_mcp_symbol_scope.py`、`test_protection_integration.py`、`test_execution_stream_metrics.py`、`test_news_schedule.py`、`test_management_scheduler.py`、`test_visible_position_management.py`、`test_shared_account_management.py` 一起运行，使用独立 PostgreSQL 测试容器，不挂载生产数据卷，不执行真实交易测试。

首次额外运行 `test_background_capacity_lifecycle.py` 时，其两个旧用例失败：仍期望 `RUNTIME_STREAM_BYTES` 改变计量值，以及 `RUNTIME_NEWS_CACHE_ENTRIES` 限制新闻缓存条数。在未修改的生产基础镜像上单独复跑，两项同样失败（0.23 秒），确认是本次修复前已存在的测试与资源观测实现不一致。没有为通过旧测试更改资源策略或新闻缓存。新增用例另行验证监听轮换期间保留原线程和资源计量，以及账本初始化失败后释放资源且不创建网络客户端。
