# Spec Delta

## MODIFIED Requirements

### Requirement: Bankruptcy freezes financial actions and order execution
破产状态 SHALL 仅在下列两类情形触发：(1) 每日扣息时可用现金不足；(2) 账户已无任何未平持仓（全部平仓或爆仓）且可用现金小于等于零且仍有未偿债务。只要账户仍持有未达维持保证金强平线的有效持仓，系统 SHALL 不将账户判定为破产，已有退出订单与止盈止损 SHALL 保持正常撮合。处于真实破产状态的账户 SHALL 禁止开平仓、撤改单、保护设置、借还款及转出，已有订单停止成交。状态读取、接受转账、系统计息及获授权下海重置仍可执行。

#### Scenario: Old order meets its trigger after bankruptcy
- **WHEN** 破产账户已有入场、退出、止盈止损或其他待成交订单达到成交条件
- **THEN** 不撮合成交、不产生新交易资金变化，不因普通停用逻辑只拦开仓而继续执行退出单

#### Scenario: Bankrupt account receives help
- **WHEN** 正常账户向破产用户转账且付款准入通过
- **THEN** 正常入账，破产状态不自动解除；该用户仍需主动下海才能恢复操作，后续每日扣息仍可使现金为负

#### Scenario: Position remains active while net equity is negative
- **WHEN** 账户持有未触及维持保证金强平线的永续合约或现货持仓，但扣除手续费或浮亏导致总资产减去债务小于等于零
- **THEN** 账户不进入破产状态，持仓保持正常状态，挂单撮合与止盈止损监控持续生效，允许执行平仓及撤单，但禁止新增开仓与新增借款

#### Scenario: All positions closed with remaining debt and nonpositive cash
- **WHEN** 账户所有持仓均已通过自主平仓或爆仓强平结束，且剩余可用现金小于等于零且仍有未偿债务
- **THEN** 账户正式进入破产状态，记录 bankruptcy 事件，冻结后续操作并等待下海重置

## ADDED Requirements

### Requirement: Historical stuck accounts with valid positions automatically recover from bankruptcy
系统在启动、加载或刷新账户状态时，SHALL 自动检测历史上因净资产小于等于零被标记为破产、但当前仍持有未平仓有效持仓的账户。系统 SHALL 自动将该账户状态恢复为活跃（active），解除订单撮合限制，使已有持仓与止盈止损挂单恢复正常监控。

#### Scenario: Account with open position recovers from bankruptcy
- **WHEN** 系统加载一个处于 bankrupt 状态但持仓列表非空的 Paper 账户
- **THEN** 将其 account_status 自动重置为 active，保留原持仓与关联挂单，使止盈止损单恢复可撮合状态
