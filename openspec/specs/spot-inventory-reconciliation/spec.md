# spot-inventory-reconciliation Specification

## Purpose

Provides resilient spot inventory reconciliation that safely isolates and bounds MCP-managed sellable quantities without fatal-failing when an account holds external balances, manual assets, or fee discrepancies.

## Requirements

### Requirement: Spot inventory tolerates external and manual holdings
现货库存查询与卖出准备 SHALL 容忍账户中存在非 MCP 建立的底仓、外部划转或手续费扣减差异，不得因总余额大于本地记录买入量而抛出致命错误阻断调用。

#### Scenario: Pre-existing balance does not block inventory query
- **WHEN** 现货账户在 MCP 买入前已有底仓，或总余额大于本地买入净数量，调用者执行 get_spot_inventory
- **THEN** 系统正常返回库存快照，标明总余额、已核验的 MCP 归属数量、已锁定数量以及可安全退出的最大数量，不抛出 INVALID_ARGUMENT 异常

#### Scenario: Sell amount is bounded by safe inventory
- **WHEN** 调用者请求 create_spot_exit 且卖出数量不超过可用余额与核验归属量的安全交集
- **THEN** 系统允许该卖出或挂单继续，并在回执中记录对账说明，避免因底仓混存拦截正常止盈或平仓

### Requirement: Discrepancies are reported as structured reconciliation metadata
当现货账户余额与 MCP 独占记录存在差异时，系统 SHALL 在库存回执中提供结构化对账明细（如 total_balance、verified_owned、external_holdings、fee_adjustment），而非直接中断交易生命周期。

#### Scenario: Structured breakdown returned on variance
- **WHEN** 发生币安 BNB 手续费抵扣或手动加仓导致账实数量不一致
- **THEN** get_spot_inventory 回执返回详细的持仓来源分解字段，并在有未决委托时明确指出需要人工复核的具体差异，供 Agent 作出准确的业务决策
