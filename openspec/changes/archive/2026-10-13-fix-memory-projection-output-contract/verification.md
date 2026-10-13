# Verification

日期：2026-10-13。变更：`fix-memory-projection-output-contract`。

## 范围与环境

本次实现同时覆盖记忆投影输出/首次发布恢复，以及用户补充的 `depends_on` 契约说明和校验。测试使用临时 SQLite 数据库、离线模型响应和模拟交易入口；没有调用真实模型、交易所或生产恢复接口。未修改运行中任务的数据或自定义模板。

工作区另有 `add-provider-model-discovery` 修改，本次不将其纳入本变更的实现或验证结论。

## 验证结果

最终相关回归：**593 passed in 95.21s**。

```powershell
$relatedTests = @(rg --files tests -g 'test_*.py' | Where-Object { $_ -match 'test_(memory_|context_projections|decision_context_availability|decision_memory_context|decision_state_context|retired_memory_context|turn_memory_and_review|prompt_context_compaction|trade_batch|trade_receipts|trade_evidence_concurrency|independent_|scoped_trade_rounds|batch_dependency_guidance|mcp_tool_contracts|mcp_trade_recovery)' })
& '.venv/Scripts/python.exe' -m pytest @relatedTests -q
```

`openspec validate fix-memory-projection-output-contract --strict` 通过；`git diff --check` 通过。

| 验收项 | 覆盖与结论 |
| --- | --- |
| 唯一输出契约 | 独立模式使用完整 `summary/memory/projections`；非独立模式保持单 summary 兼容。自定义文件、字面 JSON 花括号、system/user 角色、空待转换来源均通过。 |
| 有界纠正与费用 | 单 summary、JSON 错误、嵌套形状错误可纠正一次；正常响应一次调用。网络与格式错误交替仍最多三次实际调用；失败和成功分别审计，各次返回用量只记一次。 |
| 严格拒绝 | 截断、拒绝、工具调用、伪造来源/hash/执行引用、执行权限和来源覆盖错误不会获得格式纠正许可或伪成功发布。 |
| 一次恢复 | 两队列增量列迁移可回滚且重复执行幂等；仅旧修订的明确缺字段失败重排，保留原错误和尝试数。新修订失败、未知范围、无关故障、partial/completed 及失效来源排除。 |
| 首次发布 | 有原文但未发布，以及无原文但存在有效失败生成任务，两条链路均经后台 tick、模型纠正、持久化到共享读路径。无需新交易，原始混合文本保留审计，两个策略区块恢复内容。 |
| 持续可用与并发 | 已发布内容继续可读；旧来源晚完成不顶替较新摘要；租约阻止重复恢复。来源被编辑/删除、账户或标的变化时，事务内校验拒绝新记忆写入。 |
| 有界工作重试 | 恢复后耗尽三次尝试保持 failed，扫描/初始化不再重置。恢复后来源被删也进入失败流程，避免永久占据队首；成功生成结果在存储重试时复用。 |
| 真实缺失 | 没有原文和生成任务时不伪造记忆；只有摘要可独立发布摘要；两类来源均无时两个区块继续如实显示尚无已发布内容，不调用模型。 |
| 批次依赖 | Agent/MCP 依赖 schema 和策略描述一致；null 不再列为合法值。错误包含动作字段和 explicit independent 要求，非法请求不进入交易分发；completed 才满足依赖，重放不再提交，省略字段的序列化保持不变。 |

新增回归集中在 `tests/test_memory_projection_contract.py`、`tests/test_memory_contract_recovery.py`、`tests/test_batch_dependency_guidance.py`；已有记忆工作流、投影可用性、共享上下文、批次执行和 MCP 回归共同验证兼容性。

## 脱敏 Prompt 前后对照

以下来自测试夹具的“已有记忆原文与三条摘要、旧缺字段失败”场景，不是生产账户数据或投资建议。只展示两个相关区块。

恢复前：

```text
## Short-term memory
（尚无已发布短期策略记忆）

## 近期已发布策略摘要（最多三条；历史计划，不是成交证明）
（尚无已发布策略摘要）
```

后台一次初始响应仍只有 `summary`，纠正响应通过完整校验并发布后：

```text
## Short-term memory
[截至 2026-10-10 04:00:00] 等四小时收盘确认前不加仓；支撑95，突破110再评估。

## 近期已发布策略摘要（最多三条；历史计划，不是成交证明）
[截至 2026-10-01 01:00:00] 等四小时收盘确认前不加仓；支撑95，突破110再评估。

[截至 2026-10-01 02:00:00] 等四小时收盘确认前不加仓；支撑95，突破110再评估。

[截至 2026-10-01 03:00:00] 等四小时收盘确认前不加仓；支撑95，突破110再评估。
```

混合原文中的“旧 unknown 待核验，禁止加仓”保留在原文审计里，不进入策略区块；有效的四小时观察条件保留。第二次后台 tick 不重复模型调用。

## 发布后的生产核对

离线验收完成不等于生产已恢复。本轮为 apply；主规范同步、归档及 Git/Docker 发布按后续 archive 流程执行，生产容器更新按部署请求执行。

部署后检查受影响任务：

1. 后台 tick 是否将符合条件的旧 failed 项定向排队；检查契约修订及恢复诊断，不能通过清空标记反复恢复。
2. 管理状态 `published` 是否出现真实 `source_id/as_of`，并对照原文、`pending/failed/not_generated/errors/read_errors`。`not_generated` 指有效原文尚无投影，不能解释为没有原文。
3. 下一轮决策及绑定任务聊天是否读取到已发布策略正文和真实截至时间。仅无错误、进程重启成功或队列变 pending 不算恢复成功。
4. 若仍失败，核对实际模型调用的契约、纠正阶段和终态错误；保持已发布内容。现有投影重试只作用于已有有效 failed 投影，不能补造原文或重排生成任务。
