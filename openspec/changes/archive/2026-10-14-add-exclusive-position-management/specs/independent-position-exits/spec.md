# Spec Delta

## MODIFIED Requirements

### Requirement: Confirmed cycle closure preserves cleanup behavior
系统 SHALL 在可靠确认某方向周期归零后清理该周期残留退出单及入场单，保持其他方向与其他任务不受影响。单次空仓快照 SHALL 不足以提前宣告清理完成；清理期间发生入场成交 SHALL 被重新核实并展示残仓。本能力不增加跨周期保留入场单或自动重入策略。平仓订单的执行结果 SHALL 与周期清理状态独立表达，单张旧单核验失败不得覆盖平仓事实；残留订单安全性及成交核验未完成前不得宣告周期完成或允许可能受其影响的新周期入场。独占管理下已核验的外部减仓及全平 SHALL 参与周期核算；清理范围 SHALL 包含已纳入该周期的外部订单，并明确其周期关联。人工重开 SHALL 根据成交边界区分原周期残仓与新周期，不伪造原 Agent 平仓成功。

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

#### Scenario: Human closes a managed position
- **WHEN** 独占范围内外部全平成交、当前空仓及该周期残留订单可核验
- **THEN** 系统按授权清理该周期残单并结束周期，记录外部平仓来源，不要求 Agent 提交第二次平仓

#### Scenario: Human reopens after flattening
- **WHEN** 人工全平后再开仓，已核验成交能证明两个周期的边界
- **THEN** 原周期平仓事实保留；核验旧单影响并完成其清理后建立新周期，不将旧 all 单当作新仓位保护，必要的残仓管理不因旧周期标签被整体禁止

#### Scenario: External fill arrives during cleanup
- **WHEN** 已纳管的外部入场单在清理期间部分成交
- **THEN** 持续核验新增成交与实际残仓并交给 Agent 管理，不自动补平或提前报告 DONE

### Requirement: Missing cycles are diagnosed before ownership recovery
出现 independent_position_cycle_missing 时，系统 SHALL 区分无历史、已结束周期、同任务可恢复历史及冲突证据，并返回当前仓位、周期状态和有权限查看的核验结果。只有同账户、任务、标的、方向的完整执行证据与当前仓位一致且无竞争归属时，受控恢复才能修复原周期。有充分证据确认未提交的拒绝 SHALL 作为无执行影响的历史保留，不要求为其查到不存在的交易所订单。无独占授权的外部仓位接管继续要求既有显式授权流程；已配置独占范围的管理者 SHALL 在自动核验后记录真实接管基线或恢复原周期，不仅因外部成交或缺少周期而拒绝管理。证据不足、账户不符及真实竞争归属 SHALL 明确缺项和受影响动作。

#### Scenario: Verified flat with no active cycle
- **WHEN** 当前方向无活跃周期且交易所可靠确认空仓
- **THEN** 返回 skipped/no_position，不创建新周期、不提交平仓；独占范围内已核验挂单仍可独立纳管

#### Scenario: History proves the original residual position
- **WHEN** 活跃周期缺失或错误结束，但同任务历史订单及成交完整解释当前残仓且账户和归属核验通过
- **THEN** 后台或已授权交易恢复路径可原子恢复原周期及订单关联，记录恢复证据，Agent 随后可管理该残仓

#### Scenario: Manual or conflicting position cannot be adopted implicitly
- **WHEN** 仓位无法由原周期完整解释且未启用独占管理，或存在其他有效任务归属或账户信息不一致
- **THEN** 保持结构化失败与具体恢复建议，不通过新建入场、清空错误、伪造周期或未经授权的自动 adopt 解除限制

#### Scenario: Rejected attempt does not invalidate complete execution history
- **WHEN** 原周期真实订单及成交完整解释当前仓位，另有确认未提交且无执行影响的拒绝记录没有订单 ID
- **THEN** 该拒绝不导致 incomplete_history 或缺少订单归属；恢复保留其审计记录及原 episode_id

#### Scenario: Exclusive authorization permits automatic baseline enrollment
- **WHEN** 有独占授权、无可恢复原周期且当前外部仓位及相关订单可以核验
- **THEN** 建立可追溯管理基线与周期，不伪造历史开仓成交，并允许原管理请求继续正常准入

## ADDED Requirements

### Requirement: Verified external changes do not disable independent exits
独占管理范围内已核验的外部加减仓、订单调整和成交 SHALL 更新当前退出依据及覆盖估计，不能仅以外部来源阻止管理。固定退出参数 SHALL 保持到 Agent 显式 edit；数量冲突 SHALL 可观察且不自动重配。需要完整证据的动作 SHALL 在取证不完整时保留具体限制；已有平仓事实和无关动作 SHALL 不受其覆盖。

#### Scenario: Human reduction leaves excessive fixed exits
- **WHEN** 人工减仓后多个固定退出单总量超过当前仓位
- **THEN** 展示数量冲突及最新仓位，允许有效的 cancel/edit/减仓；不自动按比例缩量，也不把冲突写成整个周期未知

#### Scenario: External addition changes close-all coverage
- **WHEN** 独占管理中发生可核验外部加仓且已有支持的条件 all
- **THEN** 按交易所实际动态全平语义更新覆盖估计，固定退出单保持原量，不承诺保证成交
