# Proposal

## Why

用户在现货定投配置了每轮 30 USDT 预算，但因为历史挂单遇到异常未决状态，导致系统触发了死锁拦截：可用预算被强制计算为 0 USDT，系统提示词强行注入“存在尚未核实结果的下单，当前禁止新增买入”，模型被阻止正常交易；同时前端展示了复杂的「仓位与挂单管理」面板，用户在释放时再次被未决状态阻断。用户明确要求彻底去除此复杂拦截机制与预算锁死逻辑，让模型正常提交买单，并在前端隐藏该管理面板，完成后发布 Docker 镜像。

## What Changes

- **移除现货预算归零与未决拦截**：在 `backend/utils/spot_execution.py` 中移除 `pending_unknown` 造成的可用预算归零限制，预算始终按用户配置的每轮预算与已花费额度正常计算；移除下单前的未核验阻断抛错；自动释放无订单 ID 的历史遗留挂起记录。
- **移除模型提示词禁令**：在 `backend/agent/agent_graph.py` 中移除注入给模型的“存在尚未核实结果的下单，当前禁止新增买入”系统指令，不再束缚模型决策。
- **下单异常不挂起死锁**：在 `backend/agent/agent_tools.py` 中，下单遇到网络或交易所异常时直接返回失败并释放额度，不进入 `unknown` 挂起状态。
- **前端任务编辑面板精简**：在 `frontend/src/pages/AdminPage.jsx` 中隐藏任务编辑模态框里的「仓位与挂单管理」（`PositionManagementFields` 及 `ManagementLifecyclePanel`），界面保持清晰直观。
- **发布流程**：完成测试与构建验证后，按既有工作流归档、提交到 `beta` 并通过发布脚本发布 Docker 镜像。

## Capabilities

### New Capabilities

无。本次为逻辑精简与界面体验优化，`.openspec.yaml` 设置 `skip_specs: true`。

### Modified Capabilities

无。底层核心规范契约保持不变。

## Impact

- 后端：`backend/utils/spot_execution.py`、`backend/agent/agent_graph.py`、`backend/agent/agent_tools.py`。
- 前端：`frontend/src/pages/AdminPage.jsx`。
- 用户影响：现货定投预算不再意外归零，历史卡单不再阻止模型正常下单，任务编辑弹窗移除冗余复杂的管理面板。
