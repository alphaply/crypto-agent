# Design

## Context

在现有的现货定投（SPOT_DCA）逻辑中，`spot_budget_status` 与 `reserve_spot_batch` 包含针对 `unknown`/`reserved` 状态的严格断言。一旦此前有订单因超时或未确知结果进入 `unknown` 状态，系统会将 `pending_unknown` 设为 True 并将当期允许新增买入上限强制置 0，同时在提示词中注入禁止买入指令。此外，前端任务编辑表单中默认渲染了共管与生命周期管理面板（`PositionManagementFields` 与 `ManagementLifecyclePanel`），对普通定投用户造成了复杂的使用负担。

参见 `proposal.md` 了解需求背景。

## Goals / Non-Goals

**Goals:**
- 现货预算计算始终基于用户配置与当期实际支出，不受历史未知状态强制归零。
- 移除系统提示词中针对未决订单的禁止买入指令，恢复模型自主买入决策能力。
- 现货下单异常时及时释放预算预留并返回失败，防止订单长期死锁在 `unknown` 状态；清理/释放无订单 ID 的历史挂起记录。
- 前端任务编辑页面移除/隐藏冗余的共管与生命周期管理面板。

**Non-Goals:**
- 不修改合约交易（REAL perpetual / swap）与模拟盘（PAPER）的现有管理与执行机制。
- 不修改数据库底层表结构与版本迁移。
- 不自动根据余额不足降级订单金额（用户明确表示会充值足额 USDT）。

## Decisions

### 1. 预算计算解绑 unknown 阻断
- **方案**：在 `backend/utils/spot_execution.py` 的 `spot_budget_status` 中，不再因 `row['status'] in {'reserved', 'unknown'}` 将 `available` 强制赋为 0，也不将 `pending_unknown` 标为 True。`available` 取 `remaining`（与未超限的 `lifetime_remaining`）的真实差值。
- **替代方案考虑**：保留 `pending_unknown` 但只给告警不阻断。考虑用户明确要求“不要搞复杂、不要阻止”，彻底去除归零阻断是最清晰的做法。

### 2. 下单前置校验与异常释放
- **方案**：在 `reserve_spot_batch` 中移除 `any(row['status'] in {'reserved', 'unknown'})` 的拦截抛错。在 `agent_tools.py` 的现货下单异常处理中，捕获异常后统一调用 `finish_spot_reservation(..., 'released')`，返回 `failed` 状态，不残留 `unknown`。
- **替代方案考虑**：后台持续重试核验。由于币安可能因余额不足直接拒绝或无历史记录，后台死循环重试会导致额度持续卡死，直接释放更符合用户期望。

### 3. 系统提示词移除强行买入禁令
- **方案**：在 `backend/agent/agent_graph.py` 中删除 `if budget_status['pending_unknown']: system_prompt += ...`，避免向模型注入禁止买入的强指令。

### 4. 前端表单精简
- **方案**：在 `frontend/src/pages/AdminPage.jsx` 中，隐藏任务编辑表单中的 `<PositionManagementFields>` 和 `<ManagementLifecyclePanel>`。
- **替代方案考虑**：仅对小白用户隐藏。鉴于目前该面板主要是针对高级多 Agent 共管场景，直接从标准任务编辑弹窗中移除该区域，界面更加聚焦简洁。

## Risks / Trade-offs

- **[重复下单风险]** → 若上一笔订单因网络超时正在币安撮合而未能在本地获知 ID，随后模型可能发起新订单。通过币安客户端订单号去重（`spot_client_order_id`）与同一价位挂单去重（`_is_duplicate_real_order`）作为保护。
- **[余额不足风险]** → 若用户未及时充值，调用下单接口会收到“可用计价币余额不足”的明确失败信息，属于正常交易响应，不会导致任务崩溃。
