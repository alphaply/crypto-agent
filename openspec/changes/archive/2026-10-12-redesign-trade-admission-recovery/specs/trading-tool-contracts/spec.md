# Spec Delta

## MODIFIED Requirements

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

### Requirement: Callers can query an original operation without trading writes
内置 Agent 和聊天 SHALL 提供按原操作 ID 查询的工具，MCP SHALL 提供相同行为的操作查询。查询 SHALL 检查任务、账户、标的及调用者归属，可刷新交易所只读证据和本地回执；不得新建、撤销、修改订单或恢复撤建写入。前序交易失败或待核验 SHALL 不阻止合法恢复查询，查询失败不得覆盖已有执行事实。查询 SHALL 分别表达此次读取的成功/部分成功/失败与原操作的执行结果，并标识当前准入建议的证据范围和新鲜度。

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
- **THEN** 保留原操作结果，标识查询部分成功或失败及缺项；当前可用性为待刷新，不能复用旧周期的允许建议

## ADDED Requirements

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
