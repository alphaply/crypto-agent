# Tasks

以下仅为实施清单，按依赖顺序推进；每组随功能交付相应测试与文档。验证使用隔离数据库和可拦截的交易所，不把真实下单或修改生产配置作为验收步骤。

## 1. 规范动作与兼容契约

- [x] 1.1 在 `backend/agent/agent_models.py` 及工具模型层定义 open/close/edit/cancel 规范字段、六类退出和 fixed/all 互斥校验；扩展 `tests/test_tool_registry.py`、`tests/test_mcp_tool_contracts.py`，验证未知字段、布尔/零数量、价格组合与 unsupported all 均在写入前失败。
- [x] 1.2 为旧工具、旧参数和无版本批次建立显式兼容适配，新批次使用 contract_version=2，拒绝混合格式；通过契约测试证明旧 take_profit_limit 仍为普通 limit、旧 entry amount 仍是总量、纯 cancel 旧批次不会被误判为 v2。
- [x] 1.3 定义区分操作与订单状态的统一回执及带通道 order_id，保留旧回执适配；测试 submitted 不等于 filled、fixed/all 数量字段与相同数字的 regular/algo 身份不冲突。
- [x] 1.4 在 `docs/MCP.md` 和 `docs/TRADING_WORKFLOW.md` 写入新旧字段映射、数量单位、版本及模式边界；将其中最小调用示例纳入 schema 校验，确认文档与模型一致。

## 2. 持久化、迁移与幂等

- [x] 2.1 扩展实盘计划和模拟退出记录的版本、quantity_mode、可空计划数量及母子订单身份；为 `backend/database_independent.py` 编写事务迁移，并在 `tests/test_independent_exit_migration.py` 用旧数据库样本验证 ID、周期、成交、索引与回执完整保留，迁移无交易所写入。
- [x] 2.2 调整 `backend/utils/trade_operations.py`、MCP 操作存储与规范化顺序，保留旧请求摘要和首次 amount=0 快照；通过重启/跨别名重放测试确认相同请求不重发、不同意图复用 ID 被拒绝。
- [x] 2.3 兼容升级前已经持久化的比例缩量 replacement 与待核验操作，停止产生新自动缩量意图但不丢失旧恢复链；扩展 `tests/test_trade_batch.py` 与迁移测试，证明已提交替代单仅被查回且不会重复创建。
- [x] 2.4 在 `docs/WORKBENCH.md` 记录版本启用顺序及回滚限制；用隔离样本演练并记录“旧记录 fixed、新 all 重启不固化、旧版本不能接管活跃 v2”的验证结果。

## 3. 币安订单类型与方向适配

- [x] 3.1 集中订单能力/通道分类，接通六种类型的创建、查询、撤销与账本身份映射，替换 stop_market 单一条件判断；扩展真实 CCXT 请求拦截测试，验证普通 `/order` 与条件 `/algoOrder` 路由、触发价/限价和默认 CONTRACT_PRICE。
- [x] 3.2 实现 fixed 与原生条件 all 请求参数，保持 One-way reduceOnly、Hedge positionSide 及 all 不带 quantity/reduceOnly；在 `tests/test_position_protection.py` 或独立适配器测试中覆盖 LONG/SHORT、两种持仓模式、contractSize 换算及不支持组合零写入。
- [x] 3.3 补齐 One-way 对向持仓及待成交入场冲突检查、已知双向批次前置拒绝，并保持 Hedge 两向可开；在 `tests/test_trade_batch.py`、`tests/test_independent_exits.py` 验证拒绝发生在首次写入前且从不切换账户模式。
- [x] 3.4 在 `docs/TRADING_WORKFLOW.md` 写入订单类型/字段/all 能力矩阵与官方接口依据；用上一组拦截测试的请求结果逐项核对，并确认普通可成交限价不再被“盈利侧价格”校验误拒绝。

## 4. 独立退出业务与模拟一致性

- [x] 4.1 在实盘和模拟路径实现定量档与条件动态 all 并存，市价 all 使用提交前核验快照；扩展 `tests/test_independent_exits.py`，验证 1 BTC 的 0.2、0.3、all、分批 SL、后续加仓与仅目标方向全平。
- [x] 4.2 移除新自动比例缩量/补量及固定挂单合计的本地排他预留，保留单笔量校验；添加独立 quantity_conflict 统计，测试 0.7+0.6 的替代退出可交由交易所裁决、减仓后固定档不变且 cancel/edit 不被伪 pending 阻止。
- [x] 4.3 为模拟条件限价分开触发与成交、记录实际成交及未执行余量；测试触发未成交、跳空多档、执行时超额固定量不反向开仓、不虚记 PnL，并记录同根行情的确定性排序。
- [x] 4.4 将归零清理与比例缩量拆开，维护条件母单缺少数量、子单成交、暂时空读及迟到入场的对账；回归相关 independent_exits 测试，确认完整归零只清理同周期且残仓不自动补平。
- [x] 4.5 更新 `docs/TRADING_WORKFLOW.md` 和 `docs/WORKBENCH.md` 的数量冲突、全平、残仓与模拟限制说明；对照 fixed/all、冲突及归零场景逐项验收文档，删除不再成立的自动按比例缩量说明。

## 5. Edit 与 Cancel 的订单生命周期

- [x] 5.1 统一入场与退出 edit 的 remaining_amount，普通 LIMIT 优先原生修改；测试总量 1/已成交 0.2/剩余目标 0.5 转成总量 0.7，以及在途新增成交只消耗目标、不重新补量。
- [x] 5.2 对条件 edit 实现持久化撤销核验与重建，all 保持动态模式；扩展 `tests/test_independent_exits.py`，覆盖撤单超时、替代单丢回执重启恢复、目标已耗尽不重建、价格/数量冲突不自动改计划。
- [x] 5.3 实现触发后子单操作：edit 母单返回当前子单引用，cancel 母单在核验关联后撤子单未成交部分；测试触发竞态、子单部分成交、不可改 trigger_price、旧裸 ID 歧义及不操作外部订单。
- [x] 5.4 在 `docs/MCP.md` 和 `docs/WORKBENCH.md` 补充 native/cancel_replace 回执、新旧 ID、部分成交与触发后操作示例；验证示例使用实际有效身份且不承诺原子改单。

## 6. Agent、MCP 与批处理集成

- [x] 6.1 在 `tool_registry.py`、`tool_interfaces.py` 与 `backend/mcp/tools.py` 注册模式化规范工具和兼容入口，补齐独立退出 edit；运行工具目录/契约测试，确认独立模式推荐新工具、附带模式和现货工具保持原语义。
- [x] 6.2 将 MCP `guard.py`、`service.py` 的权限/额度分类接到规范动作与已归属目标；测试入场 edit 仍受增仓限制、退出 edit 的 trade 权限包含必要撤换、显式 cancel 需要 cancel scope、混合批次在首笔前验证两种权限。
- [x] 6.3 将 v2 平铺批次接入与单项相同的服务、账户一致性边界和逐项回执；扩展 `tests/test_trade_batch.py`、`tests/test_trade_tool_integration.py`，覆盖同轮多次 open、未成交入场后 close、failed/pending/unknown 阻断、not_executed 以及原操作重放。
- [x] 6.4 更新 `docs/MCP.md` 的规范调用、legacy execute_trade 与顺序 execute_trade_actions 说明；用契约测试验证分批建仓、两向 open、TP/SL 定量加 all、edit/cancel 和批次示例均可解析且没有权限旁路。

## 7. 跨轮提示词、账户上下文与前端

- [x] 7.1 按退出模式生成 REAL/STRATEGY 提示规则，移除独立模式的强制当轮保护假设并保留附带模式约束；用 prompt/context 测试验证“本轮 open、下轮 close”路径、同轮已成交可退出和用户自定义模板不被批量覆盖。
- [x] 7.2 更新 `exit_context.py`、MCP 查询、`stats_service.py` 的类型/覆盖/冲突与 all 展示；扩展 `tests/test_independent_exit_context.py`、`tests/test_stats_position_protection.py`，验证等待触发不等于成交、缺数量不显示为 0、SL limit 覆盖不被写成成交保证。
- [x] 7.3 更新现有前端退出卡片和图表的 fixed/all、触发价、委托价及状态展示；使用现有 admin exit fixture 增加新状态验证并运行 `npm run build --prefix frontend`，检查普通 LIMIT、条件 LIMIT 与 all 均无误导标签。
- [x] 7.4 同步 `README.md` 与用户运行文档，说明自主跨轮决策、固定档不自动重分配、all 的触发/快照差别和原生全平的账户方向范围；人工对照工具示例及前端 fixture，确认不再描述旧默认行为。

## 8. 跨模块验收

- [x] 8.1 运行本变更覆盖的 contracts、registry、exit_policy、trade_batch、trade_tool_integration、independent_exits、position_protection、operation/ledger、MCP 与 stats/context 测试；验证全链路 fixed/fixed/all、Hedge 两向、条件子单、升级重放场景通过，并回归 attached、adopt、spot 的原有行为。
- [x] 8.2 完成隔离账户的模拟跨轮演示及拦截式实盘接口演示，记录“第一轮建仓、第二轮分批退出、之后 edit/cancel、最终全平”的回执与截图；运行 `openspec validate refine-independent-trading-tools --strict`，逐项核对三份规格场景和任务完成证据后提交评审。
