## MODIFIED Requirements

### Requirement: Default trading context describes current state
REAL、STRATEGY、SPOT_DCA 的默认决策和绑定任务聊天 SHALL 使用同一当前交易状态语义，展示配置可见范围内的当前持仓、活跃挂单、保护覆盖、数量冲突、管理状态及相关异常。完整历史操作回执、回执修订列表和旧准入建议 SHALL NOT 被逐轮默认注入。市场、消息及有效策略背景继续可用；本轮工具结果和用户明确请求的历史查询不属于该默认注入限制。外部来源 SHALL 不单独构成不能管理的结论；共享管理、原提交来源、权限、当前目标能力及实际并发/证据限制分别表达，不把其他连接归属当作只读依据。

#### Scenario: Completed history does not grow the default prompt
- **WHEN** 当前账户和相关异常完全相同，仅新增或刷新 100 条已完成且无残留影响的历史操作
- **THEN** 默认交易状态文本不变，不出现这些操作的回执 JSON、恢复计数或历史限制

#### Scenario: An ordinary independent position
- **WHEN** 当前有 LONG 0.15、一笔未成交入场和三笔已核验等待触发的退出单
- **THEN** 默认上下文逐单展示当前有效身份、类型、方向、价格及数量语义，正常订单只展示一次，历史开仓和平仓回执不再额外展开

#### Scenario: Bound chat uses the same state
- **WHEN** 定时决策和绑定任务聊天使用相同账户证据
- **THEN** 两者呈现相同持仓、挂单、管理状态和异常事实，聊天不会再追加一份历史恢复记录块，既有审批保持有效

#### Scenario: An external position has no local cycle
- **WHEN** 当前账户存在可见人工仓位及挂单，本地周期尚未建立且本调用者具有对应交易授权
- **THEN** 如实展示数量、订单和可共享管理状态，说明管理请求会内部核验纳管，不显示空仓、要求先 adopt 或将外部来源作为全局交易禁令

#### Scenario: A visible resource belongs to another manager
- **WHEN** 查询可见某资源，但其交易范围已经由另一个有效任务认领
- **THEN** 保留资源及原提交来源事实，按当前调用者实际权限表达可执行动作，不因其他 owner 标为只读或提示先释放/转移

### Requirement: Current trade facts use compact readable text
当前持仓和挂单 SHALL 使用确定性文本或表格渲染，保留原订单身份、方向、用途、状态、精确数量以及分别标识的限价和触发价。正常项目只展示一次，省略空值和冗余内部元数据；未知、零、fixed 和触发时 all 必须可区分。真实观察时间按数据来源集中展示，差异和陈旧性显式保留；保护覆盖、数量冲突及当前执行异常简洁单列。管理范围、管理状态和外部来源 SHALL 清楚展示，来源与管理权分开表达。方向 SHALL 使用“多头/空头”，不将 SHORT 写成“空仓”；精简文本 SHALL 省略管理者内部 ID，结构化身份保持可用。格式整理 SHALL NOT 发起模型调用、认领范围或修改执行状态。

#### Scenario: Normal position and three orders
- **WHEN** 持仓为多仓 0.07、均价 2415、浮盈 7.93163651，挂有止盈市价 0.07 @2545、止损市价 0.07 @2357 和限价入场 0.1 @2385
- **THEN** 持仓用一行可读文字、订单用逐单表格展示，完整订单 ID 原样保留，不展开 client_id、重复 snapshot 字段、null 或浮点时间戳；浮盈可明确约数展示，交易数量和价格不作破坏精度的截断

#### Scenario: Trigger limit, dynamic all and child identity
- **WHEN** 条件限价单包含触发价和成交限价，all 单数量在触发时确定，或条件单已有活动子单
- **THEN** 两种价格分别保留，all 表述为触发时该方向全部仓位，子单及必要父单身份保持准确，不将未知数量写成零

#### Scenario: Missing observations and ownership conflicts
- **WHEN** 仓位、挂单、管理权或来源观察发生缺失、数量冲突或读取失败
- **THEN** 精简后的文本仍保留具体对象、最后已知信息、缺失事实和影响范围，不表述为已确认空仓、无挂单或无异常；已知外部来源本身不被描述为归属冲突

#### Scenario: Scheduled agent and bound chat share rendering
- **WHEN** 定时决策和绑定任务聊天读取相同状态
- **THEN** 二者得到相同精简事实、管理状态与已发布策略内容；增加无关内部元数据或已完成历史记录不会增长默认 Prompt

#### Scenario: Short position is not an empty account
- **WHEN** 当前存在 SHORT 0.04、均价 2332.5、浮盈 1.05 USDT，状态为 shared 且带有内部 owner
- **THEN** 精简持仓文本显示“空头 0.04｜均价 2332.5｜浮盈约 +1.05 USDT｜授权共管”，不显示“空仓”或管理者 ID；原结构化身份保持不变

### Requirement: Current context describes management scope and external changes
内置 Agent、绑定任务聊天、MCP 与前端 SHALL 以一致事实展示独占范围、管理状态、当前仓位、已纳管外部订单、变化来源、真实观察时间和对账完整性。外部来源不确定时 SHALL 标识为外部/未跟踪活动，不能推断一定由人类操作。默认上下文 SHALL 使用当前相关事实及变化摘要，不逐轮注入全部外部成交历史。

#### Scenario: External reduction is reconciled
- **WHEN** 独占范围内发生可核验外部减仓
- **THEN** 当前数量、退出覆盖和数量冲突同时更新，默认状态说明外部变化，不再仅因其来源提示归属失败

#### Scenario: External order is enrolled
- **WHEN** 外部订单纳管完成且可在既有权限下管理
- **THEN** 展示真实 order_id、入场/退出作用、原生参数、来源及可用动作，不能仍仅标为 external_or_untracked/modifiable=false

#### Scenario: Only one source is incomplete
- **WHEN** 普通订单可读而条件订单或成交证据不完整
- **THEN** 标明具体缺失来源及其观察时间，不显示“已确认没有异常”或空仓，也不把所有动作统一标为禁止
