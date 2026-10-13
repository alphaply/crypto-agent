# Spec Delta

## ADDED Requirements

### Requirement: Memory consolidation prunes execution details and bounds pending sources
短期记忆整理素材中的工具回执与执行证据 SHALL 经过精简，省略内部引擎元数据（如 continuation 准入评估、blocker_order_ids、admission 判定、recovery 调试及重试计数）与未提交至交易所的瞬态参数校验报错。待转换的历史策略投影来源 SHALL 受到有界滑动窗口限制，避免历史积压全量涌入单次输入；已取消或已完成的终态订单 SHALL 停止累加恢复尝试计数。

#### Scenario: Memory review input omits internal execution engine metadata
- **WHEN** 历史摘要中的工具操作回执包含内部 continuation、admission 与 recovery 调试字段并被注入记忆整理 Prompt
- **THEN** 记忆整理素材仅保留工具名称、动作、执行状态与关键交易事实（方向、数量、成交量、价格与当前保护状态），剥离 blocker_order_ids、continuation 规则列表、previous_assessment 及 recovery 重试元数据

#### Scenario: Transient schema validation errors are excluded from memory evidence
- **WHEN** 历史周期中发生未提交至交易所的工具参数验证错误（如 TradeActionsSchema 依赖或参数校验失败）
- **THEN** 该报错仅在决策审计日志中保留，不作为未决操作证据或阻塞订单注入短期记忆整理输入

#### Scenario: Pending historical projection sources are bounded
- **WHEN** 数据库中存在大量未完成（status='pending'）的待转换历史策略摘要
- **THEN** 记忆整理与投影准备单次仅加载最近有界数量的待转换来源（如最近 5 条），超出界限的过期来源不全量注入单次 Prompt，候选操作引用集仅取当前有界来源

#### Scenario: Terminal orders stop accumulating recovery retries
- **WHEN** 操作关联的订单已确认处于终态（如已取消或完全成交且无未决清理影响）
- **THEN** 系统将其标记为已收敛，停止对其递增 attempt_count 并移出活跃 recovery 轮询集合，回执中的尝试计数不再无限制增长
