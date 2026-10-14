# Spec Delta

## Purpose

使交易 Agent 和 MCP 在可信账户及可见交易授权范围内自动管理实际仓位、库存与订单，无需模型先手工接管或配置有限独占清单，同时通过首次原子认领、持续归属及真实来源审计协调多个管理者。

## ADDED Requirements

### Requirement: Management authorization follows trusted visible trading scope
系统 SHALL 默认使用 `visible_first_claim`，从可信任务或 profile 的交易账户、已授权标的、市场类型及调用者权限导出可管理范围。范围内资源 SHALL 不因人工、外部或启动前来源而单独被拒绝，不要求先调用 adopt 或填写额外独占范围。可见性 SHALL 以当前授权范围及核验事实判断，而非缓存、分页遗漏或模型自行提供的账户声明。

#### Scenario: Manage a visible manual position directly
- **WHEN** 可交易任务的账户与标的授权包含一笔已核验人工仓位，范围未被其他管理者占用，Agent 直接请求减仓
- **THEN** 系统在原请求内认领并纳管后继续正常准入，不要求先 adopt、解锁或再次提交相同请求

#### Scenario: Account and symbol boundaries cannot be overridden
- **WHEN** 工具参数或可见缓存引用另一账户、未授权标的或不匹配的市场类型
- **THEN** 系统在认领和交易写入前拒绝，并指出不匹配范围，不扩张任务或 profile 授权

### Requirement: The first successful durable claim determines management ownership
系统 SHALL 通过跨进程原子、持久化认领确定唯一管理者，首次成功提交的有效认领取得该范围。已有管理者 SHALL 优先保留；后续竞争只能产生具体范围的冲突，不能抢占或阻断无关范围。认领 SHALL 在启用且具备管理写权限的任务/profile 后台发现或首个合法管理请求中发生。管理请求 SHALL 先完成整批静态参数、模式及权限校验；静态拒绝不得产生认领。

#### Scenario: Concurrent processes discover the same position
- **WHEN** 两个不同任务在 Web 与独立调度进程同时认领同账户、同一重叠范围
- **THEN** 仅一个持久化认领成功，另一方获得包含实际管理者和冲突范围的结果，不产生重复周期或金融写入

#### Scenario: A conflicting side does not block an independent scope
- **WHEN** 请求范围由其他管理者占用，但同调用者在另一账户或无共享冲突的标的/方向拥有管理权
- **THEN** 只拒绝竞争范围的动作，其他满足自身条件的动作继续按顺序执行策略评估

#### Scenario: Invalid batch cannot reserve a management scope
- **WHEN** 批次参数、模式或任何所需 trade/cancel 权限在整体静态校验时失败
- **THEN** 整批在认领及交易写入前拒绝，不使无效请求取得未认领范围

### Requirement: Perpetual scopes follow actual position mode and visible directions
合约管理范围 SHALL 包含可信交易授权内实际可见或合法请求的持仓与订单方向。选定标的的 REAL 任务 SHALL 自动覆盖可见 LONG/SHORT，无需单独填写方向清单。Hedge SHALL 按方向协调；One-way SHALL 对共享净仓使用 BOTH 范围，不能以 LONG/SHORT 标签形成两个竞争管理者。

#### Scenario: Selected task discovers both hedge directions
- **WHEN** 同一任务已授权标的的 Hedge 账户同时存在可见 LONG 与 SHORT 仓位或挂单，且对应范围未被占用
- **THEN** 后台分别自动认领和管理两方向，单侧动作不修改另一侧周期或资源

#### Scenario: One-way opposite requests share ownership
- **WHEN** One-way 账户同一标的已被管理者 A 认领，管理者 B 请求相反方向交易
- **THEN** 按同一 BOTH 净仓范围返回管理竞争，不因方向相反绕过认领或金融协调

### Requirement: Unrestricted symbol access produces dynamic resource claims
MCP `symbol_scope=all` SHALL 在已授权市场内按实际发现的仓位、库存、订单或合法请求的目标动态导出认领范围，不要求预填最多 30 个标的/方向，也不预占整个市场。标的搜索、行情读取及目录发现 SHALL 不创建管理认领。

#### Scenario: Existing order is discovered outside a preset list
- **WHEN** 可交易 all profile 后台发现已授权市场的一张实际挂单，未配置手填管理标的清单且范围未被占用
- **THEN** 系统按该真实资源动态认领、核验并纳管，不因缺少有限清单拒绝

#### Scenario: First authorized entry has no existing exposure
- **WHEN** all profile 合法请求一笔当前无仓位和挂单的标的入场，所需范围未被占用
- **THEN** 在本次执行前按请求目标取得管理范围，并继续原交易校验，不要求先建立仓位

#### Scenario: Searching markets does not reserve them
- **WHEN** 调用者搜索大量交易标的或读取其行情
- **THEN** 系统只返回授权数据，不对搜索结果或所有市场建立认领

### Requirement: Management identity is shared only within the same trusted owner
任务 SHALL 使用稳定任务身份作为管理者，live MCP SHALL 使用 profile 身份作为管理者；绑定任务聊天及同 live profile 的多个 principal SHALL 沿用同一管理者。paper SHALL 使用可信 namespace/user 账户管理身份，STRATEGY 保持模拟 config 隔离。caller 的模式相应金融权限及聊天审批 SHALL 仍逐项校验，共享 owner 不授予其他调用者的权限。

#### Scenario: Bound chat manages its task order
- **WHEN** 绑定聊天请求管理同任务已认领订单
- **THEN** 沿用任务管理权并执行既有聊天审批，不作为第二个管理者竞争认领

#### Scenario: Read-only principal shares a writable profile
- **WHEN** 同 profile 的另一 principal 只有 read 权限
- **THEN** 允许其读取授权事实，不因 profile 已有管理权允许其认领、提交、修改或撤销交易

### Requirement: Ownership coordinates the physical trading account
真实管理范围 SHALL 使用可信保存账户身份协调同一物理账户，并包含市场类型与实际共享资源。相同 API 凭证的 profile 别名 SHALL 共享账户范围；同物理账户使用不同 API Key 时 SHALL 通过已确认的保存账户关联协调，不能把 Key 不同视作已证明账户隔离或自动猜测物理账户身份。

#### Scenario: Profiles use aliases of the same credentials
- **WHEN** 两个 profile 通过不同别名使用同一账户凭证并申请重叠资源
- **THEN** 系统识别同一账户范围，只允许一个管理者认领

#### Scenario: Different keys use one saved physical account
- **WHEN** 已确认属于同一物理账户的不同 API Key 配置关联同一保存账户身份
- **THEN** 重叠管理范围共同协调，不允许因 Key 不同取得两个管理者

### Requirement: Read tools and capability discovery do not claim resources
只读工具、模型目录发现及本地展示 SHALL 不认领、释放或转移管理权，也不触发交易所写入。它们 SHALL 返回实际可见资源、已有管理者和能力事实；后台或合法管理请求的自动认领 SHALL 与读取结果区别表达。read-only、尚未启用或仅发现目录的配置 SHALL 不参与首次新认领。

#### Scenario: Query an unclaimed external order
- **WHEN** read-only MCP 调用者读取一张可见但尚未被认领的外部订单
- **THEN** 返回订单与未认领状态，不创建管理者或周期，不提交、修改或撤销订单

#### Scenario: Disabled configuration appears in capability discovery
- **WHEN** 工具或配置查询读取一个暂停/停用的配置及其未认领资源
- **THEN** 查询不使该配置取得新的管理范围；已有认领继续按生命周期保留

### Requirement: Verified enrollment preserves facts and original identities
取得管理权后，系统 SHALL 自动核验资源并优先恢复同管理者可恢复的原周期；无可恢复历史时 SHALL 记录真实观察基线，保留外部订单身份、参数与来源。纳管 SHALL 不伪造入场成交、操作成功或历史收益；与原未知提交匹配的资源 SHALL 优先恢复原身份，不重复登记或重发。

#### Scenario: Existing position has protective and entry orders
- **WHEN** 启动前已有仓位、止盈止损和限价入场单，授权、认领与事实核验通过
- **THEN** 系统记录真实基线并纳管支持的订单，原请求可继续，不要求先清空挂单，也不自动撤销或恢复旧参数

#### Scenario: Lost acknowledgement becomes visible
- **WHEN** 新发现订单的客户端身份与原未知 Agent 提交相同
- **THEN** 恢复原操作和订单关联，保留其请求与成交事实，不作为第二笔外部订单登记或再次下单

### Requirement: External activity continuously updates managed resources
系统 SHALL 持续核验认领范围内外部加减仓、全平、重开及订单新增/修改/撤销，按稳定事实身份去重并保留来源与观察时间。数量相同 SHALL 不证明无成交；证据缺口 SHALL 只限制依赖它的动作。固定策略参数 SHALL 不因纳管自动重配，纳管本身不产生交易所写入。

#### Scenario: Human increases and decreases back to the same amount
- **WHEN** 外部先加仓后减仓使最终仓位数量不变，成交历史可核验
- **THEN** 两笔真实成交分别保留，当前管理量更新，不误报无变化或伪造 Agent 盈亏

#### Scenario: A human changes only order quantity
- **WHEN** 人工改量后 Agent 仅请求改价
- **THEN** 使用最新核验数量，不恢复人工修改前的数量；新事实仍保留外部来源

#### Scenario: Partial history does not become a synthetic fill
- **WHEN** 成交分页或订单通道取证不完整
- **THEN** 保留已知事实、缺项及恢复进度，不推进完整性检查点，不用余额或仓位差额伪造一笔成交

### Requirement: Current evidence and original permissions govern every write
每项交易 SHALL 在提交前重新核验目标、当前数量及必要执行依赖，保持原交易所能力、精度、trade/cancel 权限和审批。内部冲突写入 SHALL 跨进程协调；人工交易变化 SHALL 以新事实处理。未知原写入 SHALL 保留原身份并核验，不因认领、超时或调用者变化重复提交。

#### Scenario: Human reduces quantity before a fixed close
- **WHEN** 提交前人工减仓使剩余量少于请求的固定退出量
- **THEN** 返回具体数量冲突，不静默缩量或反向开仓；支持的 close_all 按其原生语义使用最新数量

#### Scenario: Unsupported external order remains visible
- **WHEN** 可见订单方向、作用或修改能力无法核验
- **THEN** 返回具体目标的缺项及支持能力，仅限制受影响动作，不把来源或单项异常扩大为全部交易禁止

### Requirement: Management ownership outlives positions and decision activity
管理权 SHALL 独立于交易周期持续保存，空仓、周期 DONE、人工全平、暂停决策、配置停用、进程重启及 token 到期均不得自动释放。停用或到期 SHALL 继续约束新的调用权限，已授权核验及必要维护 SHALL 保留。年龄、租期、空仓快照或再次发现 SHALL 不构成抢占依据。

#### Scenario: A human flattens and later reopens
- **WHEN** 管理范围先被可靠确认空仓且周期结束，之后人工再次开仓
- **THEN** 原管理者继续管理，核验新周期边界及旧单影响，其他任务不能仅因期间空仓取得范围

#### Scenario: Decisions pause while the process restarts
- **WHEN** 管理任务暂停或停用后进程重启，或者原 MCP token 已到期
- **THEN** 持久化管理者保持，后台继续已授权核验与维护；新请求仍按当前有效凭证、配置及权限判断

### Requirement: Release and transfer are explicit and preserve unsettled resources
释放、任务退役、范围缩小、账户更换或转移管理者 SHALL 为显式行为，核验活跃仓位、订单及未知写入。资源须已妥善结束，或经用户明确交接后由接收方原子接受已追踪资源、未决身份及核验责任；进行中金融 worker 须先停止新提交并保存已知阶段，旧 owner SHALL 被版本隔离。未完成不得静默释放或允许新 owner 抢占；未知写入不得通过交接重发。历史来源、原操作 ID 与首次回执 SHALL 不随交接改写。

#### Scenario: Delete a task with an unknown submission
- **WHEN** 用户申请删除管理任务，但范围仍有未知原提交
- **THEN** 返回具体未决资源并保留原管理权、记录与核验路径，不按等待时长或无可见仓位自动释放

#### Scenario: Transfer a live scope to another authorized task
- **WHEN** 用户明确请求交接，接收管理者授权匹配并接受所有已追踪资源及未决身份
- **THEN** 管理权与维护责任原子转移，不出现同时有效 owner 或未追踪间隙，历史操作和来源保持可查询

#### Scenario: An old worker is still making a financial submission
- **WHEN** 交接时旧 owner 的金融 worker 仍在执行提交阶段，尚不能完整登记其在途身份与阶段
- **THEN** 交接保持待完成，不让新 owner 并发提交；先保存或核验原执行身份并隔离旧 worker，未决交易不得以新 owner 重发

### Requirement: Migration preserves existing owners and historical evidence
升级 SHALL 将已有有效管理权，以及能证明当前活跃资源或未决写入管理者的原周期/显式接管记录映射为原管理者的持久化归属，优先于新后台竞争。仅已结束且无有效认领、活跃资源或未决影响的历史 SHALL 保持历史，不因升级重新取得范围。无既有管理者的配置 SHALL 使用默认自动认领规则。迁移 SHALL 保留历史回执、交易语义、来源和未知提交；多个冲突旧归属 SHALL 明确报告，不按升级扫描覆盖有效管理者。

#### Scenario: Existing owner migrates before a new configuration discovers exposure
- **WHEN** 升级时原配置已有有效管理权或可核验活跃周期，另一配置同时发现该仓位
- **THEN** 保留原 owner，另一配置收到具体竞争结果，既有订单、操作和周期不丢失或重复创建

#### Scenario: Legacy explicit baseline survives the default change
- **WHEN** 旧 strict 配置有显式 adopt 基线和待核验操作
- **THEN** 在原管理者下保留并继续核验，默认规则变更不伪造历史成交、不重发交易、不要求重新 adopt

#### Scenario: Completed history does not claim a new current resource
- **WHEN** 旧任务只有已经结束且无未决影响的历史周期，没有有效管理认领，升级时该范围存在尚未认领的新资源
- **THEN** 历史继续可查询，但不为旧任务自动建立管理优先权；新资源按有效默认配置的首次成功认领处理

### Requirement: Spot and simulated ownership follow their actual inventory boundaries
现货管理范围 SHALL 为物理账户、spot 市场和基础资产，同基础币不同报价交易对共享一个 owner 与库存预留。现货观察基线 SHALL 与历史归属买入分开。MCP paper SHALL 隔离 namespace/user，STRATEGY SHALL 隔离模拟 config；实盘、paper 和独立模拟账户不得互相认领或共享资金。

#### Scenario: Two spot pairs share the same base asset
- **WHEN** 同账户 BTC/USDT 与 BTC/USDC 配置竞争管理实际 BTC 库存
- **THEN** 按同一基础资产范围协调，仅一个 owner 有效，不能通过报价币不同重复认领或重复出售

#### Scenario: The same symbol exists in two paper namespaces
- **WHEN** 不同 paper namespace/user 或 STRATEGY config 持有相同交易标的
- **THEN** 维持各自账户及管理隔离，不把一方仓位、订单、资金或认领用于另一方
