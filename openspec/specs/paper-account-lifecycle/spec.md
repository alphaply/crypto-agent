# paper-account-lifecycle Specification

## Purpose

为同一 Paper 服务中的用户与机器人建立可审计的账户资金生命周期，覆盖互转、后台统一借贷与每日利息，以及破产冻结、用户主动重置和按账户周期计算的收益率及累计下海榜单。

## Requirements

### Requirement: Transfers move available USDT between authorized accounts
系统 SHALL 支持同 namespace 已开户账户间 USDT 转账，包括用户互转、用户与 bot 双向转账和 bot 用自有资金主动接济。付款身份 SHALL 来自可信授权，收款身份 SHALL 使用稳定账户标识，不凭昵称或正文自称获得付款权限。借来的可用资金 SHALL 可以转出，债务仍归原借款账户。

#### Scenario: Bot helps a user from its own account
- **WHEN** bot 自主决定向已明确身份的用户转账且自有账户有足够可用现金
- **THEN** 从 bot 账户扣款并向该用户入账，不扣其他用户资金，双方可核对同一转账事实

#### Scenario: User transfers borrowed funds to another account
- **WHEN** 用户将借来的可用 USDT 转给另一用户或 bot
- **THEN** 转账可成功，借款本金与利息责任保留在付款账户，不转移债务

### Requirement: Transfers validate amounts and commit once
转账金额 SHALL 为符合现有 USDT 精度的有限正数，不超过付款账户扣除预留后的可用现金。双账户资金变化、事件和操作回执 SHALL 原子提交；相同操作身份的重试 SHALL 不重复扣款或入账，改变意图复用身份 SHALL 拒绝。

#### Scenario: Malformed or negative amount is submitted
- **WHEN** 金额为负数、零、NaN、Infinity、布尔值或超出支持精度
- **THEN** 写入前拒绝，双方余额均不改变，不出现负金额反向扣收款人资金

#### Scenario: Concurrent transfers exceed available funds
- **WHEN** 同一付款账户并发转账或同时下单，合计金额超过可用余额
- **THEN** 提交时重新核验资金承诺，不透支；重试已完成的转账只返回原结果

### Requirement: Bank rules are configured centrally
管理员 SHALL 在后台统一设置借款开关、额度和日利率，机器人上下文及银行工具说明 SHALL 使用实际配置。关闭借款 SHALL 阻止新增贷款，不抹除已有债务、利息或已保存的借还款事实。

#### Scenario: Administrator updates bank policy
- **WHEN** 后台修改借款开关、额度或日利率并保存
- **THEN** 后续银行操作与工具上下文按已保存规则执行，不再固定宣称零利息

### Requirement: Daily interest uses principal and effective rates
系统 SHALL 对未还本金按单利计息，北京时间每日 00:00 收取上一日利息，借还款及首日按实际持续时长折算。日利率修改 SHALL 从修改时生效，结算 SHALL 保留期间适用利率，不追改已产生利息。扣息 SHALL 可以使现金为负，负现金本身不作为复利本金。

#### Scenario: Interest exceeds available cash
- **WHEN** 到期利息为 10U，账户现金只有 3U 且无其他预留
- **THEN** 完整扣收 10U，现金成为 -7U，记录利息并进入破产状态，不以余额不足跳过或减免利息

#### Scenario: Settlement retries or service restarts
- **WHEN** 同一计息期间重复执行结算，或服务重启后处理尚未结算期间
- **THEN** 每段实际借款时长仅计息一次，保留已发生借还款和调息边界，不重复扣息或漏计欠缺期间

### Requirement: Bankruptcy freezes financial actions and order execution
完整估值确认净权益不大于零，或扣息时资金不足的账户 SHALL 进入破产状态。破产账户 SHALL 禁止用户金融操作，包括开平仓、撤改单、保护设置、借还款及转出；已有订单 SHALL 停止成交。状态读取、接受转账、系统计息及获授权下海重置 SHALL 仍可执行。未知或不完整估值 SHALL 不被当作零。

#### Scenario: Old order meets its trigger after bankruptcy
- **WHEN** 破产账户已有入场、退出、止盈止损或其他待成交订单达到成交条件
- **THEN** 不撮合成交、不产生新交易资金变化，不因普通停用逻辑只拦开仓而继续执行退出单

#### Scenario: Bankrupt account receives help
- **WHEN** 正常账户向破产用户转账且付款准入通过
- **THEN** 正常入账，破产状态不自动解除；该用户仍需主动下海才能恢复操作，后续每日扣息仍可使现金为负

### Requirement: Reset requires the bankrupt user to initiate xiahai
破产用户 SHALL 通过可信本人身份主动说“下海”触发账户重置，bot SHALL 不主动邀请、不自动执行、不因引用或他人消息代为重置。非破产账户及 bot 自有账户 SHALL 不适用此重置流程；重复请求 SHALL 不多发资金或重复增加次数。

#### Scenario: User explicitly requests reset
- **WHEN** 破产用户在本人可信消息中明确要求下海
- **THEN** 执行一次自己的账户重置，累计下海次数增加一次，不再要求该用户额外申请后台审批

#### Scenario: No reset request has been made
- **WHEN** 账户已经破产，但用户仅聊天、询问状态或收到转账
- **THEN** 保持破产，不由 bot 发起下海邀请或自动重置

### Requirement: Reset starts a clean account cycle and retains audit history
下海重置 SHALL 原子清理本周期仓位、库存、挂单与预留，清零债务、负现金及本轮收益，按后台当前开户配置恢复资金与账户参数并开始新周期。历史交易、转账、利息、重置审计及累计下海次数 SHALL 保留；次数 SHALL 不限额。旧订单及旧操作重试 SHALL 不改变新周期。

#### Scenario: Account resets with negative cash and open orders
- **WHEN** 破产账户持有负现金、债务、持仓及未成交挂单并成功下海
- **THEN** 新周期为配置的初始账户状态、收益率为零、无旧仓位及挂单，历史证据仍可查，下海次数在后台可见

### Requirement: Leaderboards show current cycle returns and lifetime resets
公开排行榜 SHALL 提供交易收益率榜和下海次数榜，分别按本轮真实收益率、累计下海次数降序排列，沿用既有参赛可见性及隐私约束。交易收益 SHALL 排除转账和重置补款，计入手续费与利息；下海 SHALL 重置本轮收益率，不清除累计次数。不再以绝对盈利金额作为榜单排名指标。

#### Scenario: Reset trader earns a new return
- **WHEN** 用户初始 10000U 亏光，下海恢复 10000U 后交易净赚 2000U，期间无其他资金变化
- **THEN** 本轮交易榜收益率为 +20%，不累计旧轮亏损；下海榜保留累计次数

#### Scenario: Transfers do not manufacture trading gains
- **WHEN** 用户之间或用户与 bot 互转资金而无行情及交易损益变化
- **THEN** 转账金额不成为收款方交易利润或付款方交易亏损，沿用资本调整计分规则；利息支出仍计入本轮损益
