# binance-perpetual-orders Specification

## Purpose

准确适配币安 USDⓈ-M 合约的普通订单与条件订单，使独立交易动作遵循真实的持仓方向、数量、触发和修改能力，并在订单触发、撤改或通信异常后仍能识别原操作和实际成交。

## Requirements

### Requirement: Regular and conditional orders use distinct exchange channels
系统 SHALL 将 market、limit 分别映射到普通 MARKET、LIMIT；将 stop_market、stop_limit、take_profit_market、take_profit_limit 分别映射到条件 STOP_MARKET、STOP、TAKE_PROFIT_MARKET、TAKE_PROFIT。条件创建、查询和撤销 SHALL 使用 Algo Order 通道，不因同属退出动作而使用普通订单接口。

#### Scenario: Conditional limit take profit
- **WHEN** 提交 take_profit_limit，包含明确触发价、委托价与固定数量
- **THEN** 发送 CONDITIONAL 类型的 TAKE_PROFIT 条件委托，查询与撤销沿用条件通道

#### Scenario: Ordinary limit exit
- **WHEN** 提交 limit 退出
- **THEN** 发送普通 LIMIT 减仓委托，不增加触发条件

### Requirement: Hedge and one-way modes have explicit directional behavior
系统 SHALL 在开仓前读取并遵循账户实际持仓模式。同币多空持仓要求 Hedge Mode；系统 SHALL 不自动切换账户模式。One-way 下包含双向开仓的已知批次 SHALL 在首次提交前整体拒绝；单项开仓还 SHALL 检查冲突方向持仓与活跃入场挂单，防止把互相净额抵消当成独立双向建仓。

#### Scenario: Both sides in a hedge account
- **WHEN** Hedge Mode 下同一批包含 LONG 和 SHORT 入场
- **THEN** 分别提交 BUY/LONG 和 SELL/SHORT，各自保留独立回执与周期

#### Scenario: Both sides in a one-way account
- **WHEN** One-way Mode 下请求同币双向开仓批次
- **THEN** 批次在任何一笔提交前返回持仓模式不支持，不切换账户设置

#### Scenario: Opposite entry is pending
- **WHEN** One-way 下已有未成交 LONG 入场，又请求 SHORT 入场
- **THEN** 系统报告对向挂单冲突，不仅检查当前非零持仓

### Requirement: Fixed exits retain reduce-only intent
固定退出 SHALL 携带明确 quantity；One-way 下使用 BOTH 和 reduceOnly，Hedge 下使用目标 LONG/SHORT 与对应反向买卖动作，且不得发送 Binance 不接受的 reduceOnly 参数。交易所精度换算后数量须有效；实际执行 SHALL 不成为反向开仓。

#### Scenario: Partial short stop in hedge mode
- **WHEN** 对 SHORT 创建固定数量止损
- **THEN** 请求为 BUY、positionSide=SHORT、指定 quantity 的条件单，不携带 reduceOnly 或 closePosition=true

#### Scenario: Partial long limit exit in one-way mode
- **WHEN** 对 LONG 创建普通限价减仓
- **THEN** 请求为 SELL、positionSide=BOTH、reduceOnly=true，数量按基础币与合约单位正确换算

### Requirement: Native conditional all is reserved for supported market types
条件 close_all SHALL 仅映射 STOP_MARKET 或 TAKE_PROFIT_MARKET 的 closePosition=true，不携带 quantity 或 reduceOnly，并遵循正确的 positionSide。系统 SHALL 在创建前确认该方向仓位归属；价格条件和当前仓位估计不改变其触发时全平语义。

#### Scenario: Final take profit closes remaining long
- **WHEN** 任务对其独占管理的 LONG 创建 take_profit_market 加 close_all=true
- **THEN** 提交 SELL、closePosition=true 的 TAKE_PROFIT_MARKET 条件单，Hedge 时包含 positionSide=LONG，并省略 quantity、reduceOnly

#### Scenario: Existing close-all conflicts
- **WHEN** Binance 因已有条件退出拒绝新 close-all 请求
- **THEN** 系统返回可识别冲突及已有订单事实，不自动撤销冲突订单后重试

### Requirement: Price semantics match actual order types
普通 limit SHALL 不被限定为当前价格盈利侧的“止盈价”；可立即成交的普通限价减仓仍是有效请求。条件 TP/SL SHALL 校验所选触发基准和方向，明确区分 trigger_price 与 price。触发限价 SHALL 不被描述为触发后保证成交，也不得自动转成市价。

#### Scenario: Marketable limit reduction
- **WHEN** LONG 的普通 limit 卖出价低于当前交易价格，其他约束满足
- **THEN** 系统不因“止盈必须高于当前价”而拒绝，可按交易所规则立即成交

#### Scenario: Stop-limit triggers without filling
- **WHEN** stop_limit 触发后其限价无法成交
- **THEN** 展示已触发但执行单未成交，继续跟踪执行订单，不声明止损已完成或自动补一笔市价单

### Requirement: Order identities survive triggering
系统 SHALL 区分普通 orderId、条件 algoId 及条件触发产生的实际执行 orderId，并将其关联到同一业务操作及持仓周期。公开的 order_id SHALL 能唯一定位当前订单；查单、撤单和成交核算不得混用不同通道的相同数字 ID。母单触发与子单成交 SHALL 分别核验。

#### Scenario: Algo and regular numeric IDs collide
- **WHEN** 普通订单与条件订单的交易所数字 ID 相同
- **THEN** 返回的规范身份仍能区分二者，操作指定目标不会修改另一通道订单

#### Scenario: Condition triggers into a limit order
- **WHEN** Algo 订单触发并返回实际执行订单 ID
- **THEN** 后续成交以执行订单证据核算，查询结果提供母子关联与当前可操作身份

#### Scenario: Edit the active child after triggering
- **WHEN** 使用已触发母单身份请求 edit
- **THEN** 返回已触发状态和已核验子单身份，不自动重建条件单；后续显式 edit 子单按普通 LIMIT 修改限价/剩余量，不能再修改触发价

### Requirement: Edit chooses a supported and recoverable execution method
普通 LIMIT 修改 SHALL 优先使用原生改单；不支持直接修改的未触发条件单 SHALL 采用撤销、核验、重建。撤销未确认、触发/成交竞态或新单结果未知时 SHALL 保留可恢复操作身份并返回待核验，不重复重建。执行期间新增成交 SHALL 从原剩余目标中扣除，回执以最终核验数量为准。

#### Scenario: Native partially filled limit edit
- **WHEN** 修改部分成交的普通 LIMIT 的价格或目标剩余量
- **THEN** 按已核验累计成交计算交易所要求的总 quantity，提交原生修改并报告实际剩余量

#### Scenario: New fills consume the edit remainder
- **WHEN** edit 首次核验已成交 0.2、目标剩余 0.5，而执行修改过程中又成交 0.1
- **THEN** 新的剩余目标为 0.4，不补回 0.5；若在途成交已耗尽目标，则不再创建替代订单

#### Scenario: Trigger wins a cancel race
- **WHEN** Agent edit 条件单时，该单已触发并生成执行订单
- **THEN** 系统核实当前执行身份，不因母单不再挂起就创建第二张条件单；返回当前订单状态或待核验结果

#### Scenario: Replacement response is lost
- **WHEN** 旧单已确认取消，新单提交后响应丢失，随后进程重启
- **THEN** 通过原操作身份和预先保存的客户端 ID 找回新单，不再创建另一笔替代订单

### Requirement: Capabilities and exchange results stay explicit
系统 SHALL 公开本次支持的订单组合并在已知不支持时于交易写入前失败。请求已被交易所拒绝 SHALL 保留真实拒绝原因；请求结果不明 SHALL 区分于拒绝。系统不得把其他交易所或附带保护模式的特殊回退策略用于改变独立退出意图。创建、改单和撤单 SHALL 按实际执行阶段保留订单身份及证据；明确拒绝不能因异常封装变为 unknown，超时或含糊响应不能仅凭异常类型推断为明确拒绝。

#### Scenario: Unsupported adapter cannot emulate all silently
- **WHEN** 一个执行适配器没有原生条件全平能力
- **THEN** 创建条件 all 返回不支持，不转换成固定当前数量、不新增轮询合成单

#### Scenario: Unknown exchange outcome
- **WHEN** 发出条件单请求后网络超时且无法确认是否创建
- **THEN** 返回 unknown/pending 并保留原请求的查询身份，不把它当成明确失败自动换 ID 重试

#### Scenario: Native edit is definitively rejected
- **WHEN** 普通 LIMIT 修改收到可确认的交易所拒绝
- **THEN** 修改操作返回 failed 并保留原单状态和当前身份，不落入只有错误文本的 unknown 回执

#### Scenario: Ambiguous transport error after write
- **WHEN** 请求可能已送达交易所但收到断流、超时或无法验证的响应
- **THEN** 保留提交阶段和原客户端身份，执行查询核验，不根据错误类名将请求标记为未执行

### Requirement: Cancellation acknowledgement and final fill are separate evidence
撤销确认 SHALL 只证明指定目标的撤销事实；缺少累计成交字段、适配器补出的零值或挂单列表中消失 SHALL 不单独证明零成交。系统 SHALL 按普通单或条件母单/子单的确切身份补查终态，保留最后已知成交量及待核验状态；只有可核实的最终证据才能结束成交核验。

#### Scenario: Algo cancellation acknowledgement has no execution fields
- **WHEN** 条件单撤销 ACK 包含成功码及 algoId，但没有原生成交字段
- **THEN** 记录撤销已确认并查询原条件身份，不能把转换后的 filled=0 当作最终零成交证明

#### Scenario: Untriggered terminal parent is verified
- **WHEN** 后续查询确认母单已撤销且未触发，没有执行子单或与已有证据冲突
- **THEN** 按该接口已验证的终态语义结束母单核验，不因母单天然缺少固定数量字段永久等待

#### Scenario: Trigger wins cancellation race
- **WHEN** 撤单过程发现母单已触发并关联实际执行子单
- **THEN** 对账沿已验证关联查询子单真实成交；母单已撤销或结束本身不能证明子单成交量或剩余委托状态

#### Scenario: Stale open response follows cancellation
- **WHEN** 已确认撤销后查询短暂返回旧 open 状态或倒退累计成交
- **THEN** 保留撤销事实与最后已知累计成交，标记证据冲突并继续核验，不复活旧单、不创建重复替代单

### Requirement: Recovery preserves exit execution intent
恢复过程 SHALL 保留原请求的 fixed/all、基础币单位、持仓方向和 ordinary/algo 通道；只有完成原修改的必要步骤才能延续已持久化撤建意图。已成交平仓不得在恢复时重发，部分成交或目标耗尽不得按原计划量补量。

#### Scenario: Restart after a filled market close
- **WHEN** 市价平仓成交后清理中断并发生进程重启
- **THEN** 只恢复原订单核验和必要清理，不再次提交市价平仓；残仓由 Agent 基于新鲜证据明确决策

#### Scenario: Replacement target consumed by fills
- **WHEN** 条件退出 edit 撤旧期间新增成交已经耗尽目标剩余量
- **THEN** 返回目标已被成交耗尽的确定结果，不创建零量替代单或重新补回原剩余量
