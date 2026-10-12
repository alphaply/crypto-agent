# Spec Delta

## MODIFIED Requirements

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
系统 SHALL 对撤单及成交待核验执行有界的前台查证和可恢复的后台重试，保留最后尝试时间、下一次核验提示与阻塞证据。单笔失败 SHALL 不妨碍收集其他订单只读证据；清理与撤建写入仍须满足原意图和依赖。暂时网络失败不得清空已确认事实，核验长期失败须可诊断而非无限静默等待。查询、后台维护与写入准入 SHALL 使用一致的证据分类；已确认无执行影响的拒绝不再进入交易所核验队列，历史分类修正不得删除原始记录或重发订单。

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

## ADDED Requirements

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
Agent、MCP、模拟交易与前端 SHALL 一致展示原操作结果、当前证据、告警及动作限制。每个阻塞 SHALL 提供作用范围、具体原因、可用的操作/订单/客户端身份、核验时间及恢复建议；未知可用性不得显示为全面允许。缓存建议 SHALL 明示为参考，执行仍重新准入；相同证据与动作不得产生相反的查询和执行判断。

#### Scenario: Query and immediate admission agree on a rejected stop
- **WHEN** 查询确认原止损无执行影响，随后无证据变化地请求新的有效止损
- **THEN** 查询不显示该拒绝为待核验，执行也不以同一拒绝记录阻塞

#### Scenario: Target-specific warning does not imply every edit is blocked
- **WHEN** 仅订单 A 的修改结果未知，订单 B 的修改依赖可独立核实
- **THEN** 展示 A 的限制及 B 需按目标核验的事实，不用一个全局 edit 禁止标记代替目标范围

#### Scenario: Missing evidence is not an empty allow list
- **WHEN** 当前可用性未核验或查询失败，只保留旧操作结果
- **THEN** 展示待刷新及缺失证据，不把空 blocked_actions 解释为所有新交易可执行
