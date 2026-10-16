# Spec Delta

## MODIFIED Requirements

### Requirement: Spot inventory tolerates external and manual holdings
现货库存查询与卖出准备 SHALL 容忍账户中存在非 Agent/MCP 建立的底仓、外部划转或手续费扣减差异，不得因总余额大于本地记录买入量而抛出致命错误阻断调用。共享管理下，可交易授权范围内核验通过的实际基础资产 SHALL 自动纳管，管理量与历史买入归属量分别表达；卖出不再仅以历史本地买入量为上限。新卖出 SHALL 仍受真实可用余额、可管理未预留量及共享资产预留边界限制；只读查询不认领或建立基线。

#### Scenario: Pre-existing balance does not block inventory query
- **WHEN** 现货账户在 MCP 买入前已有底仓，或总余额大于本地买入净数量，调用者执行现货库存查询
- **THEN** 系统正常返回库存快照，标明总余额、已核验的历史 MCP 归属数量、已锁定数量、来源/共享管理状态和可安全退出的最大数量，不抛出 INVALID_ARGUMENT 异常，不通过读取认领

#### Scenario: Sell amount is bounded by safe inventory
- **WHEN** 调用者请求现货退出且卖出数量不超过真实可用余额与授权可管理未预留量的安全交集
- **THEN** 系统允许该卖出或挂单继续，并在回执中记录对账说明，避免因底仓混存或无历史 Agent 买单拦截正常止盈或平仓

#### Scenario: First sale manages visible pre-existing balance
- **WHEN** 可交易 profile 授权该账户与现货标的，实际基础资产余额可核验且共享预留和资金事实可核验，调用者直接请求卖出底仓
- **THEN** 原调用协调共享资源并记录真实库存基线后继续数量与能力校验，不要求先买入、adopt 或填写独占清单，不伪造历史买入成交

#### Scenario: Competing profile sees the same inventory
- **WHEN** 另一配置读取到同一账户基础资产，但该共享范围已有管理者
- **THEN** 查询仍返回授权事实，交易按当前权限、共享预留和真实可用量准入，不因另一 owner 拒绝，也不把其他来源库存伪造为本配置历史买入量

### Requirement: Spot ownership and reservations share the physical base inventory
现货共享管理与库存预留 SHALL 以同一物理账户、spot 市场及基础资产协调，同基础币跨报价标的共享同一真实库存及未预留数量，不按连接复制库存或设唯一 owner。历史订单所属标的、来源及操作身份 SHALL 保留，不能因管理聚合重复计入库存或收益。实盘与 paper namespace/user、STRATEGY config 的库存 SHALL 继续隔离。

#### Scenario: Cross-quote requests cannot sell the same inventory twice
- **WHEN** 同账户 BTC/USDT 已预留 0.7 BTC 退出，Agent 随后通过 BTC/USDC 请求另一笔 0.6 BTC，而实际可管理总库存为 1 BTC
- **THEN** 第二项按共享基础资产预留和真实余额拒绝数量超限，不把报价币不同视作两份独立 BTC 库存

#### Scenario: Cross-process profiles compete for one asset
- **WHEN** 两个不同 profile 同时对同账户 BTC/USDT 与 BTC/USDC 的真实 BTC 资源提交已授权动作
- **THEN** 按同一基础资产串行核验共享余额及预留，双方在资金允许时均可执行，资金不足时仅拒绝数量超限，不因管理者不同拒绝或重复计算库存

#### Scenario: Market types and simulated namespaces do not share stock
- **WHEN** 相同基础币分别存在合约账户、实盘现货及不同 paper namespace/user 或 STRATEGY config
- **THEN** 维持各自库存与权限边界，不能以另一市场持仓或模拟余额补足现货可卖数量
