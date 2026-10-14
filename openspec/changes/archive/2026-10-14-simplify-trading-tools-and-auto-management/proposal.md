# Proposal

## Why

内置 Agent 同时看到单项交易动作与批次工具，MCP 又统一公布实盘和 paper 的 50 个入口，重复的 schema、兼容名称及完整回执增加模型选择和调用成本。查询已经展示人工仓位与挂单，执行却仍可能因来源、缺少周期或额外接管步骤拒绝；用户已确认按可见交易范围自动纳管、管理权先到先得并随任务持续保留。

## What Changes

- **BREAKING** 默认模型目录移除单项 open/close/edit/cancel、各模式同义交易适配器、旧 execute_trade 分发器及 adopt 工具。内置交易 Agent 和绑定任务聊天统一绑定 `execute_trade_actions` 与 `get_trade_operation`；只读聊天、记忆和策略摘要继续无交易工具。
- 新增版本化统一顺序执行契约，覆盖 REAL、STRATEGY、SPOT_DCA 和 MCP paper 的现有交易能力；一项数组处理单笔，多项按顺序处理。保留整批结构/权限预检、逐项动态准入、失败停止、显式依赖、稳定父子操作身份和真实执行状态。现有 v2 与无版本旧批次继续按原契约解释。
- 按 key 类型、权限及任务/profile 能力裁剪 MCP 默认目录。核心收敛为交易上下文、标的搜索、行情、新闻、账户状态、订单/成交记录、顺序执行和操作查询；paper 额外保留借还款、账户设置及排行榜。事件扫描转机器人调度/HTTP 接口，旧工具保留受原权限约束的非默认兼容调用。
- 返回模型所需的精简逐项回执，批次末尾最多附一次按需账户摘要；完整回执、历史和核验证据继续持久化并可查询。
- **BREAKING** 默认管理规则改为配置可见交易范围内自动认领。首个原子登记成功的任务/profile 成为管理者；绑定聊天和同 profile 的 MCP principal 沿用同一个管理身份。人工或外部来源不再单独成为拒绝理由，读权限不取得交易或认领权限。
- 管理权独立于交易周期，空仓、人工全平、暂停决策及进程重启后继续保留。删除、缩小范围、换账户或交接必须显式处理管理权及活跃/未确认资源；已有有效管理者和历史身份优先保留，不按升级或发现先后抢占。
- 自动记录真实基线、外部订单及成交，处理人工加减仓、改单、全平与重开，按已核验边界维护周期与残单。现货纳管使用可用余额及共享资产预留约束；paper 继续隔离 namespace/user 账户。保持实际交易所能力和参数约束。
- 同步 Agent/MCP 提示、配置界面、当前状态展示及文档，删除默认流程中的先 adopt/解锁要求，明确真正的管理竞争、目标变化、数量限制和相关未知执行。

## Capabilities

### New Capabilities

- `model-tool-catalog`: 精简并按身份/模式/权限裁剪模型工具目录，聚合查询、兼容入口隔离及目录契约验收。
- `visible-position-management`: 从可见交易授权范围自动产生管理范围、先到先得认领、人工操作纳管和持久化管理权生命周期。
- `paper-ordered-trading`: paper 顺序动作执行、父子意图持久化、账户事务和重放/中断恢复。

### Modified Capabilities

- `trading-tool-contracts`: 统一版本化顺序入口、管理目标而非创建来源、精简回执及旧调用兼容。
- `independent-position-exits`: 自动恢复/建立管理基线、外部成交参与周期闭合，管理权不随周期结束释放。
- `spot-inventory-reconciliation`: 可见现货底仓自动纳管，管理量与历史买入归属量分离，保持余额/预留边界。
- `decision-state-context`: 默认状态对齐可见范围、管理者、来源、可管理动作和精简工具结果。

## Impact

- 后端：`backend/agent/tool_registry.py`、`tool_interfaces.py`、`trading_contracts.py`、`trade_batch.py`、Agent/Chat 图；MCP server/tools/service/auth/settings、paper 工具/订单/账户/事件接口；management_policy/position_management/management_lock、scheduler、execution_ledger、independent_exits、spot_execution/spot_orders/inventory、配置及数据库迁移。
- 前端与文档：任务和 MCP 管理配置、管理竞争及交接状态、当前仓位/订单投影；README、MCP、交易工作流、独占管理和 QQBot paper 文档。接口目录变更需验证客户端重新发现及旧客户端调用。
- 依赖：基于当前工作区已实现、尚未归档同步的 `add-exclusive-position-management`。其主规范同步必须先于本变更同步；本变更明确取代有限手填范围、默认 strict_owned 和模型先 adopt 的相关默认行为，不修改该变更文件，也不包含其 MCP 宕机治理等独立范围。
- 验收：各模式目录数量与 schema，单项/混合顺序动作、cancel 前置释放资源、权限预检、未知结果及崩溃重放；跨进程认领竞态、人工变化、空仓/暂停/重启保留管理权、显式交接；现货资产竞争、paper namespace/事务隔离、精简回执及界面。测试采用离线交易所/账户与故障注入，不要求真实下单。
- 本次仅生成规划。不包含任意代码 PTC、动态前项结果引用、等待成交的后台交易计划、新交易策略、新交易所能力、生产部署或发布；保留历史数据、已有回执身份、trade/cancel 权限及绑定聊天审批。
