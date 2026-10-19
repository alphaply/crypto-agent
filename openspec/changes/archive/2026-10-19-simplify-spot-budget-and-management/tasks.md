# Tasks

## 1. 现货预算与未决拦截移除

- [x] 1.1 在 `backend/utils/spot_execution.py` 中更新 `spot_budget_status` 与 `reserve_spot_batch`，移除基于 `reserved`/`unknown` 强制将预算归零和阻断下单的逻辑，并在 `reconcile_spot_reservations` 中自动释放无 order_id 的历史未知记录；通过单元测试验证。
- [x] 1.2 在 `backend/agent/agent_graph.py` 中删除 `pending_unknown` 注入的“存在尚未核实结果的下单，当前禁止新增买入”系统提示词；通过 prompt 生成测试验证。
- [x] 1.3 在 `backend/agent/agent_tools.py` 中优化现货下单异常捕获，发生网络或交易所异常时统一释放保留额度并返回失败状态，不再保持 `unknown` 锁死；通过下单异常用例验证。

## 2. 前端管理面板精简

- [x] 2.1 在 `frontend/src/pages/AdminPage.jsx` 中隐藏任务编辑模态框里的「仓位与挂单管理」（`PositionManagementFields` 和 `ManagementLifecyclePanel`），保留纯粹简洁的任务基础配置；通过前端编译构建 `npm run build --prefix frontend` 验证无语法及引用错误。

## 3. 测试与发版归档

- [x] 3.1 运行测试套件与前端构建验证改动无误。
- [x] 3.2 归档变更至 `openspec/changes/archive/2026-10-19-simplify-spot-budget-and-management`，提交至 `beta` 分支，并执行发布脚本构建与推送 Docker 镜像。
