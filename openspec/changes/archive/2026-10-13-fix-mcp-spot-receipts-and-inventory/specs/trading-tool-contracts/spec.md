# Spec Delta

## MODIFIED Requirements

### Requirement: Callers can query an original operation without trading writes
内置 Agent 和聊天 SHALL 提供按原操作 ID 查询的工具，MCP SHALL 提供相同行为的操作查询。查询 SHALL 检查任务、账户、标的及调用者归属，可刷新交易所只读证据和本地回执；不得新建、撤销、修改订单或恢复撤建写入。前序交易失败或待核验 SHALL 不阻止合法恢复查询，查询失败不得覆盖已有执行事实。查询不存在或不属于当前调用者的操作 ID 时，系统 SHALL 返回结构化的未找到结果，不得抛出未捕获异常或协议错误。查询 SHALL 分别表达此次读取的成功/部分成功/失败与原操作的执行结果，并标识当前准入建议的证据范围和新鲜度。查询或本地投影生成时间 SHALL 与实际证据观察时间区分；仅重组本地缓存不得宣称已刷新交易所证据。原操作查询是按需能力，SHALL NOT 成为每轮决策的必经步骤。

#### Scenario: Agent checks the original close
- **WHEN** Agent 收到待核验回执后以其原操作 ID 发起查询
- **THEN** 返回本次平仓、阻塞订单、持仓和清理的最新可核验结果，不要求发起第二个 close，也不要求生成新的交易操作 ID

#### Scenario: Query after a failed tool in one model turn
- **WHEN** 同一轮先发生交易失败或待核验，之后调用只读操作查询
- **THEN** 查询仍可执行；被中止的交易项不会因此自动重放

#### Scenario: Read permission does not resume replacement
- **WHEN** 只具备 read 权限的 MCP 调用者查询一个正在撤建的 edit
- **THEN** 返回原单和替代意图的证据，但不提交替代单、不撤单，其他调用者的操作仍不可见

#### Scenario: Query for non-existent or unowned operation returns structured not found
- **WHEN** 调用者查询一个不存在的 operation_id 或属于其他调用者的操作
- **THEN** 系统返回包含 operation_id、state="not_found"、status="failed" 及友好原因的结构化字典，不抛出工具执行异常或 INVALID_ARGUMENT 协议错误

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
已核验的操作证据 SHALL 同步到原操作回执、当前 Agent/聊天状态视图、记忆证据和 MCP 操作查询/同 ID 重放。现货买入与退出操作查询 SHALL 支持只读核验交易所订单事实，在确认成交后将原 submitted 回执刷新为 FILLED/PARTIAL/CANCELLED，并附带成交时间戳与均价。默认决策输入 SHALL 使用当前事实及相关异常的投影，完整历史回执按需读取，不要求各消费者逐轮注入同一份原始 JSON。恢复 SHALL 以原请求意图判断完成，保留首次回执及后续核验记录；新周期替换活跃周期后仍须能查询历史操作。确认成交不得因其他订单清理未知退回成交未知，旧快照不得覆盖较新的已确认事实。记忆消费 SHALL 对相同身份采用最新结果，保留原结果作为历史，不将未执行批次项推定为已执行。

#### Scenario: Background reconciliation resolves a close
- **WHEN** 后台核验到原平仓已成交且清理完成
- **THEN** 内置 Agent 下一次默认状态不再显示该旧限制，记忆证据取得解决结果；MCP get_operation/同 ID 重放仍返回恢复后的完整回执，不停留在首次 unknown，也不再次提交订单

#### Scenario: Spot order fill reconciles submitted receipt
- **WHEN** 现货买单已在交易所撮合成交，调用者通过 MCP get_operation 查询该操作
- **THEN** 系统只读核验交易所订单状态，将回执更新为 FILLED，附带成交数量、成交均价及成交时间戳，不再停留在 submitted

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

## ADDED Requirements

### Requirement: MCP provides read-only order and trade inspection tools
MCP 服务 SHALL 提供只读的 `get_order`（按交易所 order_id 查询单笔订单详情）和 `get_trade_history`（查询最近成交明细）工具，便于外部 Agent 在订单已离开未成交挂单列表后核验成交事实。

#### Scenario: Query filled order by order_id
- **WHEN** 订单已撮合成交且不再出现在 get_orders 未成交列表中，Agent 调用 get_order(order_id)
- **THEN** 系统只读返回该订单在交易所的终态（status、filled、avg_price、filled_at），不产生任何交易写入

#### Scenario: Query recent trades
- **WHEN** Agent 调用 get_trade_history 查询标的最近成交明细
- **THEN** 系统只读返回该 Profile 授权范围内的成交记录列表，包含成交时间戳、数量、价格与方向
