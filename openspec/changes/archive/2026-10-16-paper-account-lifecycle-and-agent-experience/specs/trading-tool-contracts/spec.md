# Spec Delta

## MODIFIED Requirements

### Requirement: Cancel uses owned order identity
`cancel` SHALL 撤销当前可信账户与标的授权范围内已核验的指定入场或退出挂单，不将撤单报告为平仓。调用方 SHALL 具备 cancel 权限，无需先取得长期独占管理权或 adopt；订单由另一已授权连接、Agent、人工或外部来源创建 SHALL 不单独阻止撤销，来源不扩大账户权限。系统 SHALL 根据已核验订单身份识别普通/条件单，向调用方返回当前有效 order_id；旧裸 ID 若存在歧义 SHALL 拒绝并要求使用查询返回的明确身份。

#### Scenario: Cancel conditional exit
- **WHEN** 调用方将查询回执中的条件单 order_id 传给 cancel
- **THEN** 系统取消该条件单并报告核验状态，不尝试把同数字普通订单当作目标

#### Scenario: Trigger precedes cancellation
- **WHEN** cancel 使用的母单身份已触发并关联一张仍活跃的限价子单
- **THEN** 系统在核实归属和关联后取消该子单未成交部分，返回当前身份与实际成交；关联不明时返回待核验，不能仅凭母单终态声称整个退出已取消

#### Scenario: Cancel a human-created order in an exclusive scope
- **WHEN** 有 cancel 权限的管理者请求撤销授权范围内一张已核验外部单
- **THEN** 原调用完成本地纳管、目标核验及撤单，保存阶段和结果，不因不在原 Agent 开单记录中而直接拒绝

#### Scenario: First financial use can cancel a manual order
- **WHEN** 有 cancel 权限的 live MCP 首次请求撤销可见授权范围内的人工挂单，即使该范围已有其他连接管理记录，且目标证据完整
- **THEN** 系统按共享资源协调并撤销已核验目标，保留人工来源和真实成交证据，不额外要求 trade 权限、先 adopt 或先查询解锁

#### Scenario: Visibility does not defeat an existing manager
- **WHEN** 调用方可以查询一笔订单，但其管理范围已有另一任务或 profile 的有效管理者
- **THEN** 具备目标 cancel 权限时可按最新事实撤销；仅可查询而无 cancel 权限仍拒绝，不因其他 owner 拒绝或因可见性扩大权限

### Requirement: Descriptions match mode and execution capability
工具说明 SHALL 按用途、必填与互斥参数、方向及数量单位、示例、回执解释提供紧凑说明，并与当前身份和模式的实际 schema 一致。默认说明 SHALL 使用 execute_trade_actions 的单项或多项示例；独立退出说明 SHALL 包含多次开仓、分批止盈、分批止损、最后全平及 edit 示例。未支持的参数不得被静默忽略；整仓附带保护规则不得混入独立模式说明。默认自动纳管说明 SHALL 区分可信授权范围、实际并发协调和证据限制，并说明不同连接可共管，不要求先 adopt，也不以人工来源统一宣告不可管理。

#### Scenario: Example and schema agreement
- **WHEN** 使用工具说明提供的独立退出示例验证请求
- **THEN** 每个示例均符合公布 schema，退出示例包含明确持仓方向与数量选择，不把提交状态写成成交

#### Scenario: Attached and spot capabilities are described separately
- **WHEN** 调用者分别取得附带保护合约、独立退出合约及现货的 v3 schema 与说明
- **THEN** 各自只展示该模式支持的参数和 protect/close 组合；现货不出现合约 SHORT 或杠杆退出语义，独立退出不宣称整仓 protect 可用

#### Scenario: Automatic management does not ask for adoption first
- **WHEN** 默认 Agent 或 MCP 目录描述可见授权范围内的人工持仓和挂单管理
- **THEN** 说明允许直接提交金融动作并在执行前核验并协调共享资源，删除无条件先 adopt 的指导；不因其他管理者拒绝；权限不足、目标事实变化或未决依赖时返回对应具体原因

### Requirement: Valid actions do not require a preliminary recovery call
Agent SHALL 能直接调用规范交易工具，系统 SHALL 在该次调用内部完成必要的有界只读取证与可自动完成的证据刷新，不新增必填预检、恢复令牌或手工解锁。内部刷新后条件满足 SHALL 在原调用中继续该次尚未提交的请求；仍未满足才返回具体限制。该行为不得重发原未知交易，也不得为通过准入隐式撤改其他订单。共享交易授权下的仓位和订单纳管 SHALL 可在同次请求内完成本地记录，不能要求调用者先执行手工 adopt；取证及纳管本身不得产生交易所写入。

#### Scenario: Corrected stop works without querying first
- **WHEN** 原止损已确认无执行影响，Agent 直接请求新的有效止损且必要证据可以取得
- **THEN** 同一次工具调用完成核验和新止损提交，不要求先调用 get_trade_operation 或等待下一轮

#### Scenario: Refreshable evidence is resolved inside the action
- **WHEN** 必需证据暂时陈旧，但在本次只读查证预算内可以更新并满足准入
- **THEN** 系统更新证据后继续尚未提交的本次请求，模型无需多一次查询/重试调用

#### Scenario: Unresolved submission is not retried to improve availability
- **WHEN** 相关原交易是否执行在有界核验后仍未知
- **THEN** 一次返回其身份、缺失证据和恢复路径，后台按既有机制推进，不无限占用本次调用、不重复原写入，也不阻断无关工具

#### Scenario: First close enrolls startup exposure
- **WHEN** 管理者直接对授权范围内已有外部仓位请求有效 close，事实可在预算内核验
- **THEN** 本地建立真实管理基线后执行原请求，不返回缺周期错误要求再次调用接管和 close

### Requirement: Enrolled external orders use the existing edit contract
已核验纳管的外部订单 SHALL 使用现有 edit 语义，按真实作用判断入场/退出和所需权限。remaining_amount SHALL 仍为目标未成交基础币数量；省略字段保留最新核验值，不能恢复被人工修改前的缓存参数。方向、类型和 fixed/all SHALL 不被猜测或静默改变。不支持的原生订单组合 SHALL 明确报告能力限制。

#### Scenario: Edit a partially filled external entry
- **WHEN** 授权范围内外部限价入场已部分成交，管理者指定 remaining_amount 与新价格
- **THEN** 按核验累计成交量和目标未成交量执行修改，保留原单或必要撤建的关联及阶段

#### Scenario: Human changes omitted fields
- **WHEN** 人工修改订单数量后 Agent 只请求改价
- **THEN** 使用最新已核验的数量执行，不把旧缓存数量重新写回交易所

### Requirement: Financial actions establish management without a preliminary adoption tool
默认金融动作 SHALL 在当前可信账户、市场、标的及动作权限范围内核验目标和协调共享资源，不要求先到先得的长期独占管理权。不同已授权 MCP profile、principal 和内置 Agent SHALL 可共同管理相同真实资源；原提交者身份仅用于来源、权限及审计。人工或外部来源和尚无交易周期 SHALL NOT 单独阻止已获交易授权的管理；来源与真实基线 SHALL 保留。只读查询 SHALL NOT 创建金融写入或独占范围，读权限 SHALL NOT 获得金融权限。

#### Scenario: Visible manual position is managed in the same action
- **WHEN** 获授权任务直接请求 close 范围内人工已有仓位，目标获授权且执行前事实和数量完整
- **THEN** 系统在本次调用内登记管理基线并执行合法退出，不要求模型先 adopt 或下一轮重试；人工仓位不被写成 Agent 入场成交

#### Scenario: Read-only discovery does not win ownership
- **WHEN** read principal 查询尚无管理者的可见人工仓位或订单
- **THEN** 查询只返回观察事实与共享管理状态，不登记独占管理者、不提交金融动作，也不使后续合法金融调用被该读者占用阻止

#### Scenario: Same profile principals share management but not private receipts
- **WHEN** 两个已授权 principal 通过同一 profile 对其管理范围提交金融动作
- **THEN** 管理者身份为该 profile，动作按共享账户资源协调；各 principal 仍遵守原权限及操作回执访问范围，不因共享账户管理而公开另一调用者的私有操作身份

### Requirement: Explicit handoff grants only resource-scoped recovery access
共享管理 SHALL 不要求先交接独占管理权。确有私有未决资源恢复授权需要交接时，经明确接受的资源 SHALL 仅提供这些资源所需的只读恢复授权与查询身份。原请求、actor、操作 ID 和证据 SHALL 保留；非交接资源及原 principal 的其他私有回执 SHALL 不因共享管理公开。查询 SHALL 只核验原操作，不能在接收方身份下重发、恢复未执行尾项或提交新交易。

#### Scenario: The receiver inspects an accepted unresolved operation
- **WHEN** 管理者 B 明确接受 A 的范围和已登记未决订单/操作，并在自己的授权上下文使用交接给出的恢复身份查询
- **THEN** 能查看该资源所需原执行阶段、订单身份和核验结果，保留原 actor 和操作身份；其他 A 的私有历史仍不可见，查询没有金融写入

#### Scenario: A former owner retries after ownership changes
- **WHEN** A 的旧客户端重放已交接的原操作，或旧 worker 返回迟到 ACK
- **THEN** 只保留或核验原已开始执行事实，不通过旧请求重放取得新提交权、不改变历史身份，也不重发原交易或批次尾项；当前仍有金融权限的 A 可以另行提交新的明确意图
