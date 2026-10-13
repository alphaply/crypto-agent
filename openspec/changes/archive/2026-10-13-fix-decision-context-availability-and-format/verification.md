# Verification

验收日期：2026-10-13。以下为 apply 阶段验收结果；归档发布准备记录见文末。

## 实现与边界

- 决策纯读取同任务、账户、标的、版本和原文 hash 有效的已发布内容；新候选排队/失败仍保留最新有效记忆和最多三条已发布摘要，日期来自原始来源。
- 记忆、摘要与执行证据独立降级。原文继续保留；手动编辑/删除使相应旧视图失效。旧执行禁令不通过自由文本回退重新注入。
- 来源发布事务内重新校验，较旧任务不因离开最近窗口被丢弃，也不能顶替新来源。短期记忆原文与策略视图原子保存；其他连接在提交前可读完整旧版本。保存失败后复用持久化模型结果。
- 整理元数据绑定单次任务租约；切换账户/标的时拒绝不匹配发布。兼容没有 window_end 的旧记忆表。升级修复标记与 failed 重排队在同一保存点内提交；重启不无限重置尝试次数。
- 管理 API/页面显示发布截至时间、队列、失败及错误，显式重试只排队仍有效的 failed 来源。公开历史入口不暴露管理状态或重试操作。
- 持仓/挂单采用纯函数格式化；完整订单身份、精确量价、父子关联、all/fixed、归属、来源时间、覆盖/冲突/异常保留。Agent 和绑定聊天共用同一路径，其他退出模式沿用原行为。

## 自动验证

1. 相关后端回归：**546 passed，7 subtests passed**（78.23 秒）。选择 `tests/test_*memory*.py`、`test_*context*.py`、`test_independent*.py`，以及交易工具集成、策略摘要、决策架构、工作台/API 回归。测试数据库全部在临时目录，模型和交易边界均为离线 mock。
2. 最后补充迁移保存点与损坏引用校验后，定向运行 **40 passed**：
   `uv run pytest tests/test_decision_context_availability.py tests/test_context_projections.py tests/test_memory_workflow.py tests/test_memory_update_retries.py -q`
   覆盖迁移中断回滚、读者在未提交事务期间仍看到旧策略、存储失败复用一次模型结果，以及此前的可用性/租约/退避场景。
3. 格式边界测试 **9 passed**（已包含于上述后端回归）：原用户三单样例、极小数量、精确价格、两种价格、父子单、触发时全部、未知与零、最后已知量、归属、数据缺失、数量冲突和观察时间差异。
4. 真实 Edge 浏览器 + mock API：`uv run --with playwright python tests/browser_decision_memory.py`，**3 cases passed**（1440 中文、390 中文、1440 英文）。验证选择任务、旧内容截至时间、失败展开、重新排队、API 失败保留既有状态、公开页面无管理请求，并检查无页面脚本错误。
5. `npm run test:chat --prefix frontend`：**24 passed**；`npm run test:dashboard --prefix frontend`：**13 passed**。
6. `npm run build --prefix frontend`：成功；改动组件/fixture 的 ESLint：0 errors、0 warnings。
7. `git diff --check`：通过；`openspec validate fix-decision-context-availability-and-format --strict`：通过。

测试过程中修复了旧 schema 缺少 window_end 的读取兼容性、整理元数据越过租约生命周期的问题；实盘 mock 补齐真实配置必有的 symbol 字段，没有放松生产标的校验。

后端认证测试夹具仍提示测试用短 JWT key；Vite 仍有部分 chunk >500 kB 提示。均未阻塞验收。不执行依赖真实凭据的模型连通或交易测试。

## 格式样例与体积

见 [完整三单样例](../../../../docs/DECISION_CONTEXT_EXAMPLE.md) 及 `tests/test_trade_context_format.py` 的冻结夹具。同一持仓/订单区域旧逐行 JSON **2,028 字符**，新格式（包含观察时间、覆盖和执行说明）**456 字符**，减少 **77.5%**。字符计数不等于 token 计数，也不代表整个 Prompt 或费用同比例下降。

UI 截图：

- [中文桌面](../../../../docs/screenshots/decision-memory-status-1440-zh.png)
- [中文手机](../../../../docs/screenshots/decision-memory-status-390-zh.png)
- [英文桌面](../../../../docs/screenshots/decision-memory-status-1440-en.png)

## 工作区状态

保留此前 `add-provider-model-discovery` 的全部未提交实现、文档与截图，以及 `.agents/` 内容。本次修改的 README/WORKBENCH 与该变更共存。后续 archive 时须按具体变更选择提交范围，并执行主规范中的 Git/Docker 发布流程；本轮没有发布请求。

## 归档发布准备（2026-10-13）

用户已请求归档并推送 GitHub / Docker。主规范 decision-state-context 已同步：修改 2 条、新增 3 条要求，原 Purpose 与无关场景保留；全部 6 份主规范及本变更严格校验通过。归档目录为 `2026-10-13-fix-decision-context-availability-and-format`。

提交仅包含本变更，共享 README/WORKBENCH 按差异暂存；供应商模型发现保留在工作区。发布入口从本次 beta 提交的干净临时副本运行，重新构建前端及镜像并完成调度关闭的隔离烟测，再推送 Git 和两个镜像标签。实际 commit、digest 与发布完成状态以发布入口输出为准，生产容器不自动重启。
