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
转账金额 SHALL 为符合现有 USDT 精度的有限正数，不超过付款账户扣除预留后的可用现金。双账户资金变化、事件和操作回执 SHALL 原子提交；相同操作身份的重试 SHALL 不重复扣款或入账，改变意图复用身份 SHALL 拒绝。转账回执 SHALL 清楚显示完整付款/收款身份、USDT 金额、转后可用现金及挂单占用，并透传适用的付息提醒。提交 SHALL 复核付款账户周期，等待期间发生下海重置的旧意图不得动用新周期资金；已完成转账重试仍返回原回执。

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

#### Scenario: Interest exceeds actual cash
- **WHEN** 到期利息为 10U，账户现金只有 3U 且无其他预留
- **THEN** 完整扣收 10U，现金成为 -7U，记录利息并进入破产状态，不以余额不足跳过或减免利息

#### Scenario: Settlement retries or service restarts
- **WHEN** 同一计息期间重复执行结算，或服务重启后处理尚未结算期间
- **THEN** 每段实际借款时长仅计息一次，保留已发生借还款和调息边界，不重复扣息或漏计欠缺期间

### Requirement: Entry orders carry advisory interest cash reminders
账户仍有借款计息或已累计未结利息时，QQBot SHALL 在开仓、增加入场挂单资金占用前提醒留出未占用 USDT 现金付息，并在成功开仓、入场挂单或入场改单回报中带出服务端付息提醒。系统 SHALL 在余额查询的 balances.interest_reserve 与对应成功金融回执的 interest_reserve 中提供后台实际日利率、完整一天估算、下次零点预计扣息、已到期未扣利息、合计预计所需现金、可用现金、预计缺口与结息时间。提醒 SHALL 仅为资金安排建议，不额外冻结资金、拒绝订单、要求再次确认或修改用户明确的下单数量。

#### Scenario: Entry spends cash needed for interest
- **WHEN** 用户明确指定开仓数量且下单通过原有资金检查，操作后未占用现金低于预计付息需求
- **THEN** 按原数量执行并在回报中提醒现金缺口，不将提醒升级为新增拒单或强制预留规则；挂单占用和仓位保证金不算未占用现金

#### Scenario: First-day estimate and paid principal retain accrued interest
- **WHEN** 账户首次借款不足一天，或当日还清本金但仍有已累计未结利息
- **THEN** 下次预计扣息按实际时段与有效利率计算，完整一天估算单独标明，不冒充首日实际利息；仍有未结利息时继续提醒

#### Scenario: Balance reminder precedes delayed maintenance
- **WHEN** 查询账户时后台尚未结算到当前时间，期间可能跨越日界或发生调息
- **THEN** 只读计算历史待扣与下次零点预计利息，保留各段有效利率和结算精度，不改变现金、破产状态或订单；next_settlement_at 指向当前时间之后的下次北京时间零点

#### Scenario: Account has no interest burden
- **WHEN** 账户既无正在产生的借款利息，也无已累计未结利息
- **THEN** balances.interest_reserve 为 null，交易回报不附加付息提醒

### Requirement: Bankruptcy freezes financial actions and order execution
破产状态 SHALL 仅在下列两类情形触发：(1) 每日扣息时账本实际现金不足，该判断不扣减挂单占用；(2) 账户已无任何未平持仓（全部平仓或爆仓）且可用现金小于等于零且仍有未偿债务。在未因实际现金不足付息触发破产的前提下，只要账户仍持有未达维持保证金强平线的有效持仓，系统 SHALL 不得仅因净权益非正将账户判定为破产，已有退出订单与止盈止损 SHALL 保持正常撮合。系统 SHALL 不得仅因挂单占用导致可用余额不足而在扣息时判定破产。处于真实破产状态的账户 SHALL 禁止开平仓、撤改单、保护设置、借还款及转出，已有订单停止成交。状态读取、接受转账、系统计息及获授权下海重置仍可执行。

实际现金不足付息触发的新破产 SHALL 记录 bankruptcy_reason=insufficient_cash_for_interest，以便后续读取、收款及持仓恢复逻辑保留真实破产原因。

#### Scenario: Pending orders reduce free cash below interest while actual cash is sufficient
- **WHEN** 账户实际现金为 100U、入场挂单占用 95U、到期利息为 10U，且现有持仓未触及强平线、没有其他破产条件
- **THEN** 扣息后实际现金为 90U，不因可用余额不足而进入破产；保持已有持仓和退出单的正常管理与撮合，入场订单继续遵守原有成交资金检查

#### Scenario: Old order meets its trigger after bankruptcy
- **WHEN** 破产账户已有入场、退出、止盈止损或其他待成交订单达到成交条件
- **THEN** 不撮合成交、不产生新交易资金变化，不因普通停用逻辑只拦开仓而继续执行退出单

#### Scenario: Bankrupt account receives help
- **WHEN** 正常账户向破产用户转账且付款准入通过
- **THEN** 正常入账，破产状态不自动解除；该用户仍需主动下海才能恢复操作，后续每日扣息仍可使现金为负

#### Scenario: Position remains active while net equity is negative
- **WHEN** 账户持有未触及维持保证金强平线的永续合约或现货持仓，但扣除手续费或浮亏导致总资产减去债务小于等于零，且未因实际现金不足付息触发破产
- **THEN** 账户不进入破产状态，持仓保持正常状态，挂单撮合与止盈止损监控持续生效，允许执行平仓及撤单，但禁止新增开仓与新增借款

#### Scenario: All positions closed with remaining debt and nonpositive cash
- **WHEN** 账户所有持仓均已通过自主平仓或爆仓强平结束，且剩余可用现金小于等于零且仍有未偿债务
- **THEN** 账户正式进入破产状态，记录 bankruptcy 事件，冻结后续操作并等待下海重置

### Requirement: Historical stuck accounts with valid positions automatically recover from bankruptcy
系统在启动、加载或刷新账户状态时，SHALL 自动检测历史上因净资产小于等于零被标记为破产、但当前仍持有未平仓有效持仓的账户，并排除实际现金不足付息导致的真实破产。系统 SHALL 将符合上述误判条件的账户状态恢复为活跃（active），解除订单撮合限制，使已有持仓与止盈止损挂单恢复正常监控。

#### Scenario: Account with open position recovers from bankruptcy
- **WHEN** 系统加载一个历史上因净资产小于等于零误判为 bankrupt、持仓列表非空且并非实际现金不足付息破产的 Paper 账户
- **THEN** 将其 account_status 自动重置为 active，保留原持仓与关联挂单，使止盈止损单恢复可撮合状态

#### Scenario: Actual interest cash shortage remains frozen with open positions
- **WHEN** 账户实际现金不足以支付到期利息并进入 bankrupt，随后重新加载账户或收到转账，而持仓列表仍非空
- **THEN** 保留破产状态和冻结，不因有持仓或收款自动恢复，不重置账户、资金或债务

### Requirement: Proven historical reservation mistakes recover without resetting accounts
系统 SHALL 在账户锁下重新读取最新账本，检查最后一次 reset 之后本周期的 interest 与 bankruptcy 历史；核验 SHALL 使用完整的有界历史，账户周期不明、记录缺失或历史未读全时不得按挂单误判恢复。存在本周期现金扣为负数的利息记录或其他明确真实破产原因时 SHALL 不恢复，即使后来收到转账或仍有持仓也不例外。仅当完整证据显示扣息后的实际现金非负但低于仍保留的挂单预留，没有其他明确破产原因，且当前实际现金非负、净权益可核验为正时，SHALL 恢复 active。恢复 SHALL 保留现金、债务、已付利息、持仓、挂单、账户周期和累计下海次数，并追加带本周期及证据事件 ID 的 bankruptcy_recovered 记录，不执行下海重置。

#### Scenario: Proven reservation-only interest bankruptcy recovers intact
- **WHEN** 已破产账户本周期的完整历史显示扣息后现金为非负数但低于仍保留的挂单预留，无真实欠息或其他明确破产原因，且锁内重读的当前现金非负、净权益可核验为正
- **THEN** 恢复 active，现金、债务、已付利息、持仓、挂单、账户周期与下海次数均不改变，追加带证据事件 ID 的恢复记录

#### Scenario: Real cash shortage cannot be erased by later funds
- **WHEN** 本周期存在实际现金扣成负数的利息记录或明确真实破产原因，即使后来转账使现金和净权益回正、或账户仍有持仓
- **THEN** 保持真实破产，不按挂单误判或有持仓自动恢复

#### Scenario: Incomplete recovery evidence leaves the account unchanged
- **WHEN** 历史核验未读全、存在缺失记录或未知账户周期，无法确认本次冻结仅由挂单占用误判导致
- **THEN** 不按挂单误判恢复，不修改资金、仓位、挂单、账户周期或下海次数，不以部分历史推断不存在真实欠息

### Requirement: Reset requires the bankrupt user to initiate xiahai
破产用户 SHALL 通过可信本人身份主动说“下海”触发账户重置，bot SHALL 不主动邀请、不自动执行、不因引用或他人消息代为重置。非破产账户及 bot 自有账户 SHALL 不适用此重置流程；重复请求 SHALL 不多发资金或重复增加次数。

#### Scenario: User explicitly requests reset
- **WHEN** 破产用户在本人可信消息中明确要求下海
- **THEN** 执行一次自己的账户重置，累计下海次数增加一次，不再要求该用户额外申请后台审批

#### Scenario: No reset request has been made
- **WHEN** 账户已经破产，但用户仅聊天、询问状态或收到转账
- **THEN** 保持破产，不由 bot 发起下海邀请或自动重置

### Requirement: Reset starts a clean account cycle and retains audit history
下海重置 SHALL 原子清理本周期仓位、库存、挂单与预留，清零债务、负现金及本轮收益，按后台当前开户配置恢复资金与账户参数并开始新周期。历史交易、转账、利息、重置审计及累计下海次数 SHALL 保留；次数 SHALL 不限额。新周期 SHALL 清除旧 bankruptcy_reason，旧破产原因继续保留在原审计历史。旧订单及旧操作重试 SHALL 不改变新周期。

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
