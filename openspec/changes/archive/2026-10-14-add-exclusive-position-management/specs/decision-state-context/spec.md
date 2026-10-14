# Spec Delta

## ADDED Requirements

### Requirement: Current context describes management scope and external changes
内置 Agent、绑定任务聊天、MCP 与前端 SHALL 以一致事实展示独占范围、管理者、当前仓位、已纳管外部订单、变化来源、真实观察时间和对账完整性。外部来源不确定时 SHALL 标识为外部/未跟踪活动，不能推断一定由人类操作。默认上下文 SHALL 使用当前相关事实及变化摘要，不逐轮注入全部外部成交历史。

#### Scenario: External reduction is reconciled
- **WHEN** 独占范围内发生可核验外部减仓
- **THEN** 当前数量、退出覆盖和数量冲突同时更新，默认状态说明外部变化，不再仅因其来源提示归属失败

#### Scenario: External order is enrolled
- **WHEN** 外部订单纳管完成且可在既有权限下管理
- **THEN** 展示真实 order_id、入场/退出作用、原生参数、来源及可用动作，不能仍仅标为 external_or_untracked/modifiable=false

#### Scenario: Only one source is incomplete
- **WHEN** 普通订单可读而条件订单或成交证据不完整
- **THEN** 标明具体缺失来源及其观察时间，不显示“已确认没有异常”或空仓，也不把所有动作统一标为禁止

### Requirement: External closure preserves original and current meanings
外部全平、原周期结束及新周期建立 SHALL 在当前状态和历史中区分。当前视图 SHALL 使用新周期事实；旧 Agent 操作和外部活动保留历史身份。外部动作 SHALL NOT 被写成 Agent 原操作的成交结果；已解决的外部变化 SHALL NOT 持续作为当前阻塞项。

#### Scenario: Human closes and opens a new position
- **WHEN** 人工全平及新开仓已核验，旧周期清理完成且新周期已登记
- **THEN** 默认上下文展示当前新仓及其保护，历史可查旧周期与外部平仓，不沿用旧周期数量、旧 all 覆盖或旧准入建议

### Requirement: Memory prose contains only strategy judgments levels and lessons
新生成及默认展示的记忆正文 SHALL 只包含策略判断、关键价位和有效教训，可保留有策略意义的观察条件。operation_id、resolution、周期报错、执行解锁流程、当前仓位/挂单快照、收益统计和账本 JSON SHALL NOT 写入记忆正文。当前交易事实 SHALL 来自独立的最新状态；原始历史证据及关联元数据 SHALL 保留在后台或按需详情，不通过旧记忆回退重新注入。

#### Scenario: Consolidate the supplied ETH memory
- **WHEN** 整理素材含两次 Edit 拒绝、缺少周期的失败批次、null 操作身份、未执行尾项及收益账本，另有四小时收盘或 CPI 观察条件
- **THEN** 新记忆保留策略判断、相关价位和有效教训，移除内部字段及历史错误复述；市场观察条件保留，执行解锁清单不进入记忆

#### Scenario: Historical noisy memory remains auditable
- **WHEN** 已存记忆正文包含 operation_id、resolution 或周期诊断
- **THEN** 经版本化后台转换发布纯策略视图，旧原文可按需审计；转换失败不回退将旧噪声注入默认策略正文，也不删除旧证据

### Requirement: Memory consolidation separates evidence plumbing from narrative input
记忆整理 SHALL 使用按语义筛选的策略素材，不默认拼接完整工具回执、周期账本、同步区间及收益统计。执行引用、来源校验和恢复状态 SHALL 由程序维护，与记忆正文及策略判断分开。精简 SHALL 依据事实分类，不按字符截断策略理由或关键价位；明示未提交的失败和未执行项不得作为待恢复交易反复复述。

#### Scenario: Definite rejections and unexecuted tails
- **WHEN** 原批次确认只有提交前失败且后续项未执行，或改单因归属校验被拒
- **THEN** 原回执保留于历史，记忆不复制失败计数或操作 ID，也不要求为不存在的交易所提交执行恢复

#### Scenario: Genuine unknown submission
- **WHEN** 存在可能已发送的真实交易写入而其结果仍未知
- **THEN** 后台保留原身份并核验；其必要影响通过当前相关事项简述，不能因精简记忆而重复下单或报告已解决

### Requirement: Current facts and restrictions are independent of historical cycle diagnostics
当前持仓 SHALL 以实际交易所观察为准；无本地周期、无已归属成交或历史账本开放周期为空 SHALL NOT 被表述为交易所空仓。历史引用读取失败、null 身份、旧归属拒绝及 unavailable 状态 SHALL NOT 单独形成当前交易禁令。对账与恢复 SHALL 由后台及执行层处理，只有当前动作真实依赖的未核实事实才进入简明待处理事项。

#### Scenario: Exchange position exists without an owned cycle
- **WHEN** 当前快照有 ETH 多仓和外部挂单，本地没有自有周期或已归属成交
- **THEN** 如实展示交易所仓位与挂单及实际可管理状态，不写“账户持仓为无”；独占授权下后台纳管，工具正常核验后管理

#### Scenario: Unrelated historical operation cannot be read
- **WHEN** 某旧操作 unavailable 且无法证明其对本次动作有实际影响，当前所需证据已满足
- **THEN** 不要求 Agent 查历史 ID 解锁，不增加“不新开仓”执行禁令；历史诊断可在详情中查看

### Requirement: Default performance presents equity change profit and drawdown only
默认收益区域 SHALL 只呈现权益变化、盈亏和回撤三类指标，标明简短区间、截至时间及必要口径。权益快照变化 SHALL 与可归属交易盈亏区分，不能相加或互相替代；收益指标 SHALL NOT 等待完整周期闭合才展示已可靠核验的金额。缺失、归属或覆盖不足 SHALL 显示暂不可统计，不以零值暗示完整结果；胜率、周期数和同步记录不默认展示。

#### Scenario: The supplied incomplete seven-day ledger
- **WHEN** 权益快照可计算，但近七天有未知归属成交、完整周期为零且覆盖不足
- **THEN** 展示已知权益变化及快照回撤，盈亏显示暂不可完整统计；不展示零盈亏、零交易结论或整份账本 JSON

#### Scenario: Verified profit before cycle closure
- **WHEN** 某未闭合管理周期已有可可靠归属并核验的已实现盈亏，相关金额口径完整
- **THEN** 默认盈亏可显示该金额及统计区间，不因完整周期数为零隐藏真实已确认金额，也不推断整笔交易已结束

#### Scenario: Mixed metrics have different scopes
- **WHEN** 权益变化按账户快照计算，而盈亏按本任务归属成交计算
- **THEN** 各指标简短标明口径和截至时间，不统一冠以 Agent 独立收益或将快照回撤宣称为逐笔盘中最大回撤

### Requirement: Detailed ledgers and diagnostics are available on demand
详细成交、费用、归属缺项、完整周期、同步覆盖和原操作回执 SHALL 在明确的详情入口按需查看，保留来源与真实观察时间。默认记忆、收益摘要和例行决策输入 SHALL NOT 自动附加上述完整资料；详情与摘要 SHALL 使用相同证据口径和时间，不重复计算盈亏。

#### Scenario: Expand performance details
- **WHEN** 用户主动打开收益或账本详情
- **THEN** 可查看已保留的成交、费用、归属、周期及同步证据；默认收益区保持三类指标，详情不自动复制到下一次记忆正文

#### Scenario: Historical snapshot is expanded
- **WHEN** 用户查看旧记忆关联的历史快照
- **THEN** 标明其历史时间，保留原证据，不把它替换为实时账户状态或用其覆盖当前持仓
