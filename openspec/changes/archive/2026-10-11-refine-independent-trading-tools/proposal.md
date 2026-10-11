# Proposal

## Why

当前独立退出已有分批开平仓基础，但工具命名、数量含义与默认提示词仍混有整仓强制保护的假设：普通限价退出被叫作 `take_profit_limit`，`amount=0` 只是提交时的固定数量，模型也容易把“开仓后补保护”理解为必须当轮完成。需要让 Agent 自主安排多次建仓、双向持仓、定量分批退出与最后剩余全平，并准确表达币安普通单、条件单及改单能力。

## What Changes

- 明确独立退出允许跨轮决策：本轮开仓，确认成交后在本轮或后续轮次自行设置、调整止盈止损；缺少 TP/SL 本身不阻止开仓或强制平仓。
- 保留并明确同轮多次 `open`、多价位分批建仓与 Hedge Mode 下同币多空建仓；同轮提交不承诺并行成交或原子事务。
- 统一 `close` 的六种执行类型：普通市价、普通限价、条件市价/限价止盈、条件市价/限价止损；每笔选择固定 `amount` 或明确 `close_all`，支持“前几笔定量，最后剩余全平”。
- **BREAKING（独立退出行为）**：停止默认按比例改写剩余固定退出档位；取消同类挂单合计量的额外本地排他预留校验，保留单笔数量校验。保留 Agent 指定数量，展示实际挂单状态及数量冲突，由 Agent 使用 `edit`/`cancel` 调整；订单仍须满足交易所减仓语义与当前归属。
- 将独立模式的 `update`/`amend` 用户入口收敛为 `edit`，统一新契约的剩余数量含义；普通 LIMIT 优先原生修改，条件单通过可恢复的撤旧建新实现，并返回新旧订单身份。
- 内置 Agent、模拟交易与 MCP 使用同一规范化契约。统一参数、结构化回执、示例和批处理动作；补齐 MCP 的独立退出改单入口。旧名称、旧参数及旧持久化记录经兼容层保留原语义，不把历史普通限价单重新解释成条件单。
- 保留 `execute_trade_actions` 作为显式顺序组合器；`execute_trade` 保留为兼容分发入口，不能获得高于专用工具的权限。`cancel` 继续撤销归属订单，按普通/条件单路由。
- 更新提示词、账户上下文、退出单展示与运行文档，区分已提交、待触发、部分成交、已成交和待核验。

## Capabilities

### New Capabilities

- `trading-tool-contracts`: 内置 Agent/MCP 的独立交易动作、参数、改单、批处理、回执及兼容契约。
- `independent-position-exits`: 跨轮自主交易、同方向聚合仓位、定量分批退出、动态全平及固定退出单的生命周期。
- `binance-perpetual-orders`: 币安 USDⓈ-M 普通单/条件单的创建、查询、撤销、修改、持仓方向与成交身份映射。

### Modified Capabilities

无。当前主规格目录尚无能力规格；上述规格同时记录与本次变更相关的既有行为和新增要求。

## Impact

- Agent：`backend/agent/{agent_models,agent_tools,tool_interfaces,tool_registry,trade_batch,agent_graph}.py` 与 REAL/STRATEGY 提示词。
- 执行与持久化：`backend/utils/{independent_exits,position_protection,execution_ledger,trade_operations,exit_context}.py`、`backend/database_independent.py`、订单身份和持仓展示服务。
- MCP：`backend/mcp/{tools,service,guard}.py` 的契约、权限、额度和操作回执；不涉及真实账户配置变更。
- 展示与文档：现有前端退出单展示、`README.md`、`docs/TRADING_WORKFLOW.md`、`docs/MCP.md`、`docs/WORKBENCH.md`；增加针对新行为和历史兼容的测试。
- 复用现有 CCXT；根据真实能力拒绝不支持的订单组合，不增加轮次间合成订单执行服务。
- 范围外：`adopt` 行为、现货交易、附带保护模式的策略、账户持仓模式自动切换、每批入场独立持仓/OCO、保证前档成交后才执行末档的依赖订单、修改既有周期结束时的入场单清理策略。
