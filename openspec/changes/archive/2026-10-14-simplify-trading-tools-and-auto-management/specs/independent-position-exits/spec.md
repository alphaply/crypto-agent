# Spec Delta

## MODIFIED Requirements

### Requirement: Scale-in orders manage aggregate directional positions
系统 SHALL 允许独立模式多次限价开仓或加仓，并按真实账户、持久化管理者、标的和 LONG/SHORT 管理聚合仓位；已认领范围内核验通过的观察基线及外部成交 SHALL 参与当前数量。每个 Hedge 方向 SHALL 独立管理退出周期，One-way 方向共享净仓协调；退出数量针对已成交仓位而非逐笔入场 lot。未成交入场 SHALL 不被当成可退出仓位。

#### Scenario: Several entries on the same side
- **WHEN** 同方向两笔入场分别成交 0.2 和 0.3 基础币
- **THEN** Agent 可针对合计 0.5 的该方向仓位安排退出，不必选择某一入场单

#### Scenario: One side exits in a hedge position
- **WHEN** 双向账户同时持有本管理者多仓和空仓，执行 LONG 的退出
- **THEN** 该操作仅减少多仓，空仓及其退出周期保持独立

#### Scenario: An observed baseline and external additions are managed together
- **WHEN** 认领范围内已有 0.2 基础币观察基线，后续外部加仓 0.3 已核验
- **THEN** Agent 可针对当前合计 0.5 管理退出，来源分别保留，不要求外部部分先成为虚构 Agent 入场

### Requirement: Fixed stages and dynamic all coexist
系统 SHALL 支持多个固定数量退出与明确的全平退出并存。固定单 SHALL 保留提交或显式 edit 后的数量；支持的条件 all 单 SHALL 在触发时作用于已核验由当前管理者认领的该方向实际剩余仓位，包括触发前同周期的 Agent 与外部加仓。all SHALL 不被按当前整仓数量再次计入固定档位预留总量；认领不扩大交易所实际全平能力。

#### Scenario: Two fixed stages followed by the remainder
- **WHEN** 1 BTC 多仓的两个固定止盈分别成交 0.2 和 0.3，之后全平止盈触发
- **THEN** 全平单退出当时剩余的 0.5 BTC，最终成交量以交易所证据为准

#### Scenario: Add after exits were placed
- **WHEN** 固定 0.2 的退出与 all 条件单已存在，随后同方向加仓 0.4
- **THEN** 固定退出仍为 0.2；all 保持全平语义，不固化为加仓前的数量

#### Scenario: Split stop losses
- **WHEN** Agent 提交两个不同触发价的定量止损及更远的一档 all 止损
- **THEN** 系统按相同的 fixed/all 规则处理，不要求止损只能是一笔整仓订单

#### Scenario: Human adds to a claimed position before an all trigger
- **WHEN** 已认领方向的人工加仓可核验且支持的条件 all 尚未触发
- **THEN** 动态全平按交易所原生规则作用于触发时实际剩余量，固定档位不自动增量，覆盖估计不作为成交保证

### Requirement: Confirmed cycle closure preserves cleanup behavior
系统 SHALL 在可靠确认某方向周期归零后清理该周期残留退出单及入场单，保持其他方向与其他任务不受影响。单次空仓快照 SHALL 不足以提前宣告清理完成；清理期间发生入场成交 SHALL 被重新核实并展示残仓。本能力不增加跨周期保留入场单或自动重入策略。平仓订单的执行结果 SHALL 与周期清理状态独立表达，单张旧单核验失败不得覆盖平仓事实；残留订单安全性及成交核验未完成前不得宣告周期完成或允许可能受其影响的新周期入场。已认领范围内核验通过的外部减仓及全平 SHALL 参与周期核算；清理范围 SHALL 包含已纳入该周期的外部订单，并明确其周期关联。人工重开 SHALL 根据成交边界区分原周期残仓与新周期，不伪造原 Agent 平仓成功。管理权 SHALL 不随空仓或周期 DONE 释放。

#### Scenario: Flat snapshot is temporarily stale
- **WHEN** 刚确认成交后仓位接口短暂查空
- **THEN** 系统保留周期并继续核验，不仅凭本次快照撤销全部退出单

#### Scenario: Independent cycle finishes
- **WHEN** LONG 周期已确认归零且订单清理结果可核验
- **THEN** 只清理该 LONG 周期的剩余入场与退出单，SHORT 周期保持有效，原管理者的认领保持

#### Scenario: Filled market close with incomplete cleanup
- **WHEN** 市价平空已确认成交，清理旧条件单时撤单 ACK 缺少成交量
- **THEN** 展示平仓成交和清理待核验两个事实，后台按原身份继续核验，核验完成后解除对应限制，不要求 Agent 再次平仓

#### Scenario: Entry fills during cleanup
- **WHEN** 市价平仓后旧入场单在撤销竞态中产生新增成交
- **THEN** 保留原平仓成交事实并单列新增成交与残仓，未观察到残仓前仍保留核验；不自动补平、不提前结束周期

#### Scenario: Human closes a managed position
- **WHEN** 已认领范围内外部全平成交、当前空仓及该周期残留订单可核验
- **THEN** 系统按授权清理该周期残单并结束周期，记录外部平仓来源，不要求 Agent 提交第二次平仓，不释放管理权

#### Scenario: Human reopens after flattening
- **WHEN** 人工全平后再开仓，已核验成交能证明两个周期的边界
- **THEN** 同一管理者保留原周期平仓事实；核验旧单影响并完成其清理后建立新周期，不将旧 all 单当作新仓位保护，必要的残仓管理不因旧周期标签被整体禁止

#### Scenario: External fill arrives during cleanup
- **WHEN** 已纳管的外部入场单在清理期间部分成交
- **THEN** 持续核验新增成交与实际残仓并交给 Agent 管理，不自动补平或提前报告 DONE

#### Scenario: Another manager discovers the flat scope
- **WHEN** 原周期已结束但管理权未显式释放，另一任务在该范围再次发现空仓或新外部入场
- **THEN** 保留原管理者，竞争者得到具体范围冲突，不能凭空仓、周期 DONE 或发现顺序接管

### Requirement: Missing cycles are diagnosed before ownership recovery
出现 independent_position_cycle_missing 时，系统 SHALL 区分无历史、已结束周期、同管理者可恢复历史及冲突证据，并返回当前仓位、周期状态和有权限查看的核验结果。只有同账户、管理者、标的、方向的完整执行证据与当前仓位一致且无竞争归属时，受控恢复才能修复原周期。有充分证据确认未提交的拒绝 SHALL 作为无执行影响的历史保留，不要求为其查到不存在的交易所订单。默认管理者 SHALL 在可信可交易范围内先取得持久化认领，再自动核验并记录真实观察基线或恢复原周期，不仅因外部成交或缺少周期而拒绝，不要求模型先 adopt。证据不足、账户不符、范围外及真实竞争归属 SHALL 明确缺项和受影响动作。

#### Scenario: Verified flat with no active cycle
- **WHEN** 当前方向无活跃周期且交易所可靠确认空仓
- **THEN** 返回 skipped/no_position，不创建新持仓周期、不提交平仓；认领范围内已核验挂单仍可独立纳管，已有管理权继续保留

#### Scenario: History proves the original residual position
- **WHEN** 活跃周期缺失或错误结束，但同管理者历史订单及成交完整解释当前残仓且账户和归属核验通过
- **THEN** 后台或已授权交易恢复路径可原子恢复原周期及订单关联，记录恢复证据，Agent 可在原尚未提交的管理请求内继续准入

#### Scenario: Manual or conflicting position cannot be adopted implicitly
- **WHEN** 人工仓位位于可信可交易授权范围外，或存在其他有效管理者归属或账户信息不一致
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

### Requirement: Verified external changes do not disable independent exits
已认领管理范围内核验通过的外部加减仓、订单调整和成交 SHALL 更新当前退出依据及覆盖估计，不能仅以外部来源阻止管理。固定退出参数 SHALL 保持到 Agent 显式 edit；数量冲突 SHALL 可观察且不自动重配。需要完整证据的动作 SHALL 在取证不完整时保留具体限制；已有平仓事实和无关动作 SHALL 不受其覆盖。显式批次依赖及未知原写入的实际依赖 SHALL 保持，自动纳管不使依赖项提前视作 completed。

#### Scenario: Human reduction leaves excessive fixed exits
- **WHEN** 人工减仓后多个固定退出单总量超过当前仓位
- **THEN** 展示数量冲突及最新仓位，允许有效的 cancel/edit/减仓；不自动按比例缩量，也不把冲突写成整个周期未知

#### Scenario: External addition changes close-all coverage
- **WHEN** 认领范围内发生可核验外部加仓且已有支持的条件 all
- **THEN** 按交易所实际动态全平语义更新覆盖估计，固定退出单保持原量，不承诺保证成交

#### Scenario: Enrollment does not satisfy a failed cancel dependency
- **WHEN** 批次新保护单显式依赖此前撤单 completed，但旧单撤销仍 pending 或 unknown，当前仓位已自动纳管
- **THEN** 新保护项保持 not_executed 并指出撤单依赖，不因仓位可管理绕过依赖；其他独立项按执行策略继续评估
