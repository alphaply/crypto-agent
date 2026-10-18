# visible-position-management Specification

## Purpose

使交易 Agent 和 MCP 在可信账户及可见交易授权范围内自动管理实际仓位、库存与订单，无需模型先手工接管或配置有限独占清单，同时通过首次原子认领、持续归属及真实来源审计协调多个管理者。

## Requirements

### Requirement: Management authorization follows trusted visible trading scope
系统 SHALL 全局使用共享管理，从可信任务或 profile 的交易账户、已授权标的、市场类型及调用者权限导出可管理范围。范围内资源 SHALL 不因人工、外部或启动前来源而单独被拒绝，不要求先调用 adopt 或填写额外独占范围。可见性 SHALL 以当前授权范围及核验事实判断，而非缓存、分页遗漏或模型自行提供的账户声明。

#### Scenario: Manage a visible manual position directly
- **WHEN** 可交易任务的账户与标的授权包含一笔已核验人工仓位，即使范围已有其他连接的历史管理记录，Agent 直接请求减仓
- **THEN** 系统在原请求内协调共享资源并核验纳管后继续正常准入，不要求先 adopt、解锁或再次提交相同请求

#### Scenario: Account and symbol boundaries cannot be overridden
- **WHEN** 工具参数或可见缓存引用另一账户、未授权标的或不匹配的市场类型
- **THEN** 系统在交易写入前拒绝，并指出不匹配范围，不扩张任务或 profile 授权

### Requirement: Perpetual scopes follow actual position mode and visible directions
合约管理范围 SHALL 包含可信交易授权内实际可见或合法请求的持仓与订单方向。选定标的的 REAL 任务 SHALL 自动覆盖可见 LONG/SHORT，无需单独填写方向清单。Hedge SHALL 按方向协调；One-way SHALL 对共享净仓使用 BOTH 范围。多个已授权连接可操作相同方向或净仓，内部写入仍按实际共享资源协调。

#### Scenario: Selected task discovers both hedge directions
- **WHEN** 同一任务已授权标的的 Hedge 账户同时存在可见 LONG 与 SHORT 仓位或挂单，且各方向均在可信授权内
- **THEN** 后台核验并共享管理两方向，单侧动作不修改另一侧周期或资源

#### Scenario: One-way opposite requests share ownership
- **WHEN** One-way 账户同一标的已被管理者 A 认领，管理者 B 请求相反方向交易
- **THEN** 按同一 BOTH 净仓范围协调并检查最新数量，不能因管理者不同拒绝，也不能因方向相反绕过金融协调

### Requirement: Unrestricted symbol access produces dynamic resource claims
MCP `symbol_scope=all` SHALL 在已授权市场内按实际发现的仓位、库存、订单或合法请求的目标动态导出共享管理范围，不要求预填最多 30 个标的/方向，也不预占整个市场。标的搜索、行情读取及目录发现 SHALL 不创建管理认领。

#### Scenario: Existing order is discovered outside a preset list
- **WHEN** 可交易 all profile 后台发现已授权市场的一张实际挂单，未配置手填管理标的清单且目标可核验
- **THEN** 系统按该真实资源动态协调、核验并纳管，不因缺少有限清单拒绝

#### Scenario: First authorized entry has no existing exposure
- **WHEN** all profile 合法请求一笔当前无仓位和挂单的标的入场，目标属于当前可信授权
- **THEN** 在本次执行前核验请求目标并协调共享资源，并继续原交易校验，不要求先建立仓位

#### Scenario: Searching markets does not reserve them
- **WHEN** 调用者搜索大量交易标的或读取其行情
- **THEN** 系统只返回授权数据，不对搜索结果或所有市场建立认领

### Requirement: Management identity is shared only within the same trusted owner
任务 SHALL 保留稳定任务身份，live MCP SHALL 保留 profile 与 actor 身份用于来源及审计；同一物理账户内不同已授权任务、profile 与 principal SHALL 可以共享管理资源，不设长期唯一 owner。绑定任务聊天及同 live profile 的多个 principal SHALL 保留原调用身份。paper SHALL 使用可信 namespace/user 账户管理身份，STRATEGY 保持模拟 config 隔离。caller 的模式相应金融权限及聊天审批 SHALL 仍逐项校验，共享管理不授予其他调用者的权限。

#### Scenario: Bound chat manages its task order
- **WHEN** 绑定聊天请求管理同任务已认领订单
- **THEN** 执行既有任务授权及聊天审批，不要求取得长期独占管理权

#### Scenario: Read-only principal shares a writable profile
- **WHEN** 同 profile 的另一 principal 只有 read 权限
- **THEN** 允许其读取授权事实，不因 profile 已有管理权允许其认领、提交、修改或撤销交易

### Requirement: Ownership coordinates the physical trading account
真实管理范围 SHALL 使用可信保存账户身份协调同一物理账户，并包含市场类型与实际共享资源。相同 API 凭证的 profile 别名 SHALL 共享账户范围；同物理账户使用不同 API Key 时 SHALL 通过已确认的保存账户关联协调，不能把 Key 不同视作已证明账户隔离或自动猜测物理账户身份。

#### Scenario: Profiles use aliases of the same credentials
- **WHEN** 两个 profile 通过不同别名使用同一账户凭证并申请重叠资源
- **THEN** 系统识别同一账户范围，允许各已授权调用者共管，按共享物理资源协调写入与预留

#### Scenario: Different keys use one saved physical account
- **WHEN** 已确认属于同一物理账户的不同 API Key 配置关联同一保存账户身份
- **THEN** 重叠管理范围共同协调，不把不同 Key 当成独立资金池，也不因调用者不同拒绝合法动作

### Requirement: Read tools and capability discovery do not claim resources
只读工具、模型目录发现及本地展示 SHALL 不认领、释放或转移管理权，也不触发交易所写入。它们 SHALL 返回实际可见资源、原提交来源、共享管理和能力事实；后台或合法管理请求的资源核验纳管 SHALL 与读取结果区别表达。read-only、尚未启用或仅发现目录的配置 SHALL 不通过发现过程获得金融写权限。

#### Scenario: Query an unclaimed external order
- **WHEN** read-only MCP 调用者读取一张可见但尚未被认领的外部订单
- **THEN** 返回订单与未认领状态，不创建管理者或周期，不提交、修改或撤销订单

#### Scenario: Disabled configuration appears in capability discovery
- **WHEN** 工具或配置查询读取一个暂停/停用的配置及其未认领资源
- **THEN** 查询不使该配置取得新的管理范围；历史来源与未决事实保留，但不构成其他连接的独占阻断

### Requirement: Verified enrollment preserves facts and original identities
在共享授权范围内，系统 SHALL 自动核验资源并按原始执行身份恢复可核验周期；无可恢复历史时 SHALL 记录真实观察基线，保留外部订单身份、参数与来源。纳管 SHALL 不伪造入场成交、操作成功或历史收益；与原未知提交匹配的资源 SHALL 优先恢复原身份，不重复登记或重发。

#### Scenario: Existing position has protective and entry orders
- **WHEN** 启动前已有仓位、止盈止损和限价入场单，当前授权、共享资源协调与事实核验通过
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

### Requirement: Spot and simulated ownership follow their actual inventory boundaries
现货管理范围 SHALL 为物理账户、spot 市场和基础资产，同基础币不同报价交易对共享同一真实库存与预留，不设长期唯一 owner。现货观察基线 SHALL 与历史归属买入分开。MCP paper SHALL 隔离 namespace/user，STRATEGY SHALL 隔离模拟 config；实盘、paper 和独立模拟账户不得互相认领或共享资金。

#### Scenario: Two spot pairs share the same base asset
- **WHEN** 同账户 BTC/USDT 与 BTC/USDC 配置竞争管理实际 BTC 库存
- **THEN** 按同一基础资产范围协调，多个授权调用者均可操作，但不能通过报价币不同复制库存或重复出售

#### Scenario: The same symbol exists in two paper namespaces
- **WHEN** 不同 paper namespace/user 或 STRATEGY config 持有相同交易标的
- **THEN** 维持各自账户及管理隔离，不把一方仓位、订单、资金或认领用于另一方

### Requirement: Authorized callers share account resources globally
系统 SHALL 全局取消持久唯一管理者限制，覆盖默认 visible_first_claim、旧 exclusive_scope、现货、合约、不同 MCP 连接及内置 Agent。当前具备相应可信账户、市场、标的与动作权限的调用者 SHALL 可操作其他连接创建的仓位、订单及保护资源，无需先释放或转移管理权。只读、停用及越界请求 SHALL 继续受原授权约束，Paper namespace/user 和 STRATEGY config SHALL 继续隔离。

#### Scenario: Existing connection ownership does not block a new authorized action
- **WHEN** 资源历史 owner 是 mcp:connection-1791515539173，另一获授权连接或 Agent 对该账户资源请求开仓、平仓、改单、撤单或保护动作
- **THEN** 按当前事实、能力和权限准入，不返回 Another manager owns this account resource 或换名的长期独占错误

#### Scenario: Shared management grants no extra account permissions
- **WHEN** 只读、无相应 trade/cancel、未授权标的或另一账户的调用者提交动作
- **THEN** 仍拒绝，不能通过共管取得金融权限、访问其他用户账户或读取其他 principal 的私有历史回执

### Requirement: Later successful explicit modifications govern shared targets
多个系统内调用者修改同一目标 SHALL 按跨进程执行协调和成功提交顺序处理，后续成功保存的明确修改生效。强制止盈止损模式中，交易所外部人工修改或撤销只更新观察事实，不替换系统保存的目标。后台维护 SHALL 使用当前有效目标版本，不恢复被后续明确修改或撤销取代的旧参数。提交前 SHALL 重核当前仓位、订单及共享预留，保留幂等、未知提交核验和只限制实际依赖的规则。

#### Scenario: Another connection changes a stop loss
- **WHEN** A 设置止损，B 随后通过系统成功保存对该止损的修改，A 的旧后台任务继续运行
- **THEN** B 的成功修改保持有效，旧维护不恢复 A 的参数；若旧 worker 已读取旧版本，提交前检测失效并刷新或停止旧意图

#### Scenario: Concurrent reductions cannot reverse a shared position
- **WHEN** 两个已授权进程同时请求减少同一实际仓位，后提交者观察到数量已经变化
- **THEN** 按最新数量及固定量/全平原语义准入，不重复成交、不超量平仓、不因不同连接绕过版本协调

### Requirement: Migration retires exclusivity without replaying financial effects
升级 SHALL 使现有长期 owner 与旧独占配置不再阻断其他获授权调用者，不要求手动清除此报错连接或先交接。原来源、周期、订单、预留、操作身份及未知提交 SHALL 保留并继续按原身份核验；共享资源及后台维护 SHALL 去重协调，不能为每个连接复制仓位、预留、保护意图或历史成交。

#### Scenario: Historical owner survives as audit metadata only
- **WHEN** 升级前已有有效认领、旧 exclusive_scope、活跃保护计划或未决订单
- **THEN** 迁移后其他获授权连接可以提交合法动作，旧事实仍可审计，未决写入按原身份恢复，不撤单、不重发、不丢失核验责任

### Requirement: All supported historical policies normalize to shared management
已有 strict_owned、exclusive_scope 和 visible_first_claim 配置 SHALL 统一使用当前授权范围共同管理；原始创建者、订单、成交、预算和未知执行身份保留。旧工具入口 SHALL 保留并复用统一内部检查和执行服务。

#### Scenario: Human modifies a position under a historical policy
- **WHEN** 旧配置升级后发生人工加减仓，当前事实可核验且动作获授权
- **THEN** 不因旧策略名称拒绝后续合法管理，不伪造人工成交为 Agent 成交

### Requirement: Attached required protection follows the saved system target
强制止盈止损 SHALL 按系统最新成功保存的目标维护。Agent 与已登录前端均可调整；人工无需二次密码。交易所外部改动不得替换系统目标。独立退出 SHALL 观察外部事实并交给后续 Agent 决策，不自动生成交易意图。

#### Scenario: External cancellation does not clear a required target
- **WHEN** 强制模式仍保存有效止损目标，但交易所上对应保护被人工取消
- **THEN** 后台按最新系统目标维护；后续 Agent 或前端保存的新目标取代旧目标
