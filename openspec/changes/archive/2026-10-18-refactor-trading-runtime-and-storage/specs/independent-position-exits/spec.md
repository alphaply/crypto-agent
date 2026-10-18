## MODIFIED Requirements

### Requirement: Confirmed cycle closure preserves cleanup behavior
系统 SHALL 在明确 Agent 全平或清理意图及可靠确认某方向周期归零后清理该周期残留退出单及入场单，保持其他方向与其他任务不受影响。单次空仓快照 SHALL 不足以提前宣告清理完成；清理期间发生入场成交 SHALL 被重新核实并展示残仓。外部人工全平不构成清理授权；仍有效的挂单保留给后续 Agent 决策，不自动重入。平仓订单的执行结果 SHALL 与周期清理状态独立表达，单张旧单核验失败不得覆盖平仓事实；残留订单的相关状态或成交未核验时仍限制依赖它的动作，不把已核验且保留的外部挂单本身作为全局阻断。已认领范围内核验通过的外部减仓及全平 SHALL 参与周期核算；清理范围 SHALL 包含已纳入该周期的外部订单，并明确其周期关联。人工重开 SHALL 根据成交边界区分原周期残仓与新周期，不伪造原 Agent 平仓成功。管理权 SHALL 不随空仓或周期 DONE 释放。

#### Scenario: Flat snapshot is temporarily stale
- **WHEN** 刚确认成交后仓位接口短暂查空
- **THEN** 系统保留周期并继续核验，不仅凭本次快照撤销全部退出单

#### Scenario: Independent cycle finishes
- **WHEN** 因明确 Agent 全平或清理意图而结束的 LONG 周期已确认归零且订单清理结果可核验
- **THEN** 只清理该 LONG 周期的剩余入场与退出单，SHORT 周期保持有效，原管理者的认领保持

#### Scenario: Filled market close with incomplete cleanup
- **WHEN** 市价平空已确认成交，清理旧条件单时撤单 ACK 缺少成交量
- **THEN** 展示平仓成交和清理待核验两个事实，后台按原身份继续核验，核验完成后解除对应限制，不要求 Agent 再次平仓

#### Scenario: Entry fills during cleanup
- **WHEN** 市价平仓后旧入场单在撤销竞态中产生新增成交
- **THEN** 保留原平仓成交事实并单列新增成交与残仓，未观察到残仓前仍保留核验；不自动补平、不提前结束周期

#### Scenario: Human closes a managed position
- **WHEN** 已认领范围内外部全平成交、当前空仓及该周期残留订单可核验
- **THEN** 记录外部平仓及当前实际挂单，不自动撤单或补仓，后续 Agent 可继续管理，不要求第二次平仓，不丢失原执行身份

#### Scenario: Human reopens after flattening
- **WHEN** 人工全平后再开仓，已核验成交能证明两个周期的边界
- **THEN** 保留原周期平仓事实并记录新仓位，核验并如实表达仍有效的旧订单及其作用，不自行撤单或把旧单伪造为新创建保护，不因旧周期标签整体禁止管理

#### Scenario: External fill arrives during cleanup
- **WHEN** 已纳管的外部入场单在清理期间部分成交
- **THEN** 持续核验新增成交与实际残仓并交给 Agent 管理，不自动补平或提前报告 DONE

#### Scenario: Another manager discovers the flat scope
- **WHEN** 原周期已结束但管理权未显式释放，另一任务在该范围再次发现空仓或新外部入场
- **THEN** 保留原来源审计，其他已获授权调用者按共享资源协调及当前事实执行，不因历史管理者或周期标签拒绝

### Requirement: All is bounded by execution capability
立即市价 all SHALL 在提交前读取当前可管理仓位并按该次核验数量下单；条件市价 all SHALL 使用交易所原生触发时全平能力。普通限价和条件限价 all SHALL 在本能力范围内被明确拒绝。全平 SHALL 不隐含终止未来所有开仓或保证覆盖提交/触发之后的并发入场成交。

#### Scenario: Market all with a later entry fill
- **WHEN** 市价 all 按核验到的 0.5 提交后，另一入场单又成交 0.1
- **THEN** 系统记录真实残余仓位并交由 Agent 管理，不自动追加一笔平仓来追求零仓位

#### Scenario: Cannot isolate shared virtual inventory
- **WHEN** 可信授权范围、该方向实际仓位或交易所原生全平能力无法核验，或请求试图把整方向全平限制为本地虚拟份额
- **THEN** 系统拒绝依赖缺失证据或不支持语义的条件 all；已授权共管本身不构成拒绝理由，不将交易所整方向全平描述为只退出本地某一虚拟份额

### Requirement: Missing cycles are diagnosed before ownership recovery
出现 independent_position_cycle_missing 时，系统 SHALL 区分无历史、已结束周期、同管理者可恢复历史及冲突证据，并返回当前仓位、周期状态和有权限查看的核验结果。只有原账户、标的、方向及原始执行身份的完整证据与当前仓位一致且无未核验执行冲突时，受控恢复才能修复原周期；其他获授权管理者的存在不构成独占限制。有充分证据确认未提交的拒绝 SHALL 作为无执行影响的历史保留，不要求为其查到不存在的交易所订单。默认管理者 SHALL 在可信可交易范围内先协调共享资源并记录管理关联，再自动核验并记录真实观察基线或恢复原周期，不仅因外部成交或缺少周期而拒绝，不要求模型先 adopt。证据不足、账户不符、范围外及相关未核验执行 SHALL 明确缺项和受影响动作。

#### Scenario: Verified flat with no active cycle
- **WHEN** 当前方向无活跃周期且交易所可靠确认空仓
- **THEN** 返回 skipped/no_position，不创建新持仓周期、不提交平仓；认领范围内已核验挂单仍可独立纳管，已有管理权继续保留

#### Scenario: History proves the original residual position
- **WHEN** 活跃周期缺失或错误结束，但同管理者历史订单及成交完整解释当前残仓且账户和归属核验通过
- **THEN** 后台或已授权交易恢复路径可原子恢复原周期及订单关联，记录恢复证据，Agent 可在原尚未提交的管理请求内继续准入

#### Scenario: Manual or conflicting position cannot be adopted implicitly
- **WHEN** 人工仓位位于可信可交易授权范围外，或账户信息不一致、相关原执行身份无法核验
- **THEN** 保持结构化失败与具体范围原因，不通过新建入场、清空错误、伪造周期或越权认领解除限制，不阻断无关已认领范围

#### Scenario: Rejected attempt does not invalidate complete execution history
- **WHEN** 原周期真实订单及成交完整解释当前仓位，另有确认未提交且无执行影响的拒绝记录没有订单 ID
- **THEN** 该拒绝不导致 incomplete_history 或缺少订单归属；恢复保留其审计记录及原 episode_id

#### Scenario: Exclusive authorization permits automatic baseline enrollment
- **WHEN** 原已授权管理者或默认 visible_first_claim 管理者持有认领，无可恢复原周期且当前外部仓位及相关订单可以核验
- **THEN** 建立可追溯管理基线与周期，不伪造历史开仓成交，并允许原管理请求继续正常准入，不要求新手填独占清单或 adopt

#### Scenario: Read-only cycle diagnosis has no management writes
- **WHEN** 只读调用者查询缺周期但可见的仓位或订单
- **THEN** 返回当前事实、已有或未认领管理者及缺项，不通过读取建立认领、基线或周期，也不恢复撤建交易写入
