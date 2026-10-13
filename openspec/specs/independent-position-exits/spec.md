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
系统 SHALL 在可靠确认某方向周期归零后清理该周期残留退出单及入场单，保持其他方向与其他任务不受影响。单次空仓快照 SHALL 不足以提前宣告清理完成；清理期间发生入场成交 SHALL 被重新核实并展示残仓。本能力不增加跨周期保留入场单或自动重入策略。平仓订单的执行结果 SHALL 与周期清理状态独立表达，单张旧单核验失败不得覆盖平仓事实；残留订单安全性及成交核验未完成前不得宣告周期完成或允许可能受其影响的新周期入场。

#### Scenario: Flat snapshot is temporarily stale
- **WHEN** 刚确认成交后仓位接口短暂查空
- **THEN** 系统保留周期并继续核验，不仅凭本次快照撤销全部退出单

#### Scenario: Independent cycle finishes
- **WHEN** LONG 周期已确认归零且订单清理结果可核验
- **THEN** 只清理该 LONG 周期的剩余入场与退出单，SHORT 周期保持有效

#### Scenario: Filled market close with incomplete cleanup
- **WHEN** 市价平空已确认成交，清理旧条件单时撤单 ACK 缺少成交量
- **THEN** 展示平仓成交和清理待核验两个事实，后台按原身份继续核验，核验完成后解除对应限制，不要求 Agent 再次平仓

#### Scenario: Entry fills during cleanup
- **WHEN** 市价平仓后旧入场单在撤销竞态中产生新增成交
- **THEN** 保留原平仓成交事实并单列新增成交与残仓，未观察到残仓前仍保留核验；不自动补平、不提前结束周期

### Requirement: Visible state is consistent across real and simulated trading
内置 Agent 上下文、MCP 查询和前端展示 SHALL 对齐 fixed/all、订单类型、触发/限价、当前有效身份、真实剩余量、SL 覆盖估计与数量冲突；一致性指事实和语义一致，不要求默认输入包含相同的完整历史回执。未触发或限价止损的挂单覆盖 SHALL 不被描述为保证成交。模拟与实盘 SHALL 采用相同数量语义与周期规则。已终态但成交仍待核验的订单 SHALL 在单独的待核验记录中保持可见，包含身份、最后已知成交、未核实事项及时间，不把最后已知量当成最终成交量或有效保护覆盖。默认决策上下文 SHALL 仅呈现当前相关异常；完整历史保留按需查询。

#### Scenario: Dynamic coverage display
- **WHEN** 账户存在一笔已核验的 stop_market all 与部分定量止盈
- **THEN** 展示止损为“剩余全部”和当前覆盖估计，定量止盈保持自身剩余量，不把重复覆盖相加为更多仓位

#### Scenario: Canceled order still needs fill verification
- **WHEN** 入场单或退出单已确认撤销但最终成交量未确认
- **THEN** 挂单列表可将其排除出活跃单，但 Agent 当前相关异常、MCP 和前端的待核验视图保留该单及原操作关联，不只显示笼统 pending

#### Scenario: Historical receipts and current views have different detail
- **WHEN** 原周期已确认结束且无残留影响，当前为新的已核验周期
- **THEN** Agent 默认状态只展示当前周期事实及相关异常，MCP 和历史页面仍能查询原周期完整回执，两者不把旧周期持仓当成当前持仓

### Requirement: Continuation is scoped to verified trading dependencies
后续交易 SHALL 在执行时按账户、任务、标的、方向、目标订单、动作意图及持仓模式检查准入；每项限制 SHALL 对应本次动作依赖的具体证据。历史错误、明确拒绝、无关订单读取失败或周期维护状态本身不得阻止依赖已核实的动作。涉及未知提交/成交、旧 all、晚到入场或归属不明的相关动作 SHALL 保留限制。权限、幂等、有效数量、方向及交易所能力约束 SHALL 保留；只读核验 SHALL 始终可在既有授权范围内进行。

#### Scenario: New entry after verified closure
- **WHEN** 原平仓成交、残留订单终态与成交量、当前空仓和周期完成均已核验，Agent 明确请求新入场
- **THEN** 同轮后续决策或下一轮可按正常交易规则创建新周期，不因旧错误文本或旧回执缓存阻塞

#### Scenario: Uncertain all cannot close a later position
- **WHEN** 旧条件 all 是否仍有效或是否已触发未核实
- **THEN** 阻止同方向新入场，回执给出阻塞订单与查询方式，不因平仓订单 completed 放行

#### Scenario: Independent opposite side in hedge mode
- **WHEN** Hedge 模式下某方向旧单清理待核验，而另一方向的仓位、订单归属和目标操作依赖均已核实且无共享冲突
- **THEN** 另一方向可按正常检查执行，不把前一方向的清理异常扩大为账户全局未知

#### Scenario: Reduce a verified residual
- **WHEN** 原平仓已终态且成交已核实，晚到入场形成可核验残仓，其他相关在途写入和退出竞态已排除
- **THEN** Agent 可显式请求对真实残仓减仓或设置有效保护，不自动补平，也不重放原全平数量

#### Scenario: Rejected stop does not block a corrected stop
- **WHEN** 原止损有充分证据确认未被接受、没有成交或活跃子单，当前仓位归属和数量已核验，Agent 显式提交新的有效止损
- **THEN** 原止损保持 failed，新止损经正常校验执行；原记录没有交易所 ID 不构成阻塞

#### Scenario: Unknown market close still blocks a second dependent close
- **WHEN** 原市价平仓请求可能已被接受且执行结果未确认，又请求同方向减仓或依据旧数量设置保护
- **THEN** 仅阻止依赖该未知执行结果的动作并指向原操作核验，不因动作名为 close 或新操作 ID 而放行

#### Scenario: Target cancellation does not require unrelated position reads
- **WHEN** 已核实本任务指定订单身份、归属及当前可撤状态，但无关订单或仓位读取暂时失败
- **THEN** 允许按目标身份撤销；目标撤销或成交待核验单独返回，不宣告整仓已核验

#### Scenario: Edit follows the target and requested effect
- **WHEN** 修改指定订单，且目标状态、修改量所需成交证据、对应增仓或退出权限以及实际必要的仓位证据已核实
- **THEN** 无关历史失败不得拦截修改；若目标存在未明撤建或修改，则阻止冲突修改并返回其操作身份

#### Scenario: One-way shared exposure requires shared evidence
- **WHEN** One-way 模式下对向未知操作可能改变同一净仓位
- **THEN** 依赖该净仓位的增仓、减仓和修改保留限制，不套用 Hedge 的方向隔离

### Requirement: Missing cycles are diagnosed before ownership recovery
出现 independent_position_cycle_missing 时，系统 SHALL 区分无历史、已结束周期、同任务可恢复历史及冲突证据，并返回当前仓位、周期状态和有权限查看的核验结果。只有同账户、任务、标的、方向的完整执行证据与当前仓位一致且无竞争归属时，受控恢复才能修复原周期。有充分证据确认未提交的拒绝 SHALL 作为无执行影响的历史保留，不要求为其查到不存在的交易所订单。证据不足 SHALL 明确缺项，外部仓位接管继续要求既有显式授权流程。

#### Scenario: Verified flat with no active cycle
- **WHEN** 当前方向无活跃周期且交易所可靠确认空仓
- **THEN** 返回 skipped/no_position，不创建新周期、不提交平仓

#### Scenario: History proves the original residual position
- **WHEN** 活跃周期缺失或错误结束，但同任务历史订单及成交完整解释当前残仓且账户和归属核验通过
- **THEN** 后台或已授权交易恢复路径可原子恢复原周期及订单关联，记录恢复证据，Agent 随后可管理该残仓

#### Scenario: Manual or conflicting position cannot be adopted implicitly
- **WHEN** 仓位无法由原周期完整解释、存在其他任务归属或账户信息不一致
- **THEN** 保持结构化失败与具体恢复建议，不通过新建入场、清空错误、伪造周期或自动 adopt 解除限制

#### Scenario: Rejected attempt does not invalidate complete execution history
- **WHEN** 原周期真实订单及成交完整解释当前仓位，另有确认未提交且无执行影响的拒绝记录没有订单 ID
- **THEN** 该拒绝不导致 incomplete_history 或缺少订单归属；恢复保留其审计记录及原 episode_id

### Requirement: Reconciliation makes bounded progress
系统 SHALL 对撤单及成交待核验执行有界的前台查证和可恢复的后台重试，保留最后尝试时间、下一次核验提示与阻塞证据。单笔失败 SHALL 不妨碍收集其他订单只读证据；清理与撤建写入仍须满足原意图和依赖。暂时网络失败不得清空已确认事实，核验长期失败须可诊断而非无限静默等待。查询、后台维护与写入准入 SHALL 使用一致的证据分类；已确认无执行影响的拒绝不再进入交易所核验队列，历史分类修正不得删除原始记录或重发订单。已终结且无未解决关联的操作 SHALL 不因常规维护反复生成无事实变化的回执修订或刷新其证据时间；新相关证据仍须更新关联结果，按需查询继续可用。

#### Scenario: Cancellation acknowledgement lacks fill fields
- **WHEN** 撤销 ACK 已确认但缺少累计成交，随后原身份查单返回完整终态
- **THEN** 在允许的查证窗口或后续后台轮次完成恢复，不必要求 Agent 再次发起撤单或等到下一次交易请求

#### Scenario: One unresolved order among several
- **WHEN** 某张旧单查证失败，其他订单仍能可靠查询
- **THEN** 更新其他订单的证据并保留具体未解决项，不将所有订单状态一起降级或越过写入依赖

#### Scenario: Legacy rejection is classified from evidence
- **WHEN** 升级后加载一条没有订单 ID 的历史 rejected 记录
- **THEN** 仅在原阶段、请求结果及关联记录证明没有执行影响时解除其限制；证据含糊时保留具体待核验事项，不因 rejected 字样直接放行

#### Scenario: Persistent read failure remains actionable
- **WHEN** 未知提交在多次查证后仍无法确认
- **THEN** 保留最后错误、尝试次数、下次重试和受影响动作；不以超时、订单不存在或单次空仓自动证明未执行，也不阻止无依赖动作

#### Scenario: Unchanged terminal history stays historical
- **WHEN** 连续后台维护期间，一笔已结束操作及关联订单、清理结果均无证据变化
- **THEN** 不反复刷新该操作的回执修订、更新时间或核验时间，默认状态不因维护轮次将它重新列为当前事项

#### Scenario: A completed execution still needs cleanup updates
- **WHEN** 原平仓 completed，但关联清理或晚到入场仍未核实
- **THEN** 后台继续推进相关证据并更新原操作投影，不因为顶层 completed 就停止恢复，也不重复提交平仓

### Requirement: Unrelated diagnostics cannot reduce action availability
系统 SHALL 以顺畅执行有效交易为默认行为。每个阻塞 SHALL 对应当前请求的具体硬约束、已声明依赖或必须核实的相关执行事实；不得仅因旧错误数量、动作同方向、调用顺序猜测或无关异常增加限制。加入无关历史诊断 SHALL 不改变相同请求在相同相关证据下的准入结论。

#### Scenario: Adding historical failures leaves valid trading available
- **WHEN** 某请求可正常执行，随后仅向上下文加入多条无执行影响的历史拒绝或其他作用范围的维护错误
- **THEN** 该请求仍可执行，新增历史只作为诊断，不导致额外预检、解锁或等待下一轮

#### Scenario: Every refusal identifies a necessary condition
- **WHEN** 系统拒绝本次动作
- **THEN** 回执指出未满足的具体权限/交易所约束、已声明依赖或相关未核实事实，不能仅返回“为安全起见”或“前序操作失败”作为规范工具准入原因

### Requirement: Admission separates order evidence from historical diagnostics
系统 SHALL 分别识别确认未提交、提交结果未知、已接受且状态可核验、终态成交待核验及已确认终态。缺少订单 ID 不得单独证明待核验；存在 ID 也不得证明执行已核实。状态为 rejected 的交易所订单、改单拒绝和替代单拒绝 SHALL 按各自实际执行阶段与原单/子单证据判断，不统一视为零成交。告警 SHALL 保留可见，但仅与目标动作有依赖关系的未核实事实可以形成阻塞。

#### Scenario: Waiting conditional all is an ordinary live order
- **WHEN** 原生 all 条件单已核实等待触发，只是没有固定数量字段
- **THEN** 其状态作为正常在途展示，不因缺少数量升级为整个方向不可交易

#### Scenario: Failed amendment leaves its original order relevant
- **WHEN** 一次改单被明确拒绝，但原订单仍然活跃
- **THEN** 拒绝的修改不继续占用未知操作状态；后续动作仍考虑原活跃单及其真实成交影响

#### Scenario: Terminal label does not hide unresolved execution
- **WHEN** rejected 或其他终态记录仍关联未核实的成交、触发子单或替代请求
- **THEN** 对相关动作保留限制并指出待核实的执行身份，不将终态标签解释为没有执行影响

### Requirement: Maintenance does not gate unrelated trading intent
新交易 SHALL 只以自身依赖的证据作为准入前提，不要求整个周期维护成功。下单前的准入取证 SHALL 不顺带恢复其他订单撤建或清理；本次请求必需的显式撤建除外。后台仍 SHALL 继续原有已授权维护，可靠归零后按既有规则清理周期，不因交易与维护分离而遗漏清理。

#### Scenario: Unrelated maintenance failure does not veto a verified target
- **WHEN** 某历史订单维护失败，但新动作的权限、目标、仓位及相关执行竞态已经核实且与其无依赖
- **THEN** 新动作可执行，维护失败保留为告警，不先尝试完成无关维护

#### Scenario: Post-trade cleanup fails after a confirmed fill
- **WHEN** 本次平仓已确认成交，随后周期维护失败
- **THEN** 回执保留确认成交，单列维护问题和具体后续限制，不把本次动作降级为执行未知

### Requirement: Trading availability explains specific dependencies
Agent、MCP、模拟交易与前端 SHALL 以一致事实表达原操作结果、当前证据、告警及动作限制；默认 Agent 输入仅投影当前相关部分，历史诊断保留按需查看。每个阻塞 SHALL 提供作用范围、具体原因、可用的操作/订单/客户端身份、真实证据观察时间及恢复建议；未知可用性不得显示为全面允许。缓存建议 SHALL 明示为参考，执行仍重新准入；相同证据与动作不得产生相反的查询和执行判断。旧周期或未刷新建议 SHALL 不作为当前禁止或允许结论，单纯历史 error 不构成整个周期待核验。

#### Scenario: Query and immediate admission agree on a rejected stop
- **WHEN** 查询确认原止损无执行影响，随后无证据变化地请求新的有效止损
- **THEN** 查询不显示该拒绝为待核验，执行也不以同一拒绝记录阻塞

#### Scenario: Target-specific warning does not imply every edit is blocked
- **WHEN** 仅订单 A 的修改结果未知，订单 B 的修改依赖可独立核实
- **THEN** 展示 A 的限制及 B 需按目标核验的事实，不用一个全局 edit 禁止标记代替目标范围

#### Scenario: Missing evidence is not an empty allow list
- **WHEN** 当前可用性未核验或查询失败，只保留旧操作结果
- **THEN** 展示待刷新及缺失证据，不把空 blocked_actions 解释为所有新交易可执行，也不将旧 blocked_actions 继续展示为当前封锁

#### Scenario: Historical diagnostic does not change coverage certainty
- **WHEN** 当前仓位和止损单证据已核验，周期保留一条无执行影响的旧错误说明
- **THEN** 当前保护覆盖与异常状态不因该错误字符串降级，旧说明仍可在审计中查看
