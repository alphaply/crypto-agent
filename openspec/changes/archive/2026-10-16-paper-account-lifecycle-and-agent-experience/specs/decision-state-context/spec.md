# Spec Delta

## MODIFIED Requirements

### Requirement: Default trading context describes current state
REAL、STRATEGY、SPOT_DCA 的默认决策和绑定任务聊天 SHALL 使用同一当前交易状态语义，展示配置可见范围内的当前持仓、活跃挂单、保护覆盖、数量冲突、管理者及相关异常。完整历史操作回执、回执修订列表和旧准入建议 SHALL NOT 被逐轮默认注入。市场、消息及有效策略背景继续可用；本轮工具结果和用户明确请求的历史查询不属于该默认注入限制。外部来源 SHALL 不单独构成不能管理的结论；共享管理、原提交来源、权限、当前目标能力及实际并发/证据限制分别表达，不把其他连接归属当作只读依据。

#### Scenario: Completed history does not grow the default prompt
- **WHEN** 当前账户和相关异常完全相同，仅新增或刷新 100 条已完成且无残留影响的历史操作
- **THEN** 默认交易状态文本不变，不出现这些操作的回执 JSON、恢复计数或历史限制

#### Scenario: An ordinary independent position
- **WHEN** 当前有 LONG 0.15、一笔未成交入场和三笔已核验等待触发的退出单
- **THEN** 默认上下文逐单展示当前有效身份、类型、方向、价格及数量语义，正常订单只展示一次，历史开仓和平仓回执不再额外展开

#### Scenario: Bound chat uses the same state
- **WHEN** 定时决策和绑定任务聊天使用相同账户证据
- **THEN** 两者呈现相同持仓、挂单、管理者和异常事实，聊天不会再追加一份历史恢复记录块，既有审批保持有效

#### Scenario: An external position has no local cycle
- **WHEN** 当前账户存在可见人工仓位及挂单，本地周期尚未建立且本调用者具有对应交易授权
- **THEN** 如实展示数量、订单和可共享管理状态，说明管理请求会内部核验纳管，不显示空仓、要求先 adopt 或将外部来源作为全局交易禁令

#### Scenario: A visible resource belongs to another manager
- **WHEN** 查询可见某资源，但其交易范围已经由另一个有效任务认领
- **THEN** 保留资源及原提交来源事实，按当前调用者实际权限表达可执行动作，不因其他 owner 标为只读或提示先释放/转移
