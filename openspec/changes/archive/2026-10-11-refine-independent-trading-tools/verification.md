# Implementation verification

验证环境：Windows，本地隔离 SQLite、fake exchange 与已安装 CCXT 请求拦截；未向真实账户下单、撤单或更改生产配置。

## 已完成的证据

- `uv run python tests/demo_independent_v2.py --output openspec/changes/archive/2026-10-11-refine-independent-trading-tools/demo-mock-receipts.json`：跨轮创建 LONG 0.4 + 0.6 与 SHORT 0.5，第二轮 TP/SL fixed 0.2、fixed 0.3、all；随后 edit/cancel；LONG 实际退出 0.2、0.3、0.5，SHORT 保留 0.5。JSON 保存每次操作及快照。
- 演示与迁移测试确认：legacy amount=0 首次解析为 fixed 1；重启不会扩大这个固定档；新 all 计划量在重启后仍为 null，实际成交另记；固定档冲突单独提示且不伪装成 pending。保留旧 ID、索引、成交与回执，无交易所写入。旧版本不能接管活跃 v2 的回滚限制已写入 WORKBENCH。
- `tests/test_independent_binance_v2.py` 以真实 CCXT 构造请求并拦截网络：六种类型的创建、查询、取消通道，LONG/SHORT、Hedge/One-way、all 不带 quantity/reduceOnly、默认 CONTRACT_PRICE、原生 LIMIT PUT 修改。
- `tests/test_independent_docs_v2.py`：MCP 文档的五份 v2 示例均由生产 schema 解析；包括分批建仓、多空、TP/SL fixed+all、edit 与 cancel。
- `tests/test_independent_prompt_v2.py`：REAL/STRATEGY 默认与随包模板支持跨轮退出；附带规则保留，自定义文件内容不覆盖。
- `tests/test_execution_ledger.py`：重启建立 algo:42 父身份和 regular child 成交关系，不抢占另一任务数字 42 普通订单；旧 take_profit_limit 仍按普通退出记账。
- `node --test frontend/tests/exitManagement.test.js`：普通/条件 LIMIT 标签、all/null 与覆盖估计通过。
- `npm run build --prefix frontend`：通过；Vite 提示现有大于 500 kB 的 chunk。
- `uv run --with playwright python tests/browser_admin_exit_modes.py`：桌面 1440 和手机 390 通过，完整后台旧模式/新增默认/切换保存回归及 v2 卡片字段、状态、数量冲突显示通过。截图另存为本变更的 [desktop.png](screenshots/desktop.png) 与 [mobile.png](screenshots/mobile.png)，已人工查看，手机表格可横向滚动且页面无横向溢出。

## 边界

条件 LIMIT 触发不是成交保证；模拟 K 线不能还原实盘逐笔顺序。原生 all 的范围是交易所账户该品种该方向，无法隔离之后的外部同向加仓。未知操作保持原身份核验，不能通过换 operation_id 重发。附带模式、现货和 adopt 不扩展业务语义。

## 规格核对

以下按三份规格的 requirement 顺序核对其下全部场景。文件均位于 `tests/`；同名 v2 mock/real 测试并行验证同一业务语义。

| 规格要求 / 场景 | 验收证据 |
|---|---|
| Canonical independent trading actions：多次入场、上下文不能覆盖 | `test_independent_tool_contracts.py`、`test_trade_tool_integration.py` |
| Explicit close order types and price fields：普通/条件限价、缺失/冲突字段 | `test_independent_tool_contracts.py`、`test_independent_binance_v2.py` |
| Explicit quantity selection：fixed/all 互斥、unsupported all | `test_independent_tool_contracts.py`、`test_independent_real_v2.py` |
| Edit means an explicit change：部分入场、条件撤建、all 改价、阶段字段 | `test_independent_real_v2.py`、`test_independent_mock_v2.py` |
| Edit authorization follows the owned target：入场不能伪装退出、trade 可必要撤换 | `test_mcp_gateway.py`、`test_mcp_tool_contracts.py` |
| Cancel uses owned order identity：条件取消、触发竞态及子单 | `test_independent_real_v2.py`、`test_independent_mock_v2.py` |
| Ordered batch uses the same action contracts：未成交入场、混合权限、无版本纯撤单 | `test_independent_tool_contracts.py`、`test_mcp_tool_contracts.py`、`test_trade_tool_integration.py` |
| Legacy contracts retain original meanings：旧 LIMIT、新 TP LIMIT、重启解释 | `test_independent_exit_migration.py`、`test_independent_mock_v2.py`、`test_independent_real_v2.py` |
| Stable operations and factual receipts：升级后重放、未知结果跨别名 | `test_mcp_tool_contracts.py`、`test_trade_batch.py`、`test_independent_receipt_recovery.py` |
| Descriptions match mode：示例/schema 一致 | `test_independent_docs_v2.py`、`test_tool_registry.py` |
| Exit timing is an agent decision：下一轮退出、同轮已成交可退出 | `test_independent_prompt_v2.py`、`test_trade_tool_integration.py`、模拟演示 JSON |
| Scale-in orders manage aggregate directional positions：多次入场、Hedge 只退一向 | `test_independent_mock_v2.py`、`test_independent_real_v2.py`、模拟演示 JSON |
| Fixed stages and dynamic all coexist：0.2/0.3/all、追加仓位、分档 SL | `test_independent_mock_v2.py`、`test_independent_real_v2.py` |
| All is bounded by execution capability：市价快照与迟到成交、整方向归属 | `test_independent_real_v2.py`、`test_position_adoption.py` |
| Existing fixed quantities are not automatically reallocated：超额保留、交易所拒绝 | `test_independent_real_v2.py`、`test_independent_mock_v2.py` |
| Fixed exit admission：重叠 0.7+0.6、单笔超量 | `test_independent_real_v2.py`、`test_independent_mock_v2.py` |
| Execution evidence determines fills and sequence：跳空、all 先触发、固定超额实际成交 | `test_independent_mock_v2.py` |
| Reconciliation distinguishes parents from executions：无计划量、触发后缺少成交证据 | `test_independent_real_v2.py`、`test_independent_receipt_recovery.py` |
| Confirmed cycle closure：短暂空读、同周期清理 | `test_independent_exits.py`、`test_independent_real_v2.py`、模拟演示 JSON |
| Visible state is consistent：动态 all/覆盖/冲突 | `test_independent_exit_context.py`、`test_mcp_tool_contracts.py`、`browser_admin_exit_modes.py`、桌面/手机截图 |
| Regular and conditional channels：TP LIMIT、普通 LIMIT | `test_independent_binance_v2.py`、`demo-real-requests.json` |
| Hedge and one-way：双向允许/前置拒绝、对向待成交 | `test_independent_tool_contracts.py`、`test_independent_real_v2.py` |
| Fixed exits retain reduce-only：SHORT Hedge、LONG One-way | `test_independent_binance_v2.py` |
| Native conditional all：LONG TP all、竞争退出拒绝 | `test_independent_binance_v2.py`、`test_independent_real_v2.py` |
| Price semantics：可成交普通 LIMIT、SL LIMIT 触发未成交 | `test_independent_mock_v2.py`、`test_independent_real_v2.py` |
| Order identities survive triggering：同数字碰撞、触发子单、子单 edit | `test_independent_real_v2.py`、`test_execution_ledger.py`、`test_mcp_tool_contracts.py` |
| Edit chooses recoverable execution：native 部分成交、竞态成交扣目标、触发抢撤、替代丢回执 | `test_independent_real_v2.py`、`test_independent_receipt_recovery.py` |
| Capabilities and exchange results：适配器不支持、未知不重发 | `test_independent_real_v2.py`、`test_trade_batch.py` |

额外交叉审阅修复：同名随包提示词经用户修改后仍保留；等待触发与未知 fill 不写成零成交；旧裸 Algo ID 迁移只隔离不明归属，保留原始成交 payload，并保留已核验 regular/child 和跨配置归属；conditional edit 失败恢复不误报为成功撤单。

## 最终回归

- 全部相关 Python 模块（independent、position_protection、position_adoption、protection_integration、exit_policy、trade_batch、trade_tool、tool_registry、execution、MCP、stats_position、spot、prompt、decision；浏览器用例单独运行）：**736 passed，3 subtests passed，115.52 秒**。
- 最终实盘生命周期与真实 CCXT 拦截专项：`uv run pytest -q tests/test_independent_real_v2.py tests/test_independent_binance_v2.py`，**57 passed**，包含综合测试收集后新增的竞争 fixed/all 拒绝与不支持适配器零写场景。
- 最终上下文与持久回执补充：`uv run pytest -q tests/test_independent_exit_context.py tests/test_independent_receipt_recovery.py`，**15 passed**，含提交前 market all 快照标签及失败撤建恢复。
- 工具/MCP/batch 集成专项 **135 passed**；迁移归属账本专项 **37 passed**；上述与综合回归有重叠，不合并计数。
- 前端生产构建、2 项 Node 展示测试、桌面/手机浏览器验证通过。仅出现现有 Vite chunk 大小提示；隔离 API 测试使用短测试 JWT key 时有测试警告。
- `git diff --check` 通过；`openspec validate refine-independent-trading-tools --strict` 通过。以上为实施阶段的验证记录；后续规格同步、归档与发布以 Git 提交及镜像记录为准。
