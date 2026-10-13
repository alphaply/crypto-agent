# Proposal

## Why

当前决策 prompt 将当前账户快照、完整历史回执、过期准入建议和短期记忆中的旧限制同时交给模型解释；用户提供的样本中，14 条回执全部 `pending=false`，完整回执仍占 prompt 约 70% 字符，记忆却保留旧空单导致的全局禁令。需要由程序提供一致的当前交易状态，让执行层负责核验与恢复，并使记忆中的执行疑虑能够随对应证据更新而退出决策上下文。

## What Changes

- 为独立退出模式的定时决策及绑定任务聊天提供统一的当前交易状态视图：当前持仓、活跃挂单、保护覆盖、数量冲突，以及仍影响当前决策的具体异常。
- 移除每轮默认注入的完整历史操作回执、重复订单描述和过期 `continuation` 限制；本轮工具结果、显式原操作查询、原始审计和 MCP 查询继续保留。
- 异常按账户、任务、标的、方向、订单/操作及实际影响范围生成；正常等待触发、已明确拒绝且无执行影响、已完成且无残留影响的操作不进入待核验异常。
- 区分证据观察时间与本地回执生成时间，避免把旧证据重新包装成“最新核验”；正常后台维护停止无变化的已结束历史回执刷新，保留相关未解决事项的推进。
- 修复记忆证据链，使每轮及滚动整理都能按原操作身份取得最新结果，包括四小时窗口外仍被旧记忆引用的事项；最新证据替换相同事项的旧限制，无法匹配的事项不得臆断已解决。
- 将决策可见的策略记忆、近期策略摘要与执行状态引用分开。原文保留用于审计，执行异常统一从当前结构化状态呈现；升级时处理旧自由文本，避免旧禁令从记忆或最近三轮摘要重新进入默认上下文。
- 核验继续由执行层和既有后台维护承担。模型无需例行调用查询解锁，也不会因为历史异常被清理而自动执行旧请求。

## Capabilities

### New Capabilities

- `decision-state-context`: 决策可见的当前交易状态、相关异常、策略记忆投影及基于证据的执行引用更新；涵盖默认输入与按需历史查询的边界。

### Modified Capabilities

- `independent-position-exits`: 明确各入口状态语义一致但展示详略不同；当前异常的范围和证据新鲜度；后台维护不持续把已结束历史操作刷新成当前事项。
- `trading-tool-contracts`: 核验结果通过当前状态视图和记忆证据到达消费者，完整历史回执按需读取；查询时间与实际证据观察时间分开，保留现有工具、身份和执行语义。

## Impact

- 上下文：`backend/agent/agent_graph.py`、`chat_graph.py`、`decision_context.py`、`backend/utils/exit_context.py`、`order_context.py`，以及新增的共享当前状态投影模块。
- 执行证据：`trade_recovery.py`、`trade_operations.py`、`trade_receipts.py`、`independent_admission.py`、`independent_exits.py` 和既有 scheduler 维护入口；复用证据分类与动作依赖，不新增另一套交易许可机制。
- 记忆：`memory_updates.py`、`memory_service.py`、`memory_agent.py`、`summary_prompts.py`、`decision_record.py`、`database_memory.py` 及添加式本地投影元数据。原始摘要、回执修订、人工记忆原文不覆盖删除。
- 契约：不改变金融工具参数、原操作幂等键、批次策略、MCP 授权边界或 Chat 审批。本次默认交易上下文行为作用于 REAL/STRATEGY 的 `independent_exits`；现货和附带保护模式保持现有语义。
- 文档与验证：同步 `README.md`、`docs/PROMPT_DESIGN.md`、`docs/TRADING_WORKFLOW.md`、`docs/TRADE_RECEIPTS_AND_RECOVERY.md`；使用合成回执、临时数据库和 fake exchange 验证，无需实盘下单或真实模型联网测试。
