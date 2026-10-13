# 验收记录

变更：`refine-decision-state-context`。验证日期：2026-10-13。
使用临时 SQLite、fake exchange 和固定模型响应；没有连接真实模型、执行真实交易或变更线上数据库。

## 行为结果

| 场景 | 验收依据与结果 |
| --- | --- |
| 默认 prompt 的14条无关历史、正常仓位与4张单 | `test_agent_and_bound_chat_use_current_state_without_history_or_old_bans`：Agent/Chat 状态一致，四张有效订单 ID 各出现一次，无完整历史回执块；市场、消息、策略条件保留 |
| 再增加100条已完成历史 | 同一集成测试及 `test_current_view_is_invariant_under_completed_history_and_error_prose`：默认状态文本不变 |
| 旧 unknown 解决，记忆模型不可用 | `test_resolved_reference_disappears_even_while_memory_model_fails`：不调用模型，下一次读取即移除该执行异常，保留“四小时收盘确认前不加仓” |
| 历史第21条之后的未决写入、Hedge / One-way | `test_old_unresolved_all_is_scoped_and_not_dropped_after_twenty`：保留原身份，标注同方向/共享净仓影响，不输出全局 blocked_actions，不增加金融写入 |
| completed + cleanup.pending、终态缺成交 | 恢复夹具与 `test_completed_operation_with_cleanup_pending_is_not_silently_resolved`：成交和清理分别呈现，后台仍推进清理 |
| 平仓完成后晚到入场成交留下残仓 | `test_confirmed_close_with_late_entry_keeps_residual_separate`：原平仓1.0保持已成交，当前残仓0.3单独保留管理需求；历史 error 本身不制造清理依赖 |
| 触发母单/执行子单、fixed/all、REAL/mock | 当前状态测试：有效 ID 去重，母子关系保留，all 为动态范围；缺失数量为 null，合约面值用于币数量换算 |
| 分项读取失败 | 直接测试 `MarketTool.get_account_status`，保存持仓/订单观察时间与条件通道错误；订单读取失败不抹掉已观察的持仓，覆盖未知 |
| local-only 与查询失败 | 本地投影不构造交易所客户端，query=not_requested；真实观察、查询时间、本地生成时间分开，needs_refresh 不发布旧规则，已确认执行不降级 |
| 历史维护与并发 | 持久水位跨调用复用，无变化历史不增加修订/updated_at；恢复/并发测试覆盖版本冲突、重载、清理继续及不重放写入 |
| 两条记忆输入 | `test_actual_memory_model_receives_latest_identity_in_both_writers` 捕获实际模型消息：每轮及滚动路径均含同身份最新结果，摘要不重复拼接，decision_json 原文不改写 |
| 窗口外引用、父子批次、相似数量及账户隔离 | 身份读取不受20条/4h边界影响，not_executed 尾项保留；缺失0.37身份不借0.30结果，跨账户返回 mismatch，不宣称解决 |
| 模型输出约束 | 来源ID/哈希、执行引用候选及额外权限字段校验；明显执行禁令拒绝成为 ready，纯策略条件和长正文完整保留 |
| 投影迁移、人工编辑、重启 | 添加式重复初始化，来源原文不改；人工编辑使哈希失效，后台重新整理；已持久化模型结果在重启恢复时复用 |
| 后台模型失败与租约 | 无新交易摘要也可排队；同一租约串行，60/300秒退避，第三次失败停止；缺投影不回退旧混合正文 |
| 模式/模板/本轮消息/MCP | 自定义四占位符不重复注入、源文件不改；同回合 ToolMessage 保留；附带保护、现货、审批、MCP范围及批次停止/依赖测试保持 |

## Prompt 对照

以下是合成样本的语义摘录，非线上交易结论。

修改前：当前订单说明后再次出现独立退出订单段落，附带完整“最新原操作核验记录”；聊天还追加“当前交易核验记录”。旧记忆/近三轮摘要中的“旧 unknown 待核验，禁止加仓”继续作为正文输入。

修改后：

```text
MARKET_RETAINED NEWS_RETAINED
持仓：LONG 0.15，成本100，来源mock_snapshot，归属task。
当前有效订单：mock:entry-A 0.10 @95；mock:sl-A 0.15 触发90；
              mock:tp-A 0.08 触发110；mock:tp-B 0.07 触发120。
SL 未覆盖估计：LONG=0，SHORT=0；无固定数量冲突。
当前相关执行异常：无（当前数据源读取完整）。
策略背景：等四小时收盘确认前不加仓；支撑95，突破110再评估。
```

出现真实未决订单时追加作用范围、原身份、缺失证据和执行层恢复信息。没有策略投影时明确显示“策略背景整理中/暂不可用”；原混合正文仍在历史中可查。

## 命令结果

- 集中回归：409 passed，3 subtests passed，63.35秒；补充显式记忆模型失败的 Agent/Chat 场景复验通过。
- `npm run build --prefix frontend`：通过；Vite 输出包体积提示（单个 chunk 超500 kB），无构建错误。
- `openspec validate refine-decision-state-context --strict`：通过。
- `git diff --check`：通过。Windows LF/CRLF 提示不影响行为。

回归文件范围：`test_decision_state_context`、`test_context_projections`、`test_independent_receipt_recovery`、
`test_trade_receipts`、`test_trade_evidence_concurrency`、`test_independent_admission`、`test_independent_exit_context`、
`test_trade_tool_integration`、`test_scoped_trade_rounds`、`test_decision_memory_context`、`test_retired_memory_context`、
`test_decision_architecture`、`test_decision_retry_refresh`、`test_memory_agent`、`test_memory_workflow`、
`test_memory_update_retries`、`test_memory_content_integrity`、`test_memory_text_integrity`、`test_memory_dispatch`、
`test_turn_memory_and_review`、`test_mcp_trade_recovery`、`test_mcp_tool_contracts`、`test_mcp_symbol_scope`、
`test_mcp_gateway`、`test_independent_prompt_v2`、`test_prompt_resolution`、`test_prompt_role_compat`、
`test_prompt_context_compaction`、`test_spot_portfolio_context`、`test_protection_integration`、`test_spot_market_context`。
均使用 `uv run pytest tests/<名称>.py ... -q` 执行。

## 部署边界

上述行为验收在归档前完成，使用本地离线环境。升级后旧混合记忆需要后台生成策略投影，过渡期间部分策略背景暂不可用。
策略/执行文字的语义分离仍依赖所配置模型；来源/身份/权限校验不能证明全部语义绝对正确。执行准入和恢复继续在执行层核验。

## 归档与发布流程补充

用户要求在本次归档时同步主规范，并将 Git 发版和 Docker 重新构建纳入后续 archive 默认流程。已同步 `decision-state-context` 的7项新增要求、`independent-position-exits` 的3项修改及 `trading-tool-contracts` 的2项修改，保留其余内容和场景；新增 `archive-release-workflow` 主规范及发布入口。

- `openspec validate --specs --strict`：6份主规范全部通过。
- `openspec validate refine-decision-state-context --strict`：通过。
- `uv run pytest tests/test_archive_release_workflow.py -q`：8 passed，覆盖成功顺序、启动重试、镜像输入未提交、前端/镜像构建失败、Git/镜像推送失败后的停止行为；使用命令替身，不产生远程写入。
- 发布入口在归档提交之后执行实际前端/Docker构建与空数据烟测，再推送 `beta` 和镜像。最终 commit、digest 与实际发布结果以该次运行输出和交付报告为准；归档本身不表示生产容器已更新。
