# Spec Delta

## MODIFIED Requirements

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

### Requirement: Ordered batch uses the same action contracts
`execute_trade_actions` 的新规范批次 SHALL 指定 contract_version=2，使用 action 判别的平铺动作，最多 20 项。无版本批次 SHALL 按旧契约解析；混合格式在写入前拒绝。整批静态、权限及已知模式检查 SHALL 在首次提交前完成；执行 SHALL 按顺序进行，failed、pending 或 unknown 阻止后续交易项，已执行结果不回滚。提交入场不等于等待成交。已确认操作附带清理待核验时 SHALL 按后续动作的范围与新鲜证据重新检查准入；不兼容的动作及之后交易项 SHALL 标为 not_executed，并指出阻塞来源。

#### Scenario: Entry is still unfilled
- **WHEN** 批次先提交限价 open，后续 close 所需的已成交仓位仍不存在
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
- **WHEN** 平仓本身已确认，但旧周期的条件 all 是否仍活跃未核实，后项请求同方向重新开仓
- **THEN** 后项及之后交易项标记 not_executed，指出旧单身份和需完成的核验，不伪称本次平仓未成交

## ADDED Requirements

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
内置 Agent 和聊天 SHALL 提供按原操作 ID 查询的工具，MCP SHALL 提供相同行为的操作查询。查询 SHALL 检查任务、账户、标的及调用者归属，可刷新交易所只读证据和本地回执；不得新建、撤销、修改订单或恢复撤建写入。前序交易失败或待核验 SHALL 不阻止合法的恢复查询，查询失败不得覆盖已有执行事实。

#### Scenario: Agent checks the original close
- **WHEN** Agent 收到待核验回执后以其原操作 ID 发起查询
- **THEN** 返回本次平仓、阻塞订单、持仓和清理的最新可核验结果，不要求发起第二个 close，也不要求生成新的交易操作 ID

#### Scenario: Query after a failed tool in one model turn
- **WHEN** 同一轮先发生交易失败或待核验，之后调用只读操作查询
- **THEN** 查询仍可执行；被中止的交易项不会因此自动重放

#### Scenario: Read permission does not resume replacement
- **WHEN** 只具备 read 权限的 MCP 调用者查询一个正在撤建的 edit
- **THEN** 返回原单和替代意图的证据，但不提交替代单、不撤单，其他调用者的操作仍不可见

### Requirement: Reconciled evidence reaches all receipt consumers
已核验的操作证据 SHALL 同步到原操作回执、当前 Agent 上下文、聊天和 MCP 操作查询/同 ID 重放。恢复 SHALL 以原请求意图判断完成，保留首次回执及后续核验记录；新周期替换活跃周期后仍须能查询历史操作。确认成交不得因其他订单清理未知退回成交未知，旧快照不得覆盖较新的已确认事实。

#### Scenario: Background reconciliation resolves a close
- **WHEN** 后台核验到原平仓已成交且清理完成
- **THEN** 内置 Agent 下一次读取及 MCP get_operation/同 ID 重放均返回恢复后的结果，不停留在首次 unknown，也不再次提交订单

#### Scenario: Canceled source does not prove successful replacement
- **WHEN** edit 的旧单撤销已确认，但替代单被拒绝或结果未明
- **THEN** edit 按替代阶段返回 failed 或 unknown/pending，不能仅凭旧单 canceled 宣告 edit 成功

#### Scenario: Completed cycle is replaced
- **WHEN** 新周期创建后查询旧周期某次待核验或已完成操作
- **THEN** 仍按旧操作及其订单证据返回结果，不错误关联新周期或丢失查询身份
