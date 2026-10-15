# Proposal

## Why

短期记忆为了生成摘要而要求模型返回多份 JSON 投影、来源哈希并进行格式纠正，增加失败点且不符合用户要求的纯文本整理。交易批次虽已顺序执行，但保护计划版本冲突、提交阶段表达和冗长回执仍会干扰模型判断；QQBot Paper 配置也需要独立入口。

## What Changes

- 短期记忆流程保持“Agent 执行结束 → 本轮策略压缩成功保存 → 更新短期记忆”；输入为最近 4 小时内包含本轮的压缩摘要、旧版最近 7 天成交/平仓操作记录、上一轮短期记忆文本，不另取新闻。
- **BREAKING**：记忆模型只输出一份纯文本摘要，取消模型生成 summary/memory/projections JSON、来源哈希回传、格式纠正及为旧输出契约服务的恢复链路；删除失去用途的校验和兼容分支，保留有效历史、审计和交易执行安全约束。
- 批次保持请求数组顺序，保留下单前必要检查与并发协调，取消工具调用内提交后的查询、轮询及等待核验（不实施原定 1 秒、最多 3 次方案）。按交易所接口响应返回成功或失败；后台核验原操作继续保留，不自动补发尾项。
- 根据实际阶段区分明确失败、已确认成功和结果未确认；确认未提交的版本冲突可在原动作内重新加载并校验，可能已提交的请求不得通过重发解决。
- 将 `Ownership evidence indexing incomplete` 导致止损/止盈未接受纳入同一执行器修复：保留原范围必要的提交前归属检查与同步，不追加等待或轮询；必要证据仍不完整即返回未提交失败；真实归属冲突仍拒绝，不把本地索引未完成表述成交易所拒绝或已挂保护单。
- **BREAKING**：默认模型交易工具返回改为简短逐项文本：动作、目标/必要数量价格、成功或失败及原因、后续未执行；完整结构化事实保留供内部状态计算及授权审计使用。
- 将现有“QQBot 模拟交易 · Paper”面板移至独立 tab，保持其现有配置和 Paper Key 功能。

## Capabilities

### New Capabilities

- `paper-admin-navigation`: QQBot Paper 独立管理 tab 与现有功能入口。

### Modified Capabilities

- `decision-state-context`: 三类素材的纯文本记忆、移除旧投影输出契约、默认模型交易结果精简。
- `trading-tool-contracts`: 顺序执行中的接口结果直返与后台核验分离、并发冲突处理、结果确定性和模型回执边界。

## Impact

- 后端：`backend/agent/memory_agent.py`、`memory_service.py`、`memory_updates.py`、`memory_workflow.py`、`context_projections.py`、`memory_contract_recovery.py`、`decision_context.py` 及相关数据库发布读取路径；沿用旧版 7 天记录来源及任务/账户/标的范围。
- 执行：`unified_trading.py`、`unified_dispatch.py`、兼容动作适配器、`position_protection.py`、`position_management.py`、`trade_recovery.py`、`trade_operations.py`、`trade_summary.py` 和工具消费边界。
- 前端：`AdminPage.jsx`、`McpSettingsPanel.jsx`、`PaperTradingPanel.jsx`，相关记忆说明与运行结果展示。
- 不新增交易所、数据源或依赖；不改变交易权限、数量语义和已持久化操作身份。现有显式 independent 策略的明确失败处理保持兼容，提交结果未知项不在调用内核验，停止本批。
- 本变更仅规划；不删除历史数据、不执行实盘验证、不归档或发布。日志尚不能证明重复成交，也不能证明本次版本冲突的具体写入者。
