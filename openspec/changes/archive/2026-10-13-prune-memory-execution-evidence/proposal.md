# Proposal

## Why

在独立退出模式的短期记忆整理（Memory Review）过程中，由于历史摘要中的工具回执全量未裁剪注入、待转换策略投影来源堆积（历史未完成来源无界累积）以及已取消/终态订单在后台被无休止自旋查询，导致短期记忆整理的 Prompt 中充斥着大量瞬态参数校验报错（如 Schema validation error）、已失效的旧止损止盈单，以及包含数千字符底层调试元数据（`continuation`、`admission`、`recovery` 及累加至数百次的重试计数）的臃肿回执。

这不仅严重浪费模型上下文 Token，还使模型被大量无关的历史技术性阻塞事实干扰，影响纯策略背景与关键价位假设的提取。必须对记忆整理阶段的执行回执与候选证据建立裁剪与收敛规范，剔除调试噪音，限制待转换来源回溯边界，并停止对终态订单的无效恢复轮询。

## What Changes

- **精简摘要附带的执行回执与证据结构**：
  - 过滤工具参数校验失败等未发往交易所的瞬态报错（如 `TradeActionsSchema` 验证失败），不作为未决操作证据注入记忆整理素材。
  - 裁剪注入短期记忆的工具回执与 `current_evidence`，剔除底层执行引擎内部状态（`continuation`、`admission`、`previous_assessment`、`evidence_versions`、`recovery` 等内部调试元数据），仅保留策略决策所需的关键事实（工具名称、动作、最终状态、方向、数量、成交量、价格与当前有效保护状态）。
- **有界收敛待转换历史策略来源与候选证据集**：
  - 在 `context_projections.py` 的 `ensure_sources` 中，对待转换（`status='pending'`）的历史来源设立有界处理窗口与数量上限，防止历史堆积的数十条旧摘要一次性全量涌入 review 输入。
  - 优化候选执行引用（`candidates`）与当前证据（`current_execution_evidence`）的收集，对于已取消（`canceled`）或已终态且无残留影响的旧订单，提供轻量摘要或退出活跃证据集，避免将全量内部回执反复序列化。
- **终态订单停止自旋恢复与重试累加**：
  - 在 `trade_recovery.py` 中，对明确处于终态（`canceled` 或已完成且确认无 pending fill/cleanup）的订单，停止递增 `attempt_count` 并退出活跃 recovery 候选集，避免重试计数累积至数百次。

## Capabilities

### New Capabilities

无。

### Modified Capabilities

- `decision-state-context`: 扩展短期记忆整理素材规范，要求注入记忆整理的执行回执与候选证据必须经过结构精简，剥离底层引擎调试状态与瞬态调用异常；待转换历史来源必须有界，不得无界累积历史证据；终态订单证据停止重复查询与无界重试。

## Impact

- **受影响代码**：
  - `backend/utils/decision_record.py`：`memory_execution_evidence` 与 `append_execution_receipts` 的精简逻辑。
  - `backend/agent/context_projections.py`：`ensure_sources` 来源窗口有界收敛，`review_input` 与 `candidates` 证据精简。
  - `backend/utils/trade_recovery.py`：终态订单判定与恢复尝试计数收敛。
  - `backend/agent/memory_updates.py`：记忆更新上下文构建。
- **测试与回归**：
  - `tests/test_context_projections.py`、`tests/test_memory_agent.py`、`tests/test_decision_record.py`、`tests/test_decision_state_context.py`。
- **兼容性**：
  - 不改变实际交易执行与交易所接口契约，不改变已发布的策略投影对外格式，不影响原始交易审计记录的完整性。
