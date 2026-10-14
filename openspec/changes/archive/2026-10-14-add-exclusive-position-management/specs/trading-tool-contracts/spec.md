# Spec Delta

## MODIFIED Requirements

### Requirement: Cancel uses owned order identity
`cancel` SHALL 撤销由当前任务管理的指定入场或退出挂单，不修改持仓数量。未授权的外部订单 SHALL NOT 被自动接管；启用独占管理时，系统 SHALL 在原调用内核验并本地纳管范围内目标后继续撤单，且仍要求 cancel 权限。系统 SHALL 根据已记录及已核验的订单身份识别普通/条件单，向调用方返回当前有效 order_id；旧裸 ID 若存在歧义 SHALL 拒绝并要求使用查询返回的明确身份。

#### Scenario: Cancel conditional exit
- **WHEN** 调用方将查询回执中的条件单 order_id 传给 cancel
- **THEN** 系统取消该条件单并报告核验状态，不尝试把同数字普通订单当作目标

#### Scenario: Trigger precedes cancellation
- **WHEN** cancel 使用的母单身份已触发并关联一张仍活跃的限价子单
- **THEN** 系统在核实归属和关联后取消该子单未成交部分，返回当前身份与实际成交；关联不明时返回待核验，不能仅凭母单终态声称整个退出已取消

#### Scenario: Cancel a human-created order in an exclusive scope
- **WHEN** 有 cancel 权限的管理者请求撤销独占范围内一张已核验外部单
- **THEN** 原调用完成本地纳管、目标核验及撤单，保存阶段和结果，不因不在原 Agent 开单记录中而直接拒绝

### Requirement: Valid actions do not require a preliminary recovery call
Agent SHALL 能直接调用规范交易工具，系统 SHALL 在该次调用内部完成必要的有界只读取证与可自动完成的证据刷新，不新增必填预检、恢复令牌或手工解锁。内部刷新后条件满足 SHALL 在原调用中继续该次尚未提交的请求；仍未满足才返回具体限制。该行为不得重发原未知交易，也不得为通过准入隐式撤改其他订单。独占授权下的仓位和订单纳管 SHALL 可在同次请求内完成本地记录，不能要求调用者先执行手工 adopt；取证及纳管本身不得产生交易所写入。

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
- **WHEN** 管理者直接对独占范围内已有外部仓位请求有效 close，事实可在预算内核验
- **THEN** 本地建立真实管理基线后执行原请求，不返回缺周期错误要求再次调用接管和 close

## ADDED Requirements

### Requirement: Enrolled external orders use the existing edit contract
已核验纳管的外部订单 SHALL 使用现有 edit 语义，按真实作用判断入场/退出和所需权限。remaining_amount SHALL 仍为目标未成交基础币数量；省略字段保留最新核验值，不能恢复被人工修改前的缓存参数。方向、类型和 fixed/all SHALL 不被猜测或静默改变。不支持的原生订单组合 SHALL 明确报告能力限制。

#### Scenario: Edit a partially filled external entry
- **WHEN** 独占范围内外部限价入场已部分成交，管理者指定 remaining_amount 与新价格
- **THEN** 按核验累计成交量和目标未成交量执行修改，保留原单或必要撤建的关联及阶段

#### Scenario: Human changes omitted fields
- **WHEN** 人工修改订单数量后 Agent 只请求改价
- **THEN** 使用最新已核验的数量执行，不把旧缓存数量重新写回交易所

### Requirement: External exchange activity is not an Agent operation result
外部成交及订单变更 SHALL 有稳定事实身份及来源，不伪造为某次 Agent 操作成功。原 Agent 请求、操作 ID、首次回执和已确认事实 SHALL 保留；相同请求回放不得因纳管、周期更换或新外部成交重复下单，未知原写入须按原身份恢复。

#### Scenario: Human flattening precedes an Agent close
- **WHEN** 人工已全平且系统可靠确认空仓，Agent 随后请求 close
- **THEN** 返回已核验无需操作的 skipped/no_position，记录外部平仓，不将它描述为 Agent close 已成交

#### Scenario: External order matches a timed-out Agent submission
- **WHEN** 新发现订单的客户端身份与原未知 Agent 提交一致
- **THEN** 恢复原操作关联和回执，不把它重复纳管成另一外部订单，不重发原提交
