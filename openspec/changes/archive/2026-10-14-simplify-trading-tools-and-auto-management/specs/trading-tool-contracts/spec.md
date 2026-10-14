## MODIFIED Requirements

### Requirement: Canonical independent trading actions
系统 SHALL 保留独立退出模式的 `open`、`close`、`edit`、`cancel` 四种规范动作及受原权限约束的非默认兼容入口；默认模型写入入口 SHALL 为 `execute_trade_actions`，单笔交易使用一项动作数组。open SHALL 使用 side=LONG|SHORT、有限正数 amount（基础币）、有限正数 price 和 reason 表达既有实盘限价入场。单独兼容调用与对应版本的批处理 SHALL 使用相同交易字段与校验；MCP 显式提供 profile、symbol 和 operation_id，内置 Agent 由可信运行上下文提供对应范围与操作身份。

#### Scenario: Repeated entry calls
- **WHEN** Agent 在同一轮使用不同操作身份提交两笔相同方向、不同数量或价格的 `open`
- **THEN** 系统分别处理两笔限价入场，不因同轮、同方向或价格相近而合并为一笔

#### Scenario: Context cannot be overridden
- **WHEN** 工具参数包含与已授权任务、账户或标的不一致的上下文
- **THEN** 系统在交易写入前拒绝该请求

#### Scenario: Default writer contains a single action
- **WHEN** 内置交易 Agent 或绑定任务聊天仅需撤销一笔订单
- **THEN** 模型通过 `execute_trade_actions` 的一项 cancel 动作表达请求，保留订单身份、原因和真实结果，不要求目录同时公布单项 cancel 工具

### Requirement: Edit means an explicit change to an existing order
`edit` SHALL 按返回的 order_id 修改当前管理者已获管理权的未完成订单的适用价格或 remaining_amount，至少一项必填；人工或外部创建来源 SHALL NOT 单独阻止已纳管目标的修改。remaining_amount SHALL 是有限正数的目标未成交基础币数量，拒绝布尔和零值。省略字段 SHALL 保持执行前已核验的当前值，包括已确认的人工调整；方向、订单类型、fixed/all 模式不能隐式改变；all 订单不接受 remaining_amount。回执 SHALL 表明修改方式及当前有效身份。旧入口的总量语义 SHALL 保持兼容。

#### Scenario: Partially filled entry modification
- **WHEN** 总量 1、已成交 0.2 的入场单被 edit 为 remaining_amount=0.5
- **THEN** 在没有新增成交的情况下，修改后的总量为 0.7、剩余量为 0.5

#### Scenario: Conditional replacement
- **WHEN** 修改条件退出单的触发价需要撤旧建新
- **THEN** 回执提供 edit_method=cancel_replace、旧身份和当前有效身份，不把旧 ID 继续标记为活跃单

#### Scenario: Dynamic all price edit
- **WHEN** Agent 只修改 all 条件单的 trigger_price
- **THEN** 修改后的订单仍为 all，不将当前仓位估值固化成数量

#### Scenario: Fields are valid for the current phase
- **WHEN** edit 对普通限价或已触发的限价子单提供 trigger_price，或试图修改市价订单
- **THEN** 系统拒绝不适用的修改；普通限价子单只允许 price 和 remaining_amount，未触发条件单按其类型接受价格字段

#### Scenario: Omitted field preserves a verified manual change
- **WHEN** 人工已将纳管挂单的限价从 100 改为 101，随后 v3 edit 仅提供 remaining_amount=0.5
- **THEN** 系统核验该目标后保留当前限价 101，不从旧本地参数恢复 100，并按最新成交事实解释目标剩余数量

### Requirement: Edit authorization follows the owned target
系统 SHALL 根据已核验管理权和订单作用判断 edit 属于入场还是退出；已按可见授权范围纳管的人工或外部订单 SHALL 接受相同的目标级校验。入场 edit SHALL 保留原有增仓能力、额度、杠杆和可交易标的检查；退出 edit SHALL 要求 trade 权限。edit 的权限包含完成同一目标修改所必需的撤换，不额外要求 cancel scope；live MCP 显式 cancel 仍要求 cancel 权限，paper 金融动作沿用 read 与 trade，内置任务沿用可信交易授权和审批。单项兼容调用、批次和兼容入口 SHALL 一致，不能借管理来源或工具改名绕过实际权限。

#### Scenario: Entry cannot be disguised as an exit edit
- **WHEN** 调用方用统一 edit 修改一张入场单，但当前配置不允许新增该敞口
- **THEN** 修改被拒绝，不因工具名称不再包含 entry 而绕过额度或能力检查

#### Scenario: Authorized edit uses cancel and replace
- **WHEN** 调用方有 trade 权限而无 cancel scope，修改本任务的未触发条件单
- **THEN** 系统可执行同一目标的必要撤换，但不能借此撤销其他订单或执行显式 cancel 动作

#### Scenario: External creation is not a separate edit denial
- **WHEN** 当前任务已取得授权范围的管理权，范围内人工限价入场单已核验且新的 edit 满足权限、数量和敞口条件
- **THEN** 系统按真实入场目标执行修改，不要求先 adopt，也不因该订单没有 Agent 创建记录而拒绝

### Requirement: Cancel uses owned order identity
`cancel` SHALL 撤销由当前管理者管理的指定入场或退出挂单，不将撤单报告为平仓。默认管理规则 SHALL 允许金融调用在可见授权范围内原子取得管理权并核验人工或外部目标，无需额外 adopt；来源本身不扩大授权范围，也不允许抢占其他管理者。系统 SHALL 根据已核验订单身份识别普通/条件单，向调用方返回当前有效 order_id；旧裸 ID 若存在歧义 SHALL 拒绝并要求使用查询返回的明确身份。

#### Scenario: Cancel conditional exit
- **WHEN** 调用方将查询回执中的条件单 order_id 传给 cancel
- **THEN** 系统取消该条件单并报告核验状态，不尝试把同数字普通订单当作目标

#### Scenario: Trigger precedes cancellation
- **WHEN** cancel 使用的母单身份已触发并关联一张仍活跃的限价子单
- **THEN** 系统在核实归属和关联后取消该子单未成交部分，返回当前身份与实际成交；关联不明时返回待核验，不能仅凭母单终态声称整个退出已取消

#### Scenario: Cancel a human-created order in an exclusive scope
- **WHEN** 有 cancel 权限的管理者请求撤销独占范围内一张已核验外部单
- **THEN** 原调用完成本地纳管、目标核验及撤单，保存阶段和结果，不因不在原 Agent 开单记录中而直接拒绝

#### Scenario: First financial use can cancel a manual order
- **WHEN** 有 cancel 权限的 live MCP 首次请求撤销可见授权范围内的人工挂单，该范围尚无管理者且目标证据完整
- **THEN** 系统原子取得该范围管理权后撤销已核验目标，保留人工来源和真实成交证据，不额外要求 trade 权限、先 adopt 或先查询解锁

#### Scenario: Visibility does not defeat an existing manager
- **WHEN** 调用方可以查询一笔订单，但其管理范围已有另一任务或 profile 的有效管理者
- **THEN** cancel 返回具体管理竞争原因和未执行状态，不因目录中的可见身份而抢占管理权或撤销目标

### Requirement: Ordered batch uses the same action contracts
`execute_trade_actions` SHALL 以 contract_version=3 提供默认统一批次，以 contract_version=2 保留独立退出规范批次，均使用 action 判别的平铺动作、每批 1 至 20 项。无版本批次 SHALL 按旧契约解析；混合格式在写入前拒绝。整批静态、权限及已知模式检查 SHALL 在首次提交前完成，执行 SHALL 按顺序逐项重新准入，已执行结果不回滚。省略 execution_policy 或指定 stop_on_error 时，failed、pending、unknown 或被阻塞项 SHALL 停止后续交易项并标为 not_executed。支持的显式 independent 策略 SHALL 只跳过依赖未满足或准入失败的项，继续评估其他项；依赖和策略 SHALL 持久化为请求意图。提交入场不等于等待成交；已确认操作附带清理告警不得代替下一动作的实际准入。

#### Scenario: Entry is still unfilled
- **WHEN** 默认停止策略的批次先提交限价 open，后续 close 所需的已成交仓位仍不存在
- **THEN** close 不预挂为未来仓位的退出单，报告失败；后续动作标记 not_executed，之前入场回执保留

#### Scenario: Cancellation permissions in a mixed batch
- **WHEN** live MCP 批次包含 open 与 cancel
- **THEN** 系统在首次提交前同时校验 trade 与 cancel 权限；纯撤单批次使用 cancel 权限

#### Scenario: Paper cancellation retains its existing financial permission
- **WHEN** paper 批次包含撤单或其他金融动作
- **THEN** 使用该 paper namespace/user 的 read 与 trade 权限校验，不额外要求 live cancel scope；内置任务仍使用可信运行授权与审批

#### Scenario: Flat legacy cancellation is not a version signal
- **WHEN** 无版本的旧批次只包含平铺 cancel 动作
- **THEN** 仍按旧契约解析和重放；显式 v2 批次缺少新字段时不得回退为旧格式

#### Scenario: Cleanup completes before the next entry
- **WHEN** 前项市价平仓已成交，执行下一笔新入场前已确认旧周期归零且所有残留订单清理完成
- **THEN** 下一项通过正常权限与敞口检查后执行，不因历史 unknown 文本继续阻塞

#### Scenario: Old close-all order can affect a new entry
- **WHEN** 默认停止策略中平仓本身已确认，但旧周期的条件 all 是否仍活跃未核实，后项请求同方向重新开仓
- **THEN** 后项及之后交易项标记 not_executed，指出旧单身份和需完成的核验，不伪称本次平仓未成交

#### Scenario: A rejected take profit does not suppress independent protection
- **WHEN** independent 批次的止盈被确认拒绝且无执行影响，后项止损无显式前置依赖，且当前仓位和相关订单证据满足准入
- **THEN** 后项止损执行，分别保留止盈失败与止损真实结果，不将整批描述为全部成功

#### Scenario: Explicit dependency preserves cancel-then-create intent
- **WHEN** independent 批次中新建项以 depends_on 引用前面的撤单项，而撤单失败、待核验或尚未 completed
- **THEN** 新建项标为 not_executed 并指出依赖项，其他独立项继续评估；不能因静态参数有效而提前建单

#### Scenario: Invalid policy or dependencies fail before any writes
- **WHEN** 策略不支持、旧版本携带新策略、depends_on 含非整数/布尔值、重复/越界/自身/后向索引，或在 stop_on_error 策略中使用 depends_on
- **THEN** 整批在首次写入前拒绝；independent 的依赖只允许引用索引更小的项并要求其 completed

#### Scenario: Unknown execution still blocks dependent actions in independent mode
- **WHEN** independent 批次前项平仓结果未知，后项需要同方向剩余仓位，即使没有声明 depends_on
- **THEN** 后项仍因实际执行依赖标为 not_executed；independent 不能绕过正常准入

#### Scenario: Replay never resumes skipped tail
- **WHEN** 同一批次操作 ID 在恢复后重放
- **THEN** 仅刷新已执行项证据，原 not_executed 保持未执行，不补发任何尾部；变更策略或依赖复用同 ID 被拒绝

#### Scenario: Third-version mixed actions retain caller order
- **WHEN** 合法 v3 请求按 cancel、edit、open 顺序提交三项动作
- **THEN** 系统按给定数组顺序执行并返回三项对应结果，不自动重排、不并行金融写入，也不要求模型在每项之间再发一次调用

### Requirement: Legacy contracts retain their original meanings
系统 SHALL 通过明确的旧入口、旧批次格式或持久化版本识别历史契约，不依据缺少价格字段猜测版本。旧 take_profit_limit SHALL 保持普通 LIMIT 含义，旧 amount=0 SHALL 保持当时全量快照语义，旧入场 edit amount SHALL 保持含已成交的总量。默认模型目录 SHALL 推荐 v3 顺序入口；旧单项工具、v2 批次及旧 execute_trade 分发器 SHALL 保留原权限与参数约束的非默认兼容调用，隐藏入口不得改变原请求、哈希、版本或恢复身份。

#### Scenario: Historical limit exit remains a limit
- **WHEN** 升级后读取无版本的旧 take_profit_limit 记录或收到旧 close_perpetual 的同名参数
- **THEN** 系统将其作为普通限价单继续查询、撤销和管理，不要求触发价也不创建条件单

#### Scenario: New take profit limit does not fall back
- **WHEN** 新 close 使用 order_type=take_profit_limit 但缺少 trigger_price
- **THEN** 系统拒绝请求，不将它推断为旧普通限价行为

#### Scenario: Persisted interpretation survives restart
- **WHEN** 新订单、待执行请求或回执被持久化后进程重启
- **THEN** 其契约版本和语义保持不变，无版本旧记录继续按 legacy 解释；旧 amount=0 的已解析快照不重新取当前仓位

#### Scenario: Hidden legacy dispatcher is not a new batch identity
- **WHEN** 旧客户端仍用 execute_trade 指定旧工具名和原参数，并重放已有 operation_id
- **THEN** 系统按持久化旧请求与原契约返回或核验原结果，不把单项分发器静默转换为 v3 批次，不新增父子操作或再次提交

### Requirement: Descriptions match mode and execution capability
工具说明 SHALL 按用途、必填与互斥参数、方向及数量单位、示例、回执解释提供紧凑说明，并与当前身份和模式的实际 schema 一致。默认说明 SHALL 使用 execute_trade_actions 的单项或多项示例；独立退出说明 SHALL 包含多次开仓、分批止盈、分批止损、最后全平及 edit 示例。未支持的参数不得被静默忽略；整仓附带保护规则不得混入独立模式说明。默认自动纳管说明 SHALL 区分管理范围和真实管理竞争，不要求先 adopt，也不以人工来源统一宣告不可管理。

#### Scenario: Example and schema agreement
- **WHEN** 使用工具说明提供的独立退出示例验证请求
- **THEN** 每个示例均符合公布 schema，退出示例包含明确持仓方向与数量选择，不把提交状态写成成交

#### Scenario: Attached and spot capabilities are described separately
- **WHEN** 调用者分别取得附带保护合约、独立退出合约及现货的 v3 schema 与说明
- **THEN** 各自只展示该模式支持的参数和 protect/close 组合；现货不出现合约 SHORT 或杠杆退出语义，独立退出不宣称整仓 protect 可用

#### Scenario: Automatic management does not ask for adoption first
- **WHEN** 默认 Agent 或 MCP 目录描述可见授权范围内的人工持仓和挂单管理
- **THEN** 说明允许直接提交金融动作并在执行前核验及认领，删除无条件先 adopt 的指导；有其他管理者、目标事实变化或未决依赖时返回对应具体原因

### Requirement: Canonical tool turns continue only independently admissible intent
保留的独立退出规范单项兼容调用 SHALL 继续原 Agent/Chat 工具轮次行为：按顺序逐项核验，不仅依据前项 failed/pending/unknown 停止全部调用。满足自身必要条件的已请求动作 SHALL 继续；仅未满足的明确前置依赖、具体硬约束或本次必需但仍未核实的执行事实可以阻止对应项，不得猜测业务顺序而停止。默认 v3 批次 SHALL 遵守其显式批次策略，省略策略采用 stop_on_error，不继承兼容多工具轮次的隐含 independent 行为。聊天金融写入 SHALL 保留逐项审批；用户拒绝审批 SHALL 停止该轮后续金融写入。非独立模式及旧工具的停止语义 SHALL 保持兼容。

#### Scenario: Protection survives an unrelated definite failure
- **WHEN** 本轮规范工具中的止盈明确未提交，随后请求针对已核验仓位的独立止损，相关执行证据满足准入
- **THEN** Agent 继续该止损；Chat 在该止损获得既有审批后执行，保留逐项结果

#### Scenario: Failure does not silently reverse a sequential intention
- **WHEN** 前项平仓或撤单失败，后项有明确声明的成功前置条件，或已有持久化撤建/实际仓位事实证明该依赖尚未满足
- **THEN** 仅阻止受该具体依赖影响的项并说明原因，不因调用顺序猜测依赖并停止其他有效动作

#### Scenario: Definite failure does not imply a hidden sequence dependency
- **WHEN** 前项确认未执行，后项是独立请求的同方向交易，未声明前置依赖且其权限、敞口、目标及必要执行证据均满足
- **THEN** 后项正常执行，不因同方向或前项失败要求模型下一轮重复提出请求

#### Scenario: User rejection is not an exchange failure
- **WHEN** 用户拒绝某个聊天交易调用的审批
- **THEN** 本轮后续金融写入保持未执行，合法只读核验仍可继续，不把 independent 解释为继续请求或执行被拒绝链路

#### Scenario: Unsupported tool effect cannot be assumed independent
- **WHEN** 当前轮包含无法识别作用范围的旧工具或异常结果
- **THEN** 对无法证明独立性的后续写入保留停止语义，同时允许授权的只读查询

#### Scenario: Batch approval retains action granularity
- **WHEN** 绑定任务聊天以一个 v3 批次表达多项金融动作
- **THEN** 审批仍能逐项识别和决定动作，用户拒绝后其余金融动作不执行；一次工具调用不将只读权限或首项审批扩大为整批写入批准

## ADDED Requirements

### Requirement: Unified third-version trading envelope uses trusted context
系统 SHALL 为 REAL、STRATEGY、SPOT_DCA 及 MCP paper 提供 contract_version=3 的 execute_trade_actions，包含 1 至 20 项平铺动作。执行引擎、账户及管理者 SHALL 由可信任务/profile 和调用身份推导，一个父操作 SHALL 只属于一个管理者及一个账户/profile 或 paper namespace/user。动作标的 SHALL 限于既有任务/profile 授权范围；客户端 SHALL NOT 用 live/paper 标志、其他管理者或账户参数改变执行上下文。

#### Scenario: Live key cannot select the paper engine
- **WHEN** 实盘 profile 的 v3 请求携带 paper=true 或与可信账户冲突的引擎字段
- **THEN** 整批在金融写入前返回参数或权限错误，不切换到 paper，也不静默丢弃上下文覆盖字段

#### Scenario: Paper key remains isolated
- **WHEN** paper 父请求已经明确合法目标 user，某动作又试图指定实盘账户或覆盖为另一个 paper user
- **THEN** 合法动作只能作用于其原 namespace/user 账户，覆盖其他账户的整批请求被拒绝，不能因入口统一而获得实盘权限

#### Scenario: Multi-symbol spot preserves one task budget
- **WHEN** 多标的 SPOT_DCA 任务的一次 v3 批次对两个已配置交易对提交买入动作
- **THEN** 每项明确其受授权标的，共享原任务和当期预算及资产预留边界；不会因拆成子项而每项重新获得完整预算，未指定且无法唯一确定标的的项在写入前拒绝

#### Scenario: A batch cannot span different managers
- **WHEN** 同一 v3 请求试图把不同 profile、任务或 paper user 的金融动作放入同一数组
- **THEN** 系统在首次金融写入前拒绝，不能通过每项自带上下文实现跨账户或跨管理者批次

### Requirement: Third-version actions preserve mode-dependent financial capabilities
v3 SHALL 使用 open、close、edit、cancel、protect 作为模式相关动作，并公布实际支持的订单类型、参数、单位与互斥条件。amount 与 edit.remaining_amount SHALL 使用基础币数量，后者表示目标未成交量；原引擎支持的入场 quote_amount/margin_amount SHALL 保留明确单位及互斥 sizing。合约 open/close 的 side SHALL 为 LONG/SHORT；现货 open LONG SHALL 买入、close LONG SHALL 卖出。实盘既有限价入场、附带 TP/SL、现货原生退出/OCO 及 paper 既有市价、限价、部分/全平能力 SHALL 保留，不增加交易所未支持组合。

#### Scenario: Live opening remains limit-only
- **WHEN** REAL 合约 v3 open 请求 order_type=market，而该 profile 的既有入场能力仅支持限价
- **THEN** 系统在首次写入前拒绝不支持组合并指明支持的限价字段，不因 paper 支持市价入场而放宽实盘能力

#### Scenario: Paper opening accepts its existing market capability
- **WHEN** paper 合约 v3 open 使用 LONG、基础币数量与受支持的 market 类型
- **THEN** 系统按 paper 既有真实公开行情、费用及模拟成交规则处理，不强制转换为限价挂单或引入实盘下单

#### Scenario: Spot direction has no short-position interpretation
- **WHEN** spot v3 open 使用 LONG，随后 close 使用 LONG 和明确数量及受支持的退出类型
- **THEN** 前者买入基础币、后者卖出可管理数量，不建立合约多仓或空仓；现货 SHORT 在整批写入前拒绝

#### Scenario: Attached entry keeps its configured protection policy
- **WHEN** attached_required 合约模式的 v3 open 缺少 stop_loss 或 take_profit
- **THEN** 整批在首次写入前返回必填字段错误；合法附带入场及 attached_optional 的省略语义保持原能力，不将附带保护解释为独立退出单

#### Scenario: Fixed and all quantities retain their modes
- **WHEN** v3 edit 仅改价，或现有 fixed 退出数量与新仓位数量发生冲突
- **THEN** all 仍保持 all，fixed 仍保持请求的固定数量；系统不自动按仓位缩量、补量或转换模式，实际冲突通过对应动作的明确结果报告

### Requirement: Protect is exposed only for existing protection mechanisms
v3 protect SHALL 仅在模式实际支持时表达整仓附带保护、现货原生 OCO/保护退出或 paper 既有单腿/成对保护，并保留各机制的目标、数量、价格、关联及取消语义。paper 入场可选 TP/SL、单独一腿、固定部分量、当前周期 all 和省略已设置腿的保持语义 SHALL 保留。现货保护 SHALL 使用可管理余额与共享预留约束；已设置的成对保护 SHALL 不被计为两份可售库存。实盘独立退出 SHALL 使用 close 创建独立退出订单，不公布或接受 protect 整仓附带保护。

#### Scenario: Spot native OCO preserves one reservation
- **WHEN** 支持原生 OCO 的现货任务通过 v3 protect 为同一数量设置止盈和止损
- **THEN** 系统保留原生关联、触发价及必要执行价，数量只预留一次；不猜测缺失价格，也不以两张无关联卖单替代不受支持的原生组合

#### Scenario: Paper paired protection uses its own order identities
- **WHEN** paper v3 protect 设置其既有成对止盈止损
- **THEN** 保护仍属于当前 namespace/user 和已核验持仓，返回可追踪的保护/订单身份及关联状态，不使用实盘 OCO 能力作为 paper 实现前提

#### Scenario: Independent live protection is represented as close
- **WHEN** REAL independent_exits 的 v3 请求包含 protect 动作
- **THEN** 整批在首次写入前拒绝 unsupported_combination，公布以对应 close 订单类型设置独立 TP/SL 的方式，不自动猜测转换

#### Scenario: Paper changes one protection leg while keeping the other
- **WHEN** paper 仓位已经有 TP/SL，v3 protect 只提供新的 stop_loss，或新仓只请求单独一腿
- **THEN** 省略已设置的 take_profit 保持原值，单独一腿合法；部分数量及周期 all 语义仍按原规则，不强制同时填写 TP 和 SL

#### Scenario: Paper entry carries optional protection
- **WHEN** paper v3 open 提供既有支持的可选 stop_loss 或 take_profit
- **THEN** 保留原入场附带保护行为，市价成交后设置、限价成交后激活，不将省略保护视为缺参，也不提前声称限价已经成交

### Requirement: Batch preflight separates static authorization from dynamic admission
v3 SHALL 在首项金融提交前校验整批结构、版本、权限、标的授权、已知模式能力及依赖合法性；未知参数或不支持组合 SHALL 整批拒绝。依赖当前余额、仓位、挂单、数量、杠杆或执行事实的准入 SHALL 在各项实际执行前重新核验。后项尚不满足动态条件 SHALL NOT 预先阻止合法前置 cancel。纯撤单 SHALL 不附加开仓额度或杠杆要求；live MCP 使用 cancel 权限，paper 沿用 read 与 trade，内置任务沿用可信交易授权。

#### Scenario: Cancel releases resources before opening admission
- **WHEN** 合法 v3 批次先 cancel 一笔占用资源的挂单，后 open 的动态余额或敞口在撤单确认前不足
- **THEN** 系统在整批结构和 trade/cancel 权限通过后先核验并执行 cancel，再根据更新事实判断 open；不以撤单前的开仓限制拒绝整批

#### Scenario: Later permission failure rejects every item
- **WHEN** 批次第一项为合法 cancel，后项 open 缺少 trade 权限
- **THEN** 整批在第一项撤单之前拒绝，两项均未执行；动态准入分阶段不允许绕过权限预检

#### Scenario: Invalid later static combination causes no early write
- **WHEN** v3 首项 cancel 合法，后项存在不受支持的订单类型、未知字段或范围外标的
- **THEN** 系统报告对应动作索引及错误，所有项均未提交，不先撤单再发现静态非法请求

#### Scenario: Pure cancellation survives an opening gate
- **WHEN** 有 cancel 权限的 live MCP 请求仅撤销可管理目标，而账户开仓杠杆或新增敞口条件未满足
- **THEN** 系统按真实撤单目标和管理权核验后处理，不因无关开仓条件要求 trade 权限、改杠杆或新增预检调用

### Requirement: Financial actions establish management without a preliminary adoption tool
默认金融动作 SHALL 在当前可见交易授权范围内核验目标管理权，并在无管理者时按原子先到先得规则取得管理权；绑定任务聊天及同 profile principal SHALL 沿用同一管理者身份。人工或外部来源和尚无交易周期 SHALL NOT 单独阻止已获权管理；来源与真实基线 SHALL 保留。只读查询 SHALL NOT 认领或抢占范围，读权限 SHALL NOT 获得金融权限。

#### Scenario: Visible manual position is managed in the same action
- **WHEN** 获授权任务直接请求 close 范围内人工已有仓位，管理权可取得且执行前事实和数量完整
- **THEN** 系统在本次调用内登记管理基线并执行合法退出，不要求模型先 adopt 或下一轮重试；人工仓位不被写成 Agent 入场成交

#### Scenario: Read-only discovery does not win ownership
- **WHEN** read principal 查询尚无管理者的可见人工仓位或订单
- **THEN** 查询只返回观察事实与管理状态，不登记管理者、不提交金融动作，也不使后续合法金融调用被该读者占用阻止

#### Scenario: Same profile principals share management but not private receipts
- **WHEN** 两个已授权 principal 通过同一 profile 对其管理范围提交金融动作
- **THEN** 管理者身份为该 profile，动作按共享账户资源协调；各 principal 仍遵守原权限及操作回执访问范围，不因同管理者而公开另一调用者的私有操作身份

### Requirement: Third-version identities are stable and version-specific
v3 SHALL 在金融提交前持久化父操作和各子项的原始版本化意图、策略、依赖与稳定身份。同身份同意图重放 SHALL 返回或核验原结果而不重复写入；不同意图、上下文、版本、策略或依赖复用身份 SHALL 拒绝。v1、无版本旧请求及 v2 的既有哈希和省略字段 SHALL 保持不变；入口隐藏或别名 SHALL NOT 伪造等价请求、生成新操作或改变已确认解释。

#### Scenario: Third-version replay preserves partial results
- **WHEN** v3 批次已有首项 completed、第二项 unknown、第三项 not_executed，重启后同身份同意图重放
- **THEN** 返回或只读核验原父子结果，已完成项不重复写入、未知项不盲重发、第三项不补执行，批次不被报告为全部成功

#### Scenario: A version change cannot reuse an old operation
- **WHEN** 调用者用已有 v2 operation_id 提交看似等价的 v3 参数
- **THEN** 系统拒绝身份与版本化意图冲突，不假装两种序列化身份等价，也不创建第二笔金融写入

#### Scenario: Legacy omitted policy retains its exact identity
- **WHEN** 升级后重放省略 execution_policy 的 v2 或无版本旧请求
- **THEN** 命中原请求身份并保持原省略及停止语义，不补默认字段改变哈希，不因 v3 默认入口而转换原请求

### Requirement: Ordered execution remains a bounded foreground request
v3 SHALL 在一次有界调用中逐项执行已声明动作，默认失败、pending、unknown 或未满足依赖后停止并保留未执行尾部；已执行结果 SHALL 不回滚。显式 independent SHALL 仅按已公布能力接受严格的此前 completed 索引依赖，submitted 不满足依赖。系统 SHALL NOT 等待入场未来成交、解释动态前项结果引用、启动后台交易计划、后台续跑未执行尾部或为重试生成新身份。

#### Scenario: A failed second item does not erase the first
- **WHEN** v3 首项撤单 completed、第二项新建被明确拒绝、第三项尚未尝试
- **THEN** 批次保留首项 completed、第二项 failed 和第三项 not_executed 的真实结果；总体失败不抹去已成功撤单，不重发前项或隐式回滚

#### Scenario: Submitted entry does not satisfy a close dependency
- **WHEN** 显式 independent 批次中的限价 open 为 submitted，后项 close 通过 depends_on 引用该项
- **THEN** 后项标为 dependency_not_completed 且不执行；调用返回当前结果，不等待成交或将 close 注册为后台未来计划

#### Scenario: A dynamic result reference is rejected before trading
- **WHEN** v3 动作以动态表达式引用前项生成的 order_id、成交量或账户字段
- **THEN** 系统在首次金融写入前拒绝不支持的参数形式，不解释任意代码或隐式结果替换

#### Scenario: Recovery does not issue an unattempted tail
- **WHEN** 网络中断或进程崩溃后恢复 v3 父操作证据，原批次部分子项未执行
- **THEN** 恢复仅核验原已提交身份和事实，保留未执行项；新的管理意图须有明确新请求，不能由后台恢复自动续跑尾部

### Requirement: Compact model receipts retain actionable execution evidence
默认模型回执 SHALL 返回精简逐项投影，保留父子操作身份、动作索引、status 与 order_status、当前/旧订单及客户端身份、数量模式、请求/已知成交/剩余数量、修改方式、错误码、失败阶段、具体依赖阻塞和可访问恢复路径。完整原始回执及后续核验证据 SHALL 持久化且可按原身份查询；投影 SHALL NOT 把 submitted 当成交、隐藏部分成功或泄露其他 principal 的操作身份。

#### Scenario: Compact timeout retains a recovery target
- **WHEN** v3 子项提交后超时且已有客户端身份、尚无交易所订单 ID
- **THEN** 精简回执保留父子 operation_id、客户端身份、unknown/pending 的真实区别及可用操作查询路径，不只返回一个失败字符串或空 order_id

#### Scenario: Compact edit keeps replacement outcomes separate
- **WHEN** edit 已确认撤旧，但替代单被拒绝
- **THEN** 精简回执保留旧身份、当前有效身份或替代客户端身份、edit_method、明确替代失败及其错误码，不因旧单 canceled 宣告 edit completed

#### Scenario: Full evidence remains accessible after projection
- **WHEN** Agent 或授权 MCP 调用者按原操作身份请求详细核验
- **THEN** 返回授权范围内的完整持久化回执及最新证据和原观察时间；精简投影不删除原请求、成交事实、失败阶段或历史恢复身份

### Requirement: Batch account summaries are optional and non-repetitive
默认逐项回执 SHALL NOT 在每个结果重复完整余额、仓位、挂单或账户快照。请求需要账户摘要时，批次 SHALL 在结尾最多附一次当前可取得的摘要，标明账户、标的范围、观察时间和读取完整性。摘要读取失败 SHALL 保留各项金融执行结果并单独报告，不能将已确认成交降级为未知，也不能声称汇总时间就是所有证据的新观察时间。

#### Scenario: Three actions produce at most one account summary
- **WHEN** v3 批次完成三项动作并请求账户摘要
- **THEN** 返回三项精简真实结果和结尾至多一份摘要，每项不重复账户列表；摘要只覆盖授权范围且不伪造缺失余额或仓位

#### Scenario: Summary read failure does not rewrite a completed trade
- **WHEN** 最后一项退出已核验成交，但结尾账户摘要读取失败
- **THEN** 退出仍保留 completed、实际成交量和订单身份，摘要单独标识失败或缺项，并提供按需核验方式

### Requirement: Explicit handoff grants only resource-scoped recovery access
经明确交接并原子接受的未决资源 SHALL 向接收管理者提供仅用于这些资源的只读恢复授权与可访问查询身份。原请求、actor、操作 ID 和证据 SHALL 保留；非交接资源及原 principal 的其他私有回执 SHALL 不因管理权转移公开。查询 SHALL 只核验原操作，不能在接收方身份下重发、恢复未执行尾项或提交新交易。

#### Scenario: The receiver inspects an accepted unresolved operation
- **WHEN** 管理者 B 明确接受 A 的范围和已登记未决订单/操作，并在自己的授权上下文使用交接给出的恢复身份查询
- **THEN** 能查看该资源所需原执行阶段、订单身份和核验结果，保留原 actor 和操作身份；其他 A 的私有历史仍不可见，查询没有金融写入

#### Scenario: A former owner retries after ownership changes
- **WHEN** A 的旧客户端重放已交接的原操作，或旧 worker 返回迟到 ACK
- **THEN** 只保留或核验原已开始执行事实，不获得新提交权、不改变历史身份，也不重发原交易或批次尾项
