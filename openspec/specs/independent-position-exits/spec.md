# independent-position-exits Specification

## Purpose

使交易 Agent 能像分轮评估的交易员一样自主安排开仓、加仓和退出，以同方向实际聚合仓位管理定量分批订单及最终全平，同时明确成交、数量变化、异常状态与周期结束的可观察行为。

## Requirements

### Requirement: Exit timing is an agent decision
独立退出模式 SHALL 允许开仓时没有 TP/SL，也允许在确认成交后的任意后续轮次创建退出单。缺少或未覆盖 TP/SL 本身 SHALL 不导致拒绝有效开仓、强制当轮安装保护或自动平仓。系统 SHALL 展示实际覆盖与下一轮评估事实，不承诺 Agent 在轮次间持续作出交易决策。

#### Scenario: Entry now and exits next round
- **WHEN** 第一轮只提交入场，第二轮确认成交后提交止盈与止损
- **THEN** 两轮操作均可正常执行；第一轮结果如实显示尚未设置退出单，不因缺少保护被判定为失败

#### Scenario: Exit in the same round is also allowed
- **WHEN** 同一轮已获得可靠成交和仓位证据，Agent 决定创建退出单
- **THEN** 系统允许该操作，不强制等待下一轮

### Requirement: Scale-in orders manage aggregate directional positions
系统 SHALL 允许独立模式多次限价开仓或加仓，并按账户、任务、标的和 LONG/SHORT 管理聚合仓位。每个方向 SHALL 独立管理退出周期；退出数量针对已成交仓位而非逐笔入场 lot。未成交入场 SHALL 不被当成可退出仓位。

#### Scenario: Several entries on the same side
- **WHEN** 同方向两笔入场分别成交 0.2 和 0.3 基础币
- **THEN** Agent 可针对合计 0.5 的该方向仓位安排退出，不必选择某一入场单

#### Scenario: One side exits in a hedge position
- **WHEN** 双向账户同时持有本任务多仓和空仓，执行 LONG 的退出
- **THEN** 该操作仅减少多仓，空仓及其退出周期保持独立

### Requirement: Fixed stages and dynamic all coexist
系统 SHALL 支持多个固定数量退出与明确的全平退出并存。固定单 SHALL 保留提交或显式 edit 后的数量；支持的条件 all 单 SHALL 在触发时作用于已核验为当前任务独占管理的该方向实际剩余仓位，包括触发前的同周期加仓。all SHALL 不被按当前整仓数量再次计入固定档位预留总量。

#### Scenario: Two fixed stages followed by the remainder
- **WHEN** 1 BTC 多仓的两个固定止盈分别成交 0.2 和 0.3，之后全平止盈触发
- **THEN** 全平单退出当时剩余的 0.5 BTC，最终成交量以交易所证据为准

#### Scenario: Add after exits were placed
- **WHEN** 固定 0.2 的退出与 all 条件单已存在，随后同方向加仓 0.4
- **THEN** 固定退出仍为 0.2；all 保持全平语义，不固化为加仓前的数量

#### Scenario: Split stop losses
- **WHEN** Agent 提交两个不同触发价的定量止损及更远的一档 all 止损
- **THEN** 系统按相同的 fixed/all 规则处理，不要求止损只能是一笔整仓订单

### Requirement: All is bounded by execution capability
立即市价 all SHALL 在提交前读取当前可管理仓位并按该次核验数量下单；条件市价 all SHALL 使用交易所原生触发时全平能力。普通限价和条件限价 all SHALL 在本能力范围内被明确拒绝。全平 SHALL 不隐含终止未来所有开仓或保证覆盖提交/触发之后的并发入场成交。

#### Scenario: Market all with a later entry fill
- **WHEN** 市价 all 按核验到的 0.5 提交后，另一入场单又成交 0.1
- **THEN** 系统记录真实残余仓位并交由 Agent 管理，不自动追加一笔平仓来追求零仓位

#### Scenario: Cannot isolate shared virtual inventory
- **WHEN** 该账户方向的仓位无法确认由当前任务独占管理
- **THEN** 系统拒绝创建原生条件 all，不将交易所整方向全平描述为只退出本地某一虚拟份额

### Requirement: Existing fixed quantities are not automatically reallocated
仓位变化后系统 SHALL 不新建自动等比例缩量、补量或更换固定档位的操作，也不因创建冲突而擅自撤掉其他退出单。固定挂单总量超过剩余仓位 SHALL 被报告为数量冲突而非未知执行结果；该事实本身不得阻止 cancel、有效 edit、减仓或有效入场。已开始且结果不明的旧修改仍须完成核验。

#### Scenario: A take profit leaves oversized stop orders
- **WHEN** 部分止盈使仓位从 1 降至 0.6，而原固定止损合计仍为 1
- **THEN** 系统保留 Agent 请求的固定数量，展示各单的真实状态与超额信息，等待显式 edit/cancel；不自动把各档乘以 0.6

#### Scenario: Exchange rejects a competing exit
- **WHEN** 交易所拒绝新退出单与已有单并存
- **THEN** 系统报告拒绝并保留已有单，不通过自动清除其他退出单重试

### Requirement: Fixed exit admission respects actual position rather than allocation guesses
新建或显式调整固定退出 SHALL 核验单笔目标剩余量不超过当前该方向实际仓位。不同挂单的固定剩余量总和 SHALL 作为分组观察信息，不作为额外本地排他预留规则；交易所的真实减仓、精度与订单限制继续适用。全平退出 SHALL 单独显示，不能被计为数量为零或一笔固定整仓。

#### Scenario: Overlapping alternatives within a position
- **WHEN** 当前仓位为 1，Agent 已挂一笔定量退出 0.7，又请求另一价位定量退出 0.6
- **THEN** 系统不得仅因合计 1.3 拒绝请求，提交前仍核验单笔量、归属和减仓语义，并如实返回交易所接受或拒绝

#### Scenario: A single excessive quantity
- **WHEN** 当前仓位为 0.6，新请求固定退出 0.7
- **THEN** 系统在提交前返回数量超限，不擅自将请求缩成 0.6

### Requirement: Execution evidence determines fills and sequence
“最后一档” SHALL 仅表示 Agent 选择的退出条件，不构成前几档必须先成交的依赖关系。成交核算 SHALL 使用真实执行量，不能使用计划量或触发前 all 的估计量；退出执行 SHALL 不增加或反转目标仓位。模拟结果 SHALL 区分成交与未执行余量，并注明其确定性撮合次序不代表实盘保证。

#### Scenario: Price gaps across all stages
- **WHEN** 行情跳过多个退出价位，全平单在其他固定单完全成交前执行
- **THEN** 系统允许按实际结果直接全平，剩余退出单按周期规则清理，不虚构前档已成交

#### Scenario: Fixed exit exceeds position at execution time
- **WHEN** 一笔固定退出请求 0.7，但执行时仅剩 0.4
- **THEN** 实际退出不超过 0.4；真实成交量与未成交余量的拒绝、取消或其他终态分别展示，不报告成交 0.7

### Requirement: Reconciliation distinguishes trigger parents from executions
系统 SHALL 区分条件单等待触发、已触发的执行订单与已确认成交。原生 all 条件母单缺少固定数量或 filled 字段 SHALL 不单独造成整个周期未知；无法核实是否触发或实际成交时 SHALL 标记待核验并避免重发。全平估计覆盖量 SHALL 与实际成交量分开。

#### Scenario: Waiting all order without fixed quantity
- **WHEN** 已核验的条件母单仍在等待触发且没有数量/成交字段
- **THEN** 展示“剩余全部、等待触发”，不显示固定 0 数量，也不推断已有成交

#### Scenario: Trigger receipt without complete fill evidence
- **WHEN** 母单已触发但执行子单的成交结果暂时不可确认
- **THEN** 跟踪该执行身份并返回 pending/unknown，不将计划全平当作仓位已经归零

### Requirement: Confirmed cycle closure preserves cleanup behavior
系统 SHALL 在可靠确认某方向周期归零后清理该周期残留退出单及入场单，保持其他方向与其他任务不受影响。单次空仓快照 SHALL 不足以提前宣告清理完成；清理期间发生入场成交 SHALL 被重新核实并展示残仓。本能力不增加跨周期保留入场单或自动重入策略。

#### Scenario: Flat snapshot is temporarily stale
- **WHEN** 刚确认成交后仓位接口短暂查空
- **THEN** 系统保留周期并继续核验，不仅凭本次快照撤销全部退出单

#### Scenario: Independent cycle finishes
- **WHEN** LONG 周期已确认归零且订单清理结果可核验
- **THEN** 只清理该 LONG 周期的剩余入场与退出单，SHORT 周期保持有效

### Requirement: Visible state is consistent across real and simulated trading
内置 Agent 上下文、MCP 查询和前端展示 SHALL 对齐 fixed/all、订单类型、触发/限价、当前有效身份、真实剩余量、SL 覆盖估计与数量冲突。未触发或限价止损的挂单覆盖 SHALL 不被描述为保证成交。模拟与实盘 SHALL 采用相同数量语义与周期规则。

#### Scenario: Dynamic coverage display
- **WHEN** 账户存在一笔已核验的 stop_market all 与部分定量止盈
- **THEN** 展示止损为“剩余全部”和当前覆盖估计，定量止盈保持自身剩余量，不把重复覆盖相加为更多仓位
