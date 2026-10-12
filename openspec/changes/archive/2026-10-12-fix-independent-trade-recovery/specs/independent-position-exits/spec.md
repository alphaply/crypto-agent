# Spec Delta

## MODIFIED Requirements

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
内置 Agent 上下文、MCP 查询和前端展示 SHALL 对齐 fixed/all、订单类型、触发/限价、当前有效身份、真实剩余量、SL 覆盖估计与数量冲突。未触发或限价止损的挂单覆盖 SHALL 不被描述为保证成交。模拟与实盘 SHALL 采用相同数量语义与周期规则。已终态但成交仍待核验的订单 SHALL 在单独的待核验记录中保持可见，包含身份、最后已知成交、未核实事项及时间，不把最后已知量当成最终成交量或有效保护覆盖。

#### Scenario: Dynamic coverage display
- **WHEN** 账户存在一笔已核验的 stop_market all 与部分定量止盈
- **THEN** 展示止损为“剩余全部”和当前覆盖估计，定量止盈保持自身剩余量，不把重复覆盖相加为更多仓位

#### Scenario: Canceled order still needs fill verification
- **WHEN** 入场单或退出单已确认撤销但最终成交量未确认
- **THEN** 挂单列表可将其排除出活跃单，但 Agent、MCP 和前端的待核验视图保留该单及原操作关联，不只显示笼统 pending

## ADDED Requirements

### Requirement: Continuation is scoped to verified trading dependencies
后续交易 SHALL 在执行时按账户、任务、标的、方向、订单关联及持仓模式检查准入；清理告警不得一律使所有交易结果未知，也不得直接放行所有写入。与阻塞事实无关的动作可经正常校验继续；涉及未知成交、未清理 all、晚到入场或归属不明的相关动作 SHALL 保留限制。只读核验 SHALL 始终可在既有授权范围内进行。

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

### Requirement: Missing cycles are diagnosed before ownership recovery
出现 independent_position_cycle_missing 时，系统 SHALL 区分无历史、已结束周期、同任务可恢复历史及冲突证据，并返回当前仓位、周期状态和有权限查看的核验结果。只有同账户、任务、标的、方向的完整入场/退出证据与当前仓位一致且无竞争归属时，受控恢复才能修复原周期。证据不足 SHALL 明确缺项，外部仓位接管继续要求既有显式授权流程。

#### Scenario: Verified flat with no active cycle
- **WHEN** 当前方向无活跃周期且交易所可靠确认空仓
- **THEN** 返回 skipped/no_position，不创建新周期、不提交平仓

#### Scenario: History proves the original residual position
- **WHEN** 活跃周期缺失或错误结束，但同任务历史订单及成交完整解释当前残仓且账户和归属核验通过
- **THEN** 后台或已授权交易恢复路径可原子恢复原周期及订单关联，记录恢复证据，Agent 随后可管理该残仓

#### Scenario: Manual or conflicting position cannot be adopted implicitly
- **WHEN** 仓位无法由原周期完整解释、存在其他任务归属或账户信息不一致
- **THEN** 保持结构化失败与具体恢复建议，不通过新建入场、清空错误、伪造周期或自动 adopt 解除限制

### Requirement: Reconciliation makes bounded progress
系统 SHALL 对撤单及成交待核验执行有界的前台查证和可恢复的后台重试，保留最后尝试时间、下一次核验提示与阻塞证据。单笔失败 SHALL 不妨碍收集其他订单只读证据；清理与撤建写入仍须满足原意图和依赖。暂时网络失败不得清空已确认事实，核验长期失败须可诊断而非无限静默等待。

#### Scenario: Cancellation acknowledgement lacks fill fields
- **WHEN** 撤销 ACK 已确认但缺少累计成交，随后原身份查单返回完整终态
- **THEN** 在允许的查证窗口或后续后台轮次完成恢复，不必要求 Agent 再次发起撤单或等到下一次交易请求

#### Scenario: One unresolved order among several
- **WHEN** 某张旧单查证失败，其他订单仍能可靠查询
- **THEN** 更新其他订单的证据并保留具体未解决项，不将所有订单状态一起降级或越过写入依赖
