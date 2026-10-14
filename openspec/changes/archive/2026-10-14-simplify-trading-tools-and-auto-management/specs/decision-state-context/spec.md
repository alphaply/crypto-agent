# Spec Delta

## MODIFIED Requirements

### Requirement: Default trading context describes current state
REAL、STRATEGY、SPOT_DCA 的默认决策和绑定任务聊天 SHALL 使用同一当前交易状态语义，展示配置可见范围内的当前持仓、活跃挂单、保护覆盖、数量冲突、管理者及相关异常。完整历史操作回执、回执修订列表和旧准入建议 SHALL NOT 被逐轮默认注入。市场、消息及有效策略背景继续可用；本轮工具结果和用户明确请求的历史查询不属于该默认注入限制。外部来源 SHALL 不单独构成不能管理的结论；实际管理竞争、权限及目标能力分别表达。

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
- **WHEN** 当前账户存在可见人工仓位及挂单，本地周期尚未建立且该范围未被其他管理者认领
- **THEN** 如实展示数量、订单和可自动认领状态，说明首次管理请求会内部纳管，不显示空仓、要求先 adopt 或将外部来源作为全局交易禁令

#### Scenario: A visible resource belongs to another manager
- **WHEN** 查询可见某资源，但其交易范围已经由另一个有效任务认领
- **THEN** 保留资源事实并明确管理者及本调用者只读状态；其他未冲突范围不被整体禁用

### Requirement: Current trade facts use compact readable text
当前持仓和挂单 SHALL 使用确定性文本或表格渲染，保留原订单身份、方向、用途、状态、精确数量以及分别标识的限价和触发价。正常项目只展示一次，省略空值和冗余内部元数据；未知、零、fixed 和触发时 all 必须可区分。真实观察时间按数据来源集中展示，差异和陈旧性显式保留；保护覆盖、数量冲突及当前执行异常简洁单列。管理范围、当前管理者和外部来源 SHALL 清楚展示，来源与管理权分开表达。格式整理 SHALL NOT 发起模型调用、认领范围或修改执行状态。

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

## ADDED Requirements

### Requirement: Availability separates capability authority and execution evidence
Agent、MCP 和前端 SHALL 分别表达资源是否在可见范围内、管理权是否可认领或已持有、调用者权限、目标原生能力和本次动作依赖的证据状态。可自动认领 SHALL 不要求模型先调用接管工具；只读调用者 SHALL 不因可见而获得写入权限。支持动作是能力说明，实际提交前仍重新核验。

#### Scenario: The owner manages a human amended order
- **WHEN** 人工修改本管理者范围内挂单的价格和数量，最新状态已经核验
- **THEN** 展示最新参数、外部变化和支持动作，不能因原参数变化或不是 Agent 创建而将其显示为永不可修改

#### Scenario: An unrelated incomplete observation exists
- **WHEN** 订单 A 的最终成交待核验，但订单 B 的指定撤单依赖已完整核实
- **THEN** 状态指出 A 的具体限制，B 仍展示按目标执行核验的能力，不用全局 cancel/edit 禁令替代实际依赖

### Requirement: Default tool results preserve compact actionable facts
默认模型工具结果 SHALL 返回精简的逐项动作事实，保留动作索引、操作/订单身份、操作状态与订单状态、数量模式和已知成交、具体失败或依赖阻塞及按需查询指引。整批账户摘要 SHALL 最多出现一次且只含请求的当前信息；完整原始证据 SHALL 继续可按授权查询，不逐项重复嵌入。

#### Scenario: A twenty action paper batch
- **WHEN** 二十项 paper 动作都有已保存回执，调用者请求账户摘要
- **THEN** 默认结果包含二十项精简真实结果和一份末尾摘要，不出现二十份账户全量快照；未知字段和必要核验信息不被省略成成功

#### Scenario: Failed and unexecuted steps remain distinguishable
- **WHEN** 第一步撤单待核验，默认停止策略使后续开仓未执行
- **THEN** 模型结果准确区分撤单 pending 与开仓 not_executed，保留原操作查询身份，不把整批写成全部失败或全部已提交

#### Scenario: Complete evidence requested explicitly
- **WHEN** 有权限的调用者明确请求原操作完整详情
- **THEN** 返回持久化完整回执及实际证据时间，默认投影的精简不删除原始事实，也不因详情查询恢复任何交易写入
