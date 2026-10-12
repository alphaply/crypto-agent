# Proposal

## Why

独立退出模式下，市价平仓后的旧挂单清理异常会覆盖本次平仓的已知执行事实，工具仅返回 `unknown` 和撤单错误，导致 Agent 无法判断是否已经平仓，也无法安全继续交易。周期缺失、异常回执缺少查询身份、恢复结果未同步及其他交易流程的状态归类问题进一步延长阻塞；同时需要准确解释 B AI Claude Sonnet 5.5 的 effort 请求与缺失推理文本。

## What Changes

- **最高优先级：修复市价平仓后的交易恢复。** 分离本次订单结果、持仓证据、周期清理状态和后续动作准入；已核验成交不能被无关清理异常改写为成交未知。清理结束后，Agent 在同轮后续决策或下一轮能够依据最新证据继续交易，无需人工换 ID 或重置周期。
- 增加保留操作身份、订单身份、失败阶段、证据时间和阻塞目标的结构化回执；统一 `failed / pending / unknown / submitted / completed / skipped / not_executed` 的明确语义。
- 补齐按原操作查询的只读恢复入口，将后台对账结果同步到内置 Agent、聊天、持久化回执和 MCP；只读查询不得撤单、补单或继续撤建。
- 对 `independent_position_cycle_missing` 提供可审计诊断和基于完整归属证据的原周期恢复；无法确认归属时保留阻塞，不自动接管外部仓位。
- 横向修复开仓、固定/all 保护单、普通改单、条件撤建、撤单、批处理及兼容入口的同类回执问题；保持幂等、减仓语义、权限和母子订单关联。
- 单独补充模型推理可观测性：区分请求的 effort、发送的参数、上游报告的推理用量与可展示文本。复用已有原生 Messages 能力，不把 HTTP 200 或文本为空当作 effort 生效/失效证明，不静默迁移现有渠道。
- 增加主故障端到端回归、异常与重启恢复矩阵及运营诊断文档。实盘交易验证不纳入自动测试；模型探测使用脱敏样本，在线探测为显式、限额操作。

## Capabilities

### New Capabilities

- `model-reasoning-observability`: 模型推理配置的实际发送参数、上游证据和展示状态，覆盖流式/非流式及工具轮次。

### Modified Capabilities

- `trading-tool-contracts`: 结构化操作回执、只读操作查询、跨入口恢复以及根据明确证据继续执行。
- `independent-position-exits`: 平仓与周期清理解耦、阻塞范围、原周期诊断恢复和待核验订单可见性。
- `binance-perpetual-orders`: 撤单 ACK 与最终成交证据分离、条件母子单核验及明确拒绝/未知结果分类。

## Impact

- 执行与存储：`backend/utils/independent_exits.py`、`position_protection.py`、`trade_operations.py`、订单证据/归属账本、`backend/database_schema.py`、`backend/database_independent.py`。
- 调用链：`backend/agent/independent_tools.py`、`tool_registry.py`、`tool_interfaces.py`、`trade_batch.py`、两个 Agent graph、后台 scheduler 与 MCP service/store/tools。
- 展示：Agent 账户上下文、决策回执组件、MCP 查询；模型配置与推理展示链路包括 `llm_utils.py`、配置 API、模型渠道页及 reasoning 持久化。
- 在现有交易 `contract_version=2` 中增加版本化回执扩展；保留旧请求哈希和旧订单解释，旧回执采用保守读取。现货及附带保护模式只覆盖共用状态解析的兼容回归，不改其策略或生命周期。
- 不修改交易策略、仓位规模、自动重入规则或账户持仓模式；不增加自动接管外部仓位、盲目重发平仓或自动调整固定保护档位。推理可观测性作为后续独立阶段，不阻塞平仓问题先验收。
