# Tasks

## 1. 回执精简与瞬态错误过滤

- [x] 1.1 在 `backend/utils/decision_record.py` 中实现 `compact_execution_receipt` 与精简版 `memory_execution_evidence`，剔除 `continuation`、`admission`、`previous_assessment`、`evidence_versions`、`recovery` 等底层调试引擎字段，通过单元测试验证精简后的结构仅包含核心交易与状态字段。
- [x] 1.2 在 `backend/utils/decision_record.py` 中增加未发往交易所的瞬态参数校验报错（如包含 `validation error for TradeActionsSchema`）过滤，不将其作为未决订单注入短期记忆输入，通过测试验证过滤效果。
- [x] 1.3 编写或更新 `tests/test_decision_record.py`，覆盖正常成交单、已取消挂单与瞬态参数报错回执的精简投影，执行 `uv run pytest tests/test_decision_record.py` 验证通过。

## 2. 待转换来源有界收敛与证据集优化

- [x] 2.1 在 `backend/agent/context_projections.py` 的 `ensure_sources` 中增加待转换历史来源数量上限（`MAX_PENDING_SOURCES = 5`）与时间窗口限制，防止历史堆积的数十条旧摘要一次性全量涌入 review 输入。
- [x] 2.2 优化 `backend/agent/context_projections.py` 中的 `prepare_review` 与 `candidates` 收集逻辑，对已取消或已终态的旧订单回执避免深层展开冗余元数据。
- [x] 2.3 编写或更新 `tests/test_context_projections.py`，模拟多条 pending 来源积压与已取消订单场景，执行 `uv run pytest tests/test_context_projections.py` 验证拉取来源数量有界且 `review_input` 长度健康。

## 3. 终态订单重试收敛与回归验证

- [x] 3.1 在 `backend/utils/independent_exits.py` 与 `backend/utils/trade_receipts.py` 中优化订单状态识别，对明确处于 `canceled` 或已完成且无未决清理的订单停止递增 `attempt_count`，设置 `next_retry_at = None` 并退出活跃 recovery 候选集。
- [x] 3.2 运行短期记忆与决策上下文端到端回归测试 `uv run pytest tests/test_memory_agent.py tests/test_decision_state_context.py`，验证短期记忆整理流程正常运行且输入不包含多余阻塞订单与调试字段。
- [x] 3.3 更新 `docs/PROMPT_DESIGN.md`，说明短期记忆整理中执行证据精简与待转换来源有界收敛规则。

## Workflow follow-up

- 用户确认后执行应用工作流（apply）并验证测试。
- 同步主规范并执行规范归档与发布流程。
