# trading-tool-contracts Specification

## Purpose

为内置 Agent、模拟交易和外部 MCP 提供一致、易于正确调用的独立合约交易工具，使调用方能表达开仓、退出、改单与撤单，并依据明确的版本、数量语义和真实回执继续管理订单。

## Requirements

### Requirement: Canonical independent trading actions
系统 SHALL 为独立退出模式提供 `open`、`close`、`edit`、`cancel` 四种规范动作。open SHALL 使用 side=LONG|SHORT、有限正数 amount（基础币）、有限正数 price 和 reason 表达限价入场。单独调用与批处理 SHALL 使用相同交易字段与校验；MCP 显式提供 profile、symbol 和 operation_id，内置 Agent 由可信运行上下文提供对应范围与操作身份。

#### Scenario: Repeated entry calls
- **WHEN** Agent 在同一轮使用不同操作身份提交两笔相同方向、不同数量或价格的 `open`
- **THEN** 系统分别处理两笔限价入场，不因同轮、同方向或价格相近而合并为一笔

#### Scenario: Context cannot be overridden
- **WHEN** 工具参数包含与已授权任务、账户或标的不一致的上下文
- **THEN** 系统在交易写入前拒绝该请求

### Requirement: Explicit close order types and price fields
`close` SHALL 使用 `side=LONG|SHORT` 表示被减仓的持仓方向，并以 `order_type` 区分 `market`、`limit`、`take_profit_market`、`take_profit_limit`、`stop_market`、`stop_limit`。普通市价不接受任何价格；普通限价仅使用 price；条件市价仅使用 trigger_price；条件限价同时要求 price 与 trigger_price。

#### Scenario: Ordinary limit versus conditional take profit
- **WHEN** 分别提交 `limit` 加 price 和 `take_profit_limit` 加 price、trigger_price
- **THEN** 前者直接挂限价退出，后者等待止盈条件触发后提交限价退出

#### Scenario: Missing or conflicting price fields
- **WHEN** 条件限价缺少触发价，或普通限价同时提供触发价
- **THEN** 请求在提交交易所之前失败，并返回对应字段错误

### Requirement: Explicit quantity selection
新 `close` 契约 SHALL 恰好接受一种数量选择：有限正数 `amount` 或 `close_all=true`。amount SHALL 使用基础币数量，不是保证金、计价币金额或合约张数。零值、布尔数量、两种选择同时出现、两种选择都缺失以及 close_all=false SHALL 被拒绝；close_all 的可用订单类型与执行含义 SHALL 明示。

#### Scenario: Fixed stage and final exit
- **WHEN** 提交一笔 amount=0.2 的退出和一笔 close_all=true 的退出
- **THEN** 回执分别标识 fixed 和 all 数量模式，all 不被伪装成固定 0 数量

#### Scenario: Unsupported full-close combination
- **WHEN** 新请求为普通限价或条件限价选择 close_all=true
- **THEN** 系统在写入前返回 unsupported_combination，提示改用固定数量或支持全平的市价类型

### Requirement: Edit means an explicit change to an existing order
`edit` SHALL 按返回的 order_id 修改本任务未完成订单的适用价格或 remaining_amount，至少一项必填。remaining_amount SHALL 是有限正数的目标未成交基础币数量，拒绝布尔和零值。省略字段保持不变；方向、订单类型、fixed/all 模式不能隐式改变；all 订单不接受 remaining_amount。回执 SHALL 表明修改方式及当前有效身份。

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

### Requirement: Edit authorization follows the owned target
系统 SHALL 根据已核验归属订单判断 edit 属于入场还是退出。入场 edit SHALL 保留原有增仓能力、额度、杠杆和可交易标的检查；退出 edit SHALL 要求 trade 权限。edit 的权限包含完成同一目标修改所必需的撤换，不额外要求 cancel scope；显式 cancel 仍要求 cancel 权限。单项、批次和兼容入口 SHALL 一致。

#### Scenario: Entry cannot be disguised as an exit edit
- **WHEN** 调用方用统一 edit 修改一张入场单，但当前配置不允许新增该敞口
- **THEN** 修改被拒绝，不因工具名称不再包含 entry 而绕过额度或能力检查

#### Scenario: Authorized edit uses cancel and replace
- **WHEN** 调用方有 trade 权限而无 cancel scope，修改本任务的未触发条件单
- **THEN** 系统可执行同一目标的必要撤换，但不能借此撤销其他订单或执行显式 cancel 动作

### Requirement: Cancel uses owned order identity
`cancel` SHALL 撤销由当前任务管理的指定入场或退出挂单，不修改持仓数量或接管外部订单。系统 SHALL 根据已记录的订单身份识别普通/条件单，向调用方返回当前有效 order_id；旧裸 ID 若存在歧义 SHALL 拒绝并要求使用查询返回的明确身份。

#### Scenario: Cancel conditional exit
- **WHEN** 调用方将查询回执中的条件单 order_id 传给 cancel
- **THEN** 系统取消该条件单并报告核验状态，不尝试把同数字普通订单当作目标

#### Scenario: Trigger precedes cancellation
- **WHEN** cancel 使用的母单身份已触发并关联一张仍活跃的限价子单
- **THEN** 系统在核实归属和关联后取消该子单未成交部分，返回当前身份与实际成交；关联不明时返回待核验，不能仅凭母单终态声称整个退出已取消

### Requirement: Ordered batch uses the same action contracts
`execute_trade_actions` 的规范批次 SHALL 指定 contract_version=2，使用 action 判别的平铺动作，最多 20 项。无版本批次 SHALL 按旧契约解析；混合格式在写入前拒绝。整批静态、权限及已知模式检查 SHALL 在首次提交前完成，执行 SHALL 按顺序逐项重新准入，已执行结果不回滚。省略 execution_policy 或指定 stop_on_error 时，failed、pending、unknown 或被阻塞项 SHALL 停止后续交易项并标为 not_executed。显式 independent 策略 SHALL 只跳过依赖未满足或准入失败的项，继续评估其他项；依赖和策略 SHALL 持久化为请求意图。提交入场不等于等待成交；已确认操作附带清理告警不得代替下一动作的实际准入。

#### Scenario: Entry is still unfilled
- **WHEN** 默认停止策略的批次先提交限价 open，后续 close 所需的已成交仓位仍不存在
- **THEN** close 不预挂为未来仓位的退出单，报告失败；后续动作标记 not_executed，之前入场回执保留

#### Scenario: Cancellation permissions in a mixed batch
- **WHEN** 批次包含 open 与 cancel
- **THEN** 系统在首次提交前同时校验 trade 与 cancel 权限；纯撤单批次使用 cancel 权限

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

### Requirement: Legacy contracts retain their original meanings
系统 SHALL 通过明确的旧入口、旧批次格式或持久化版本识别历史契约，不依据缺少价格字段猜测版本。旧 take_profit_limit SHALL 保持普通 LIMIT 含义，旧 amount=0 SHALL 保持当时全量快照语义，旧入场 edit amount SHALL 保持含已成交的总量。新模型推荐目录 SHALL 使用规范动作并清楚标记兼容入口。

#### Scenario: Historical limit exit remains a limit
- **WHEN** 升级后读取无版本的旧 take_profit_limit 记录或收到旧 close_perpetual 的同名参数
- **THEN** 系统将其作为普通限价单继续查询、撤销和管理，不要求触发价也不创建条件单

#### Scenario: New take profit limit does not fall back
- **WHEN** 新 close 使用 order_type=take_profit_limit 但缺少 trigger_price
- **THEN** 系统拒绝请求，不将它推断为旧普通限价行为

#### Scenario: Persisted interpretation survives restart
- **WHEN** 新订单、待执行请求或回执被持久化后进程重启
- **THEN** 其契约版本和语义保持不变，无版本旧记录继续按 legacy 解释；旧 amount=0 的已解析快照不重新取当前仓位

### Requirement: Stable operations and factual receipts
所有金融写入 SHALL 使用持久化操作身份。同一操作的相同请求重放不得重复写入，身份复用但意图变化 SHALL 拒绝。回执 SHALL 分开表达操作 status 与 order_status，并提供订单身份、执行通道、数量模式、已知成交量及未知状态；接受委托不得被报告为成交。兼容工具不得绕过相同的身份检查。版本化结构回执 SHALL 额外区分本次执行、持仓证据、清理状态与后续动作限制；异常仍须保留已知身份和失败阶段，其他订单的清理错误不得覆盖本次已确认执行事实。

#### Scenario: Upgrade followed by replay
- **WHEN** 升级后重放已经记录的旧操作 ID 与原始请求
- **THEN** 返回原操作结果或对账结果，不因字段规范化或工具重命名再次下单

#### Scenario: Unknown result followed by another alias
- **WHEN** 一个操作结果未知，调用方使用兼容入口重试同一操作
- **THEN** 系统恢复或查询原操作，不创建第二笔订单；不同意图复用该 ID 被拒绝

#### Scenario: Market close succeeds while another order needs verification
- **WHEN** 市价平空单已核验成交，清理旧保护单时撤销 ACK 缺少累计成交量
- **THEN** close 回执保留平仓单的已确认成交量和身份，单独指出待核验保护单及清理状态，不仅返回 unknown 和撤单错误

#### Scenario: Submission timeout retains a queryable identity
- **WHEN** 新入场、退出或替代订单提交后响应超时
- **THEN** 回执保留原操作 ID 和预先保存的客户端 ID，已知交易所 ID 也保留，并明确当前提交结果未知、不得换 ID 重发

### Requirement: Descriptions match mode and execution capability
工具说明 SHALL 按用途、必填与互斥参数、方向及数量单位、示例、回执解释提供紧凑说明，并与实际 schema 一致。独立退出说明 SHALL 包含多次开仓、分批止盈、分批止损、最后全平及 edit 示例。未支持的参数不得被静默忽略；整仓附带保护规则不得混入独立模式说明。

#### Scenario: Example and schema agreement
- **WHEN** 使用工具说明提供的独立退出示例验证请求
- **THEN** 每个示例均符合公布 schema，退出示例包含明确持仓方向与数量选择，不把提交状态写成成交

### Requirement: Operation states preserve certainty and execution phase
结构化回执 SHALL 区分明确未执行或拒绝的 failed、可追踪的待完成 pending、是否执行无法确认的 unknown、已接受未完成的 submitted、请求动作已确认的 completed、已核实无需操作的 skipped，以及因前项阻塞而未尝试的 not_executed。状态 SHALL 由执行证据与阶段决定，不由错误字符串猜测；旧回执缺少证据时仍保守处理。

#### Scenario: Permission rejection precedes writes
- **WHEN** 开仓或改单在提交前被权限、参数或数量检查拒绝
- **THEN** 返回 failed、submitted=false 和具体原因，不报告可能已经执行

#### Scenario: Pending is not unknown
- **WHEN** 撤单已确认但累计成交量仍等待最终订单证据
- **THEN** 返回 pending 并保留撤销事实、最后已知成交及证据状态，不将 pending 在封装或持久化中统一转换为 unknown

#### Scenario: Explicit amendment rejection
- **WHEN** 普通 LIMIT 改单被交易所明确拒绝
- **THEN** edit 返回 failed 和拒绝原因，同时保留原订单的已知状态；不把原单视作撤销或把该拒绝标为未知

### Requirement: Callers can query an original operation without trading writes
内置 Agent 和聊天 SHALL 提供按原操作 ID 查询的工具，MCP SHALL 提供相同行为的操作查询。查询 SHALL 检查任务、账户、标的及调用者归属，可刷新交易所只读证据和本地回执；不得新建、撤销、修改订单或恢复撤建写入。前序交易失败或待核验 SHALL 不阻止合法恢复查询，查询失败不得覆盖已有执行事实。查询 SHALL 分别表达此次读取的成功/部分成功/失败与原操作的执行结果，并标识当前准入建议的证据范围和新鲜度。查询或本地投影生成时间 SHALL 与实际证据观察时间区分；仅重组本地缓存不得宣称已刷新交易所证据。原操作查询是按需能力，SHALL NOT 成为每轮决策的必经步骤。

#### Scenario: Agent checks the original close
- **WHEN** Agent 收到待核验回执后以其原操作 ID 发起查询
- **THEN** 返回本次平仓、阻塞订单、持仓和清理的最新可核验结果，不要求发起第二个 close，也不要求生成新的交易操作 ID

#### Scenario: Query after a failed tool in one model turn
- **WHEN** 同一轮先发生交易失败或待核验，之后调用只读操作查询
- **THEN** 查询仍可执行；被中止的交易项不会因此自动重放

#### Scenario: Read permission does not resume replacement
- **WHEN** 只具备 read 权限的 MCP 调用者查询一个正在撤建的 edit
- **THEN** 返回原单和替代意图的证据，但不提交替代单、不撤单，其他调用者的操作仍不可见

#### Scenario: Successful query of a rejected operation
- **WHEN** 原止损确实失败，此次查询成功读取其拒绝证据和当前作用范围
- **THEN** 展示查询成功与原止损失败两个事实，不将查询成功改写为止损成功，也不将原止损失败误报为读取失败

#### Scenario: Query cannot refresh current scope
- **WHEN** 旧操作可读取，但当前周期、账户或仓位依赖不能重新核验
- **THEN** 保留原操作结果，标识查询部分成功或失败及缺项；当前可用性为待刷新，不能复用旧周期的允许或禁止建议

#### Scenario: Local regeneration is not an exchange observation
- **WHEN** 后台只根据已保存证据重组原操作回执，未读取交易所
- **THEN** 回执标识证据来源和原观察时间；本地生成成功不表示仓位、订单或当前准入已经在生成时刻重新核验

### Requirement: Reconciled evidence reaches all receipt consumers
已核验的操作证据 SHALL 同步到原操作回执、当前 Agent/聊天状态视图、记忆证据和 MCP 操作查询/同 ID 重放。默认决策输入 SHALL 使用当前事实及相关异常的投影，完整历史回执按需读取，不要求各消费者逐轮注入同一份原始 JSON。恢复 SHALL 以原请求意图判断完成，保留首次回执及后续核验记录；新周期替换活跃周期后仍须能查询历史操作。确认成交不得因其他订单清理未知退回成交未知，旧快照不得覆盖较新的已确认事实。记忆消费 SHALL 对相同身份采用最新结果，保留原结果作为历史，不将未执行批次项推定为已执行。

#### Scenario: Background reconciliation resolves a close
- **WHEN** 后台核验到原平仓已成交且清理完成
- **THEN** 内置 Agent 下一次默认状态不再显示该旧限制，记忆证据取得解决结果；MCP get_operation/同 ID 重放仍返回恢复后的完整回执，不停留在首次 unknown，也不再次提交订单

#### Scenario: Canceled source does not prove successful replacement
- **WHEN** edit 的旧单撤销已确认，但替代单被拒绝或结果未明
- **THEN** edit 按替代阶段返回 failed 或 unknown/pending，不能仅凭旧单 canceled 宣告 edit 成功

#### Scenario: Completed cycle is replaced
- **WHEN** 新周期创建后查询旧周期某次待核验或已完成操作
- **THEN** 仍按旧操作及其订单证据返回结果，不错误关联新周期或丢失查询身份；该历史查询不使旧持仓或旧准入建议进入下一轮默认状态

#### Scenario: Memory gets the reconciled result rather than only a display overlay
- **WHEN** 原始工具回执为 unknown，当前同身份回执为 completed，记忆通过摘要记录消费执行证据
- **THEN** 实际传入记忆整理的证据包含最新结果及与原始结果的关联，不只在历史消息展示字段中补充当前回执

#### Scenario: An unresolved item is older than the recent receipt page
- **WHEN** 记忆或当前异常引用的操作未出现在最新一页回执，或属于已离开摘要窗口的批次子项
- **THEN** 消费者按原身份在授权范围内读取关联证据，不因最近记录条数限制丢失真实未决问题或其解决结果

### Requirement: Admission receipts provide scoped decisions and recovery instructions
规范交易和查询 SHALL 提供版本化的准入说明，区分本次请求的允许、带告警允许、拒绝及未评估；包含动作/目标范围、证据版本与时间、阻塞项、告警和恢复建议。查询中的建议不是新交易授权，执行 SHALL 重新核验。兼容 blocked_actions SHALL 不扩大目标级限制为全局禁止，空列表 SHALL 不代表已获全面准入；所有入口对相同动作及证据 SHALL 使用一致结论。

#### Scenario: Diagnostic error is only a warning
- **WHEN** 旧提交已确认失败且无执行影响，新请求全部实际依赖满足
- **THEN** 新请求获得允许或带告警允许，回执保留旧拒绝原因但不返回 trade_recovery_required

#### Scenario: A missing order ID still has a usable recovery identity
- **WHEN** 真正未知的提交尚无交易所 ID
- **THEN** 阻塞项保留原 operation_id 和客户端身份，并提供当前入口可用的查询方式，不只返回空订单 ID

#### Scenario: MCP recovery identity is accessible to the caller
- **WHEN** MCP 回执引用阻塞的其他操作
- **THEN** 仅返回当前 principal/profile 授权可访问的外部操作身份及查询入口；内部身份不得泄露或误映射为当前请求 ID，无法映射时明确给出授权范围内的恢复说明

#### Scenario: Admission changes between query and write
- **WHEN** 查询后发生新成交、其他写入或归属变化
- **THEN** 执行基于新证据重新判断，若拒绝则指出变化的证据及限制，而不是信任缓存允许标记

### Requirement: Canonical tool turns continue only independently admissible intent
独立退出模式的规范 Agent/Chat 工具轮次 SHALL 按顺序逐项核验，不仅依据前项 failed/pending/unknown 停止全部调用。满足自身必要条件的已请求动作 SHALL 继续；仅未满足的明确前置依赖、具体硬约束或本次必需但仍未核实的执行事实可以阻止对应项，不得猜测业务顺序而停止。聊天金融写入 SHALL 保留逐项审批；用户拒绝审批 SHALL 停止该轮后续金融写入。非独立模式及旧工具的停止语义 SHALL 保持兼容。

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

### Requirement: Canonical action names tolerate case-only model variations
Agent 和绑定任务聊天的 open、close、edit、cancel 工具执行入口 SHALL 将仅大小写不同的名称归一化后进行原有模式、参数、归属及准入校验。能力发现仍使用规范小写名，不增加其他别名、文件操作或交易权限；聊天写入 SHALL 保持原审批要求。归一化动作与规范名 SHALL 共享既有操作身份和幂等保护。

#### Scenario: Capitalized edit replays the same operation
- **WHEN** 模型调用 Edit 且提供合法交易改单参数，随后以相同操作身份调用 edit
- **THEN** 系统按规范 edit 校验和执行，已完成的同一操作不重复提交，不仅因大小写返回模式不允许

#### Scenario: Case variation grants no additional capabilities
- **WHEN** Edit 用于不支持独立改单的模式、带文件编辑参数，或聊天用户拒绝审批
- **THEN** 原模式、参数或审批检查拒绝执行，不操作文件、不绕过权限，也不把未知工具名称猜测为交易动作

### Requirement: Execution policies are explicit and backward compatible
系统 SHALL 在规范工具 schema、说明和能力发现中声明支持的执行策略及依赖语义。省略策略的旧请求 SHALL 保持原哈希和停止语义，显式新策略及依赖 SHALL 参与持久化身份校验。恢复不得改变原批次策略，旧客户端不认识新增回执字段时 SHALL 仍能读取原 status、错误及订单身份。

#### Scenario: Old request replays after upgrade
- **WHEN** 升级后以原格式重放省略 execution_policy 的 v2 或 legacy 请求
- **THEN** 命中原操作身份与请求哈希，不因补默认值而冲突，不重新执行

#### Scenario: New policy cannot be silently ignored
- **WHEN** 客户端向不支持 independent 的版本或不适用模式提交该策略
- **THEN** 系统明确拒绝或能力协商表明不支持，不静默按不同执行策略下单

### Requirement: Valid actions do not require a preliminary recovery call
Agent SHALL 能直接调用规范交易工具，系统 SHALL 在该次调用内部完成必要的有界只读取证与可自动完成的证据刷新，不新增必填预检、恢复令牌或手工解锁。内部刷新后条件满足 SHALL 在原调用中继续该次尚未提交的请求；仍未满足才返回具体限制。该行为不得重发原未知交易，也不得为通过准入隐式撤改其他订单。

#### Scenario: Corrected stop works without querying first
- **WHEN** 原止损已确认无执行影响，Agent 直接请求新的有效止损且必要证据可以取得
- **THEN** 同一次工具调用完成核验和新止损提交，不要求先调用 get_trade_operation 或等待下一轮

#### Scenario: Refreshable evidence is resolved inside the action
- **WHEN** 必需证据暂时陈旧，但在本次只读查证预算内可以更新并满足准入
- **THEN** 系统更新证据后继续尚未提交的本次请求，模型无需多一次查询/重试调用

#### Scenario: Unresolved submission is not retried to improve availability
- **WHEN** 相关原交易是否执行在有界核验后仍未知
- **THEN** 一次返回其身份、缺失证据和恢复路径，后台按既有机制推进，不无限占用本次调用、不重复原写入，也不阻断无关工具

### Requirement: Batch dependency guidance matches validation
模型可见的批次契约 SHALL 说明依赖只适用于显式 execution_policy=independent，depends_on 可省略但提供时必须为非 null 的严格整数列表，索引从零开始且只引用不重复的此前项。Schema、示例和运行时拒绝信息 SHALL 一致；错误指出相关字段和纠正条件。系统 SHALL 保留省略策略时的默认停止语义与既有序列化身份，不自动切换策略、丢弃依赖或执行非法批次。

#### Scenario: Dependency requires explicit independent policy
- **WHEN** v2 批次携带 depends_on 但省略 execution_policy 或选择 stop_on_error
- **THEN** 整批在写入前拒绝，错误指出动作索引和显式 independent 要求；工具说明区分批次策略与任务的 independent_exits 模式

#### Scenario: Schema does not suggest null dependencies
- **WHEN** Agent 或 MCP 取得 v2 批次 Schema
- **THEN** depends_on 展示为可省略的整数数组，不展示 null 为合法值；依赖示例显式指定 independent，首项省略依赖，后项引用此前 completed 项

#### Scenario: Invalid dependency leaves all actions unexecuted
- **WHEN** 依赖为 null、布尔值、字符串、重复索引、负值或自身及之后索引
- **THEN** 系统返回可定位的参数错误，在任何交易写入前拒绝且不自动修补；合法批次仍按既有 completed 依赖条件执行

#### Scenario: Omitted fields retain request identity
- **WHEN** 旧 v2 请求省略策略和依赖并被重新校验或重放
- **THEN** 序列化保持省略，操作身份与默认停止行为不变，已执行动作不重复提交
