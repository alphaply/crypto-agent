# spot-inventory-reconciliation Specification

## Purpose

Provides resilient spot inventory reconciliation that safely isolates and bounds MCP-managed sellable quantities without fatal-failing when an account holds external balances, manual assets, or fee discrepancies.

## Requirements

### Requirement: Spot inventory tolerates external and manual holdings
现货库存查询与卖出准备 SHALL 容忍账户中存在非 Agent/MCP 建立的底仓、外部划转或手续费扣减差异，不得因总余额大于本地记录买入量而抛出致命错误阻断调用。默认 visible_first_claim 下，可交易范围内取得管理认领并核验通过的实际基础资产 SHALL 自动纳管，管理量与历史买入归属量分别表达；卖出不再仅以历史本地买入量为上限。新卖出 SHALL 仍受真实可用余额、可管理未预留量及共享资产预留边界限制；只读查询不认领或建立基线。

#### Scenario: Pre-existing balance does not block inventory query
- **WHEN** 现货账户在 MCP 买入前已有底仓，或总余额大于本地买入净数量，调用者执行现货库存查询
- **THEN** 系统正常返回库存快照，标明总余额、已核验的历史 MCP 归属数量、已锁定数量、管理者/认领状态和可安全退出的最大数量，不抛出 INVALID_ARGUMENT 异常，不通过读取认领

#### Scenario: Sell amount is bounded by safe inventory
- **WHEN** 调用者请求现货退出且卖出数量不超过真实可用余额与已认领可管理未预留量的安全交集
- **THEN** 系统允许该卖出或挂单继续，并在回执中记录对账说明，避免因底仓混存或无历史 Agent 买单拦截正常止盈或平仓

#### Scenario: First sale manages visible pre-existing balance
- **WHEN** 可交易 profile 授权该账户与现货标的，实际基础资产余额可核验且共享资产范围未被占用，调用者直接请求卖出底仓
- **THEN** 原调用自动认领并记录真实库存基线后继续数量与能力校验，不要求先买入、adopt 或填写独占清单，不伪造历史买入成交

#### Scenario: Competing profile sees the same inventory
- **WHEN** 另一配置读取到同一账户基础资产，但该共享范围已有管理者
- **THEN** 查询仍返回授权事实，交易仅因具体管理竞争被拒绝，不重复认领或把另一管理者库存当成本配置买入量

### Requirement: Discrepancies are reported as structured reconciliation metadata
当现货账户余额与管理基线或历史买入记录存在差异时，系统 SHALL 在库存回执中提供结构化对账明细，包括 total_balance、free_balance、managed_inventory、verified_owned、external_holdings、fee_adjustment、共享预留、观察时间与认领状态，而非直接中断交易生命周期。已核验外部变化 SHALL 更新当前管理量并保留来源；无法解释或取证不完整的差异 SHALL 指出缺项及受影响卖出，不伪造买入、收益或全面可交易结论。

#### Scenario: Structured breakdown returned on variance
- **WHEN** 发生币安 BNB 手续费抵扣或手动加仓导致账实数量不一致
- **THEN** 现货库存回执返回详细的持仓来源分解字段，并在有未决委托时明确指出需要复核的具体差异，供 Agent 作出准确的业务决策，不仅因外部来源阻断可核验卖出

#### Scenario: Historical attribution differs from current managed quantity
- **WHEN** 认领时存在 1 BTC 外部底仓，后续 Agent 买入净量为 0.2 BTC 且当前总量 1.2 BTC 可核验
- **THEN** 分别返回管理库存 1.2、历史买入归属 0.2 及外部基线 1，不把底仓计为 Agent 买入或伪造其历史成本和收益

#### Scenario: Incomplete evidence preserves known inventory facts
- **WHEN** 余额可读取但未决退出或外部变化证据不足以确认未预留管理量
- **THEN** 保留已知余额、来源和身份，将依赖缺失证据的可卖数量标为待核验，不用零值或历史买入量冒充完整最新数量

### Requirement: Spot ownership and reservations share the physical base inventory
现货认领与库存预留 SHALL 以同一物理账户、spot 市场及基础资产协调，同基础币跨报价标的共享唯一管理者及未预留数量。历史订单所属标的、来源及操作身份 SHALL 保留，不能因管理聚合重复计入库存或收益。实盘与 paper namespace/user、STRATEGY config 的库存 SHALL 继续隔离。

#### Scenario: Cross-quote requests cannot sell the same inventory twice
- **WHEN** 同账户 BTC/USDT 已预留 0.7 BTC 退出，Agent 随后通过 BTC/USDC 请求另一笔 0.6 BTC，而实际可管理总库存为 1 BTC
- **THEN** 第二项按共享基础资产预留和真实余额拒绝数量超限，不把报价币不同视作两份独立 BTC 库存

#### Scenario: Cross-process profiles compete for one asset
- **WHEN** 两个不同 profile 同时对同账户 BTC/USDT 与 BTC/USDC 的真实 BTC 资源认领
- **THEN** 仅一个原子认领成功，另一方得到基础资产范围及现管理者的具体冲突，不产生重复库存基线或卖出预留

#### Scenario: Market types and simulated namespaces do not share stock
- **WHEN** 相同基础币分别存在合约账户、实盘现货及不同 paper namespace/user 或 STRATEGY config
- **THEN** 维持各自库存与权限边界，不能以另一市场持仓或模拟余额补足现货可卖数量

### Requirement: Sellable quantity uses verified free balance and distinct reservations
新现货卖出 SHALL 同时不超过最新已核验 free balance 与当前可管理库存扣除不同活跃退出预留后的剩余量。交易所已锁定数量及同一本地预留 SHALL 不重复扣除。OCO 等原生互斥组合 SHALL 按一份可实际卖出的共享数量计入预留；部分成交、取消确认及未知结果 SHALL 按真实证据更新或保留预留。

#### Scenario: Native OCO reserves its quantity once
- **WHEN** 可管理库存为 1 BTC，一组原生 OCO 的两互斥腿共同退出 0.4 BTC，除此以外无预留或余额缺项
- **THEN** 该 OCO 总预留为 0.4，而非 0.8；下一笔退出仍按交易所 free balance 与管理未预留量的交集判断

#### Scenario: Cancel first makes verified inventory available to the next action
- **WHEN** 顺序批次先撤销占用库存的旧退出组合，终态、最终成交量及预留释放已核验，下一项请求新退出
- **THEN** 下一项在原调用内使用刷新后的 free balance 和共享预留准入，不要求调用者逐个等待工具返回或重复查询

#### Scenario: Uncertain cancellation cannot release stock early
- **WHEN** 旧退出撤销 ACK 已返回但最终成交或当前有效子单仍 pending/unknown，后项依赖其资源释放
- **THEN** 保留相关预留和原身份，后项按已声明依赖或实际库存依赖保持 not_executed，不把收到 ACK 当作可重复出售证明

#### Scenario: Another process reserves after a displayed snapshot
- **WHEN** 库存查询后同一共享资产出现新订单、成交或预留变化
- **THEN** 提交前重新核验并原子协调当前可用量，旧查询中的允许或可卖数字不作为执行授权

### Requirement: External spot changes preserve strategy and historical attribution
已认领现货范围的外部划转、人工买卖及挂单变化 SHALL 在核验后更新管理库存、当前订单和数量限制，保留真实来源及事实身份。观察基线或外部活动 SHALL 不伪造为 Agent 买入、卖出或收益。自动纳管 SHALL 不恢复人工修改前参数、不自动出售差额或改变原策略；固定请求数量不足时 SHALL 明确拒绝而非静默缩量。

#### Scenario: Manual removal reduces a requested sale
- **WHEN** 人工划走部分资产后真实 free balance 或管理未预留量小于已请求固定卖出量
- **THEN** 返回当前数量与具体限制，不按原快照卖出、不静默改成较小量，也不把差额记作 Agent 成交

#### Scenario: Human modifies an enrolled spot order
- **WHEN** 已纳管订单被人工改价、改量或撤销
- **THEN** 保留原身份并更新当前事实，不自动重建原参数；后续合法请求使用最新核验值及适配器支持能力
