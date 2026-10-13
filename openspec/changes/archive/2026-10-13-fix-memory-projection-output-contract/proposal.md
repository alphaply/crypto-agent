# Proposal

## Why

独立退出模式的记忆请求同时携带旧的 `{"summary": ...}` 输出要求和新的 `ProjectionReview` 输出要求；用户实际遇到模型只返回 `summary`，随后因缺少 `memory`、`projections` 校验失败。用户进一步确认决策 Prompt 的短期记忆和近期摘要均显示“尚无已发布”，因此本次修复必须覆盖从失败来源到首次成功发布的恢复过程，不能只消除报错或保留不存在的上一版。

## What Changes

- 根据当前模式选择唯一的内置输出契约：独立退出模式要求 `summary`、`memory`、`projections`，其他模式保留单 `summary` 格式；明确区分自定义整理素材与程序输出约束。
- 对完整、无工具调用响应的 JSON/结构错误增加至多一次格式纠正，纠正与传输重试共享每次整理最多三次实际模型请求的预算。来源、引用、执行权限及发布校验保持严格。
- 保留每次失败和纠正的真实调用、费用及错误分类，禁止用空字段、复制混合原文或伪造投影把失败变成成功。
- 对确认为旧输出契约缺字段故障的终止来源和记忆任务提供按修订版本仅执行一次的后台重新排队；沿用租约、退避、来源核验和成功结果复用，不等待新的交易产生，也不重放交易。
- 加入从“已有原文但无已发布策略”到成功发布短期记忆及最多三条策略摘要的完整回归，同时覆盖无短期记忆原文但存在可恢复记忆任务的情况。
- 纳入用户补充的交易批次依赖故障：模型可见字段说明明确 `depends_on` 仅适用于显式 `execution_policy=independent`、只接受列表和此前动作索引；错误指出具体字段与纠正条件，不自动切换策略或提交交易。

## Capabilities

### New Capabilities

无。

### Modified Capabilities

- `decision-state-context`: 增加记忆输出契约一致性、有界格式纠正和既有格式失败的首次发布恢复要求；沿用有效策略持续可用、来源隔离和历史执行限制不回流的既有约束。
- `trading-tool-contracts`: 模型可见批次依赖 Schema、示例和拒绝信息与既有运行时契约一致，保持默认停止、幂等身份及执行语义。

## Impact

- 主要代码：`backend/agent/memory_agent.py`、`context_projections.py`、`memory_updates.py`；按需调整 `memory_workflow.py` 中的错误/恢复元数据，并复用现有管理端状态与重试入口。
- 回归：`tests/test_memory_agent.py`、`test_context_projections.py`、`test_decision_context_availability.py`、`test_memory_update_retries.py` 及记忆工作流相关测试。
- 文档：`docs/PROMPT_DESIGN.md`、`docs/WORKBENCH.md`，说明输出格式、请求预算和升级后的恢复状态。
- 交易接口涉及 `trading_contracts.py`、`tool_interfaces.py` 的字段说明、非 null 列表契约及明确错误；回归覆盖 Agent/MCP 和执行前拒绝。
- 不新增供应商依赖，不强制所有渠道使用 JSON Schema 原生接口；不更改交易执行语义、当前持仓/挂单格式、已发布投影版本或用户保存的 Prompt。恢复标记采用向后兼容的增量存储。
- 本次提案未读取生产数据库或完整生产调用记录；“空记忆由此错误造成”的关联由用户症状与现有失败路径支持，实施验收需区分无原始来源、无成功发布及来源已失效。
