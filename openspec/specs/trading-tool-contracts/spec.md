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
`execute_trade_actions` 的新规范批次 SHALL 指定 contract_version=2，使用 action 判别的平铺动作，最多 20 项。无版本批次 SHALL 按旧契约解析；混合格式在写入前拒绝。整批静态、权限及已知模式检查 SHALL 在首次提交前完成；执行 SHALL 按顺序进行，failed、pending 或 unknown 阻止后续项，已执行结果不回滚。提交入场不等于等待成交。

#### Scenario: Entry is still unfilled
- **WHEN** 批次先提交限价 open，后续 close 所需的已成交仓位仍不存在
- **THEN** close 不预挂为未来仓位的退出单，报告失败；后续动作标记 not_executed，之前入场回执保留

#### Scenario: Cancellation permissions in a mixed batch
- **WHEN** 批次包含 open 与 cancel
- **THEN** 系统在首次提交前同时校验 trade 与 cancel 权限；纯撤单批次使用 cancel 权限

#### Scenario: Flat legacy cancellation is not a version signal
- **WHEN** 无版本的旧批次只包含平铺 cancel 动作
- **THEN** 仍按旧契约解析和重放；显式 v2 批次缺少新字段时不得回退为旧格式

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
所有金融写入 SHALL 使用持久化操作身份。同一操作的相同请求重放不得重复写入，身份复用但意图变化 SHALL 拒绝。回执 SHALL 分开表达操作 status 与 order_status，并提供订单身份、执行通道、数量模式、已知成交量及未知状态；接受委托不得被报告为成交。兼容工具不得绕过相同的身份检查。

#### Scenario: Upgrade followed by replay
- **WHEN** 升级后重放已经记录的旧操作 ID 与原始请求
- **THEN** 返回原操作结果或对账结果，不因字段规范化或工具重命名再次下单

#### Scenario: Unknown result followed by another alias
- **WHEN** 一个操作结果未知，调用方使用兼容入口重试同一操作
- **THEN** 系统恢复或查询原操作，不创建第二笔订单；不同意图复用该 ID 被拒绝

### Requirement: Descriptions match mode and execution capability
工具说明 SHALL 按用途、必填与互斥参数、方向及数量单位、示例、回执解释提供紧凑说明，并与实际 schema 一致。独立退出说明 SHALL 包含多次开仓、分批止盈、分批止损、最后全平及 edit 示例。未支持的参数不得被静默忽略；整仓附带保护规则不得混入独立模式说明。

#### Scenario: Example and schema agreement
- **WHEN** 使用工具说明提供的独立退出示例验证请求
- **THEN** 每个示例均符合公布 schema，退出示例包含明确持仓方向与数量选择，不把提交状态写成成交
