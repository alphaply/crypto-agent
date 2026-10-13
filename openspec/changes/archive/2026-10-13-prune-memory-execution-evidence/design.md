# Design

## Context

在短期记忆整理链路中（`memory_updates.py` -> `memory_agent.py`），输入 Prompt 由两大部分注入执行信息：
1. `append_execution_receipts`（位于 `backend/utils/decision_record.py`）：为过去 4 小时滚动窗口内的每条摘要追加该轮的 `execution_results` 及其 `current_evidence`。
2. `review_input`（位于 `backend/agent/context_projections.py`）：为独立退出模式注入待转换来源列表 `sources`、候选操作引用 `candidates` 以及全量操作证据 `current_execution_evidence`。

目前这些数据均直接调用底层的 `operation_evidence`，而 `operation_evidence` 返回的是供底层撮合引擎核验使用的完整内部对象（包含 `admission` 准入规则、`continuation` 阻塞链条、`recovery` 重试状态以及完整的嵌套版本号）。此外，`ensure_sources` 会无上限加载所有历史 `status='pending'` 记录，导致数十条摘要及其历史订单全部汇入单个 Prompt。

## Goals / Non-Goals

**Goals:**
- 提供专门针对短期记忆整理消费的轻量级回执投影视图（`compact_execution_receipt` / `compact_execution_evidence`）。
- 剥离注入 Prompt 的底层调试引擎元数据（`continuation`、`admission`、`previous_assessment`、`evidence_versions`、`recovery`），只保留核心策略与订单事实。
- 过滤或简写未进入交易所执行的瞬态参数校验报错（如 `TradeActionsSchema` 语法错误）。
- 为 `ensure_sources` 增加待转换历史来源数量上限（例如最多 5 条）与时间窗口约束，防止未完成来源无限堆积。
- 收敛终态订单的恢复尝试逻辑，对已确认 `canceled` / `filled` 的订单停止递增 `attempt_count`。

**Non-Goals:**
- 不改变数据库中原始 `decision_json`、`report_json` 或 `trade_action_runs` 的持久化格式（审计数据必须保持完整）。
- 不改变实际交易下单、撤单、保护核验的执行语义。
- 不改变 LLM 记忆输出的契约定义（`summary`、`memory`、`projections`）。

## Decisions

### 1. 记忆回执专用的精简视图函数

在 `backend/utils/decision_record.py` 中引入针对记忆整理的裁剪逻辑：
- **裁剪规则**：
  - 保留字段：`tool`、`action`、`status`、`side`、`amount`、`filled_amount`、`price`、`trigger_price`、`order_status`、`order_id`、`exit_type`。
  - 剔除字段：`continuation`、`admission`、`previous_assessment`、`evidence_versions`、`recovery`、`client_id`、`episode_id`、内部浮点微秒时间戳、`projected_at`。
- **瞬态错误过滤**：
  - 当工具调用因本地参数验证失败（例如包含 `validation error for TradeActionsSchema`）时，不将其当作未决订单，仅输出简要的 `{"tool": ..., "status": "failed", "error": "validation_error"}`，不关联空未决 `current_evidence`。

*替代方案考虑*：
- 方案 A：直接修改 `operation_evidence` 删减字段。**否决**，因为底层真实交易准入检查（`independent_admission.py`）需要完整的 continuation 与 recovery 结构来进行阻塞判定。
- 方案 B：仅在输出给 memory 整理前做只读投影裁剪（本方案）。**采纳**，职责分离，不破坏核心引擎。

### 2. `ensure_sources` 的有界滑动窗口与分批加载

在 `backend/agent/context_projections.py` 中，约束 `ensure_sources` 加载的 pending 来源集合：
- 设置 `MAX_PENDING_SOURCES = 5`，且仅拉取距离当前时间窗口最近的未完成摘要。
- 对于超过一定时效（例如超过 24 小时或已有更新短期记忆覆盖）的历史 pending 来源，提供惰性收敛或跳过机制，防止单次 review 拖入十几个跨周期的候选操作。

*替代方案考虑*：
- 一次性尝试转换全部 pending 来源。**否决**，当积累了 19 个来源时，Prompt 长度直接超出模型有效窗口且大幅增加幻觉概率。

### 3. 终态订单证据轻量化与重试收敛

在 `backend/utils/trade_recovery.py` 中：
- 识别 `order_status in ('canceled', 'filled')` 且无未决 fill/cleanup 的订单，不再递增 `attempt_count`，设置 `next_retry_at = None`。
- 在 `context_projections.py` 构建 `current_execution_evidence` 时，对于已处于终态的历史订单，仅保留其终态摘要，不嵌套冗余的完整恢复状态机。

## Risks / Trade-offs

- **[Risk]** 裁剪后模型是否会遗漏当前仍活跃的止损/止盈保护信息？
  - **Mitigation**: 裁剪保留了关键的 `order_id`、`order_type`、`trigger_price` 与 `order_status: "waiting_trigger"`，当前持仓与活跃保护信息依然 100% 完整传达给模型，仅去除了与策略无关的底层规则树和重试计数。
- **[Risk]** 限制单次 pending 来源数量后，历史堆积的来源是否会被遗漏？
  - **Mitigation**: 后台定时任务（`tick_memory_updates`）会在随后的轮次中按 FIFO 继续消化剩余的待转换来源，单次处理 3~5 条可在几轮内平滑消化，同时保证每轮 Prompt 长度健康。
