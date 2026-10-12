# 准入与恢复重设计验收

日期：2026-10-12。全部金融操作使用 fake exchange、模拟账本和临时 SQLite；未连接真实交易账户，未调用联网模型，未部署或修改线上订单。

## 实现与调用次数

有效 open/close/edit/cancel 不新增预检参数、恢复令牌或额外模型轮次。服务内部收集依赖，允许明确拒绝后的修正新单，保持权限、归属、幂等、真实相关未知及交易所约束。

| 验收路径 | 模型/网关请求 | 金融调用断言（不计初始化入场） | 证据 |
| --- | --- | --- | --- |
| 明确拒绝后直接新止损 | 两个原本请求，额外查询 0 | 拒绝尝试 1，新 create 1 | test_independent_admission / test_mcp_trade_recovery |
| Agent 拒绝 → 查询 → 新止损 | 指定的 3 个请求 | query 不写；拒绝 1、新 create 1 | test_scoped_trade_rounds::test_agent_rejected_stop_query_and_corrected_stop_in_one_round |
| Agent/Chat 同轮独立保护 | 每笔 1 个调用 | 拒绝 1、有效 create 1；Chat 逐项审批 | test_same_round_rejection_does_not_block_valid_independent_action |
| MCP 直接 open/close/edit/cancel | 每动作 1 个请求，查询 0 | create 2、edit 1、cancel 1 | test_mcp_direct_open_close_edit_cancel_each_need_one_call |
| 无关仓位接口故障下撤目标 | 1 个 cancel | 无新 create；目标撤销 1，先撤后维护 | test_target_cancel_survives_position_read_failure_and_no_prewrite_maintenance |
| 陈旧改单取证错误 | 1 个 edit | 当次读取刷新并 edit 1 | test_direct_edit_refreshes_its_old_query_error_in_same_call |
| 未知市价 → 依赖项 → 独立撤单 | 一个 independent 批次 | 未知平仓 create 1，第二平仓 0，独立撤单 1；重放不补发 | test_independent_batch_unknown_only_blocks_dependent_writes |
| 提交前 CAS 冲突 | 1 个动作，内部最多刷新 1 次 | 一次冲突后 create 1；持续冲突 create 0 | test_pre_submit_generation_conflict_refreshes_once_without_resending / test_persistent_pre_submit_conflict_stops_after_one_refresh |
| 提交后冲突 / ACK 丢失 | 原请求及可选只读恢复 | 原写入只发生 1 次；绝不重发 | test_trade_evidence_concurrency / test_trade_tool_integration |
| 清理事实已完成，但旧状态未记账 | 1 个新 open，查询 0 | 本地结束旧 episode，新 create 1，无额外撤改 | test_verified_finished_cleanup_opens_new_cycle_without_scheduler_or_query |

同方向真实未知会返回受影响 effect、具体订单/原操作与缺失事实；Hedge 无共享依赖的方向可继续，One-way 共享净仓仍检查。已确认终态不被旧诊断重新降级。新增批次策略省略时不进入旧请求哈希；显式策略和依赖参与哈希，旧未执行尾项不自动补发。

## 实际验证命令

PowerShell / 仓库根目录：

```powershell
.venv\Scripts\python.exe -m pytest tests -q --disable-warnings --tb=short --maxfail=8
.venv\Scripts\python.exe -m pytest tests/test_independent_admission.py tests/test_trade_receipts.py tests/test_trade_evidence_concurrency.py tests/test_independent_receipt_recovery.py tests/test_scoped_trade_rounds.py tests/test_mcp_trade_recovery.py tests/test_trade_admission_docs.py -q --tb=short
.venv\Scripts\python.exe -m pytest tests/test_mcp_tool_contracts.py tests/test_mcp_trade_recovery.py -q --tb=short
npm run build --prefix frontend
uv run --no-project --with playwright python tests/browser_trade_recovery.py
openspec validate redesign-trade-admission-recovery --strict
git -c core.safecrlf=false diff --check
```

- 第一轮实现回归：78 passed；加入批次和并发后 107 passed。
- 一轮全量回归：1538 passed，26 subtests passed，294.70 秒；包括现货、附带保护、legacy、Agent/Chat、MCP、real/binance v2。
- 补充跨周期可用性、调用次数和故障路径后，定向回归：84 passed，38.22 秒。
- 最终补充回归结果见本文末尾，保留上述实际运行记录，不将前一轮计数冒充最终测试集合。
- 前端构建通过（Vite 提示现有大于 500 kB chunk，不影响构建）；浏览器离线 fixture 桌面 1440×1100、移动 390×844 通过，无页面异常与横向溢出。截图已人工查看。
- OpenSpec strict 校验与 diff whitespace 检查通过。

浏览器环境说明：项目 venv 没有 playwright；使用 `uv run --no-project --with playwright` 的临时工具环境完成验证，没有修改项目依赖或锁文件。

## 发现并修复的问题

- 原先按异常类型把 InvalidOrder 认定为未提交。现在要求明确业务拒绝代码；测试中的明确拒绝 fixture 补入代码，模糊文本单独覆盖 unknown，重复客户端身份不作为无执行证明。
- 全周期维护被拆离后，原先隐含在维护中的接管仓位归属检查必须移到动作取证；已修复，外部增减仓及净额不变的混入成交测试仍拒绝未归属写入。
- 原改单兼容结果在重放时按 close 推断状态。现在按 edit 原意图投影，拒绝修改与撤建失败保持 failed，原订单状态独立保留。
- 原维护异常抛出会覆盖已知执行；改为结果与清理分别返回。旧测试改为检查真实 pending/成交及清理证据，未放宽未知写入限制。
- 两个晚到成交测试只匹配旧错误单词 `reconciliation`；改为断言结构化 `trade_recovery_required / cycle_cleanup_unverified`，数量尚未可见时依旧零新增写入。
- 原查询失败缺少传入 previous 时可能丢失已知成交。现在从相同任务/标的持久回执恢复原事实，query.failed 与 continuation.needs_refresh 分开报告。
- MCP 恢复过去把所有 blocker 指向当前操作；现在逐个按 principal/profile 映射，无法映射明确标记，不暴露内部 ID。
- 最后接口核对发现 MCP 扁平批次 wrapper 漏传 execution_policy；补齐公开签名、说明与序列化，并验证策略/depends_on 持久化、重放和非法策略拒绝。MCP 专项 37 passed，24.26 秒。

## 持久化与迁移演练

`test_trade_evidence_concurrency` 使用临时库重复 initialize_schema、保留 generation/evidence、SQLite backup 后回读；两个进程从同一快照竞争，只有一个 CAS 成功。真实服务保存路径覆盖同方向/对向旧快照、Hedge 刷新继续、One-way 共享依赖及网络调用期间另一连接可开启写事务。

交易请求、原哈希、原 episode 与原始回执修订不清空。恢复为添加式。生产升级/回滚必须暂停新旧交易及维护 worker，保留表与备份；不能让旧 worker 接手不理解的 independent 在途请求。这里没有执行生产升级、清理或回滚。

## independent-position-exits 场景对应

以下测试名省略 `tests/` 和 `.py`；完整测试文件在仓库 tests 目录。各行覆盖对应规格场景，故障注入均为离线。

| Scenario | 测试证据 |
| --- | --- |
| New entry after verified closure | test_independent_receipt_recovery::test_close_cleanup_recovery_then_explicit_new_entry_once；test_independent_admission::test_verified_finished_cleanup_opens_new_cycle_without_scheduler_or_query |
| Uncertain all cannot close a later position | test_independent_receipt_recovery::test_close_cleanup_recovery_then_explicit_new_entry_once；test_independent_real_v2 |
| Independent opposite side in hedge mode | test_independent_admission::test_opposite_unknown_only_blocks_shared_net_position；test_trade_evidence_concurrency::test_scope_reservation_rejects_stale_same_or_opposite_snapshot |
| Reduce a verified residual | test_independent_receipt_recovery::test_late_verified_residual_can_be_explicitly_managed |
| Rejected stop does not block a corrected stop | test_independent_admission::test_corrected_stop_after_definite_rejection_needs_no_unlock |
| Unknown market close still blocks a second dependent close | test_scoped_trade_rounds::test_independent_batch_unknown_only_blocks_dependent_writes |
| Target cancellation does not require unrelated position reads | test_independent_admission::test_target_cancel_survives_position_read_failure_and_no_prewrite_maintenance |
| Edit follows the target and requested effect | test_independent_admission::test_direct_edit_refreshes_its_old_query_error_in_same_call；test_independent_real_v2 |
| One-way shared exposure requires shared evidence | test_independent_admission::test_opposite_unknown_only_blocks_shared_net_position |
| Verified flat with no active cycle | test_trade_tool_integration::test_real_close_skips_confirmed_empty_side_and_continues_batch |
| History proves the original residual position | test_independent_receipt_recovery::test_missing_cycle_query_diagnoses_but_does_not_restore |
| Manual or conflicting position cannot be adopted implicitly | test_independent_receipt_recovery::test_missing_cycle_never_restores_conflicting_or_unreadable_history；test_position_adoption_gateway |
| Rejected attempt does not invalidate complete execution history | test_independent_admission::test_missing_cycle_with_confirmed_rejection_preserves_history |
| Cancellation acknowledgement lacks fill fields | test_independent_exits::test_real_cleanup_unverified_cancel_fill_retains_cycle_until_position_visible；test_independent_binance_v2 |
| One unresolved order among several | test_independent_receipt_recovery::test_read_failure_does_not_prevent_other_order_evidence |
| Legacy rejection is classified from evidence | test_trade_receipts；test_independent_admission::test_ambiguous_invalid_order_does_not_become_definite_rejection |
| Persistent read failure remains actionable | test_independent_receipt_recovery::test_persistent_unknown_blocks_dependent_writes_after_read_query |
| Adding historical failures leaves valid trading available | test_independent_admission::test_unrelated_failure_cannot_change_valid_admission |
| Every refusal identifies a necessary condition | test_independent_admission；test_independent_exits::test_real_filled_entry_ack_waits_for_position_across_restart；test_mcp_gateway |
| Waiting conditional all is an ordinary live order | test_trade_receipts::test_execution_classification_drives_unresolved_view；test_independent_real_v2 |
| Failed amendment leaves its original order relevant | test_independent_real_v2::test_native_edit_rejection_replay_is_not_success；test_trade_receipts |
| Terminal label does not hide unresolved execution | test_trade_receipts::test_execution_classification_drives_unresolved_view；test_independent_binance_v2 |
| Unrelated maintenance failure does not veto a verified target | test_independent_admission::test_target_cancel_survives_position_read_failure_and_no_prewrite_maintenance |
| Post-trade cleanup fails after a confirmed fill | test_trade_tool_integration::test_market_short_close_keeps_fill_when_cleanup_ack_has_no_fill |
| Query and immediate admission agree on a rejected stop | test_independent_admission::test_corrected_stop_after_definite_rejection_needs_no_unlock |
| Target-specific warning does not imply every edit is blocked | test_independent_admission / test_trade_receipts 的 continuation 投影；前端目标长 ID fixture，blocked_actions 不含全局 edit |
| Missing evidence is not an empty allow list | test_independent_receipt_recovery::test_failed_query_preserves_confirmed_execution_and_marks_advice_stale；browser_trade_recovery |

## trading-tool-contracts 场景对应

| Scenario | 测试证据 |
| --- | --- |
| Entry is still unfilled | test_trade_tool_integration::test_canonical_batch_cannot_exit_unfilled_entry |
| Cancellation permissions in a mixed batch | test_mcp_tool_contracts；test_mcp_gateway |
| Flat legacy cancellation is not a version signal | test_independent_tool_contracts；test_tool_registry |
| Cleanup completes before the next entry | test_independent_receipt_recovery::test_close_cleanup_recovery_then_explicit_new_entry_once |
| Old close-all order can affect a new entry | test_independent_receipt_recovery::test_admission_isolates_hedge_but_blocks_one_way_shared_unknown |
| A rejected take profit does not suppress independent protection | test_scoped_trade_rounds::test_independent_batch_continues_and_replay_never_sends_again |
| Explicit dependency preserves cancel-then-create intent | test_scoped_trade_rounds::test_only_completed_dependency_allows_successor |
| Invalid policy or dependencies fail before any writes | test_independent_tool_contracts::test_batch_dependencies_are_strict_backward_unique_indices / test_batch_policy_hash_compatibility_and_metadata_boundaries |
| Unknown execution still blocks dependent actions in independent mode | test_scoped_trade_rounds::test_independent_batch_unknown_only_blocks_dependent_writes |
| Replay never resumes skipped tail | test_scoped_trade_rounds::test_only_completed_dependency_allows_successor；test_independent_receipt_recovery::test_batch_query_keeps_unexecuted_tail_and_hash |
| Agent checks the original close | test_independent_receipt_recovery::test_close_cleanup_recovery_then_explicit_new_entry_once |
| Query after a failed tool in one model turn | test_scoped_trade_rounds::test_agent_rejected_stop_query_and_corrected_stop_in_one_round；test_decision_memory_context |
| Read permission does not resume replacement | test_mcp_trade_recovery::test_mcp_read_and_replay_refresh_same_internal_operation_without_writes |
| Successful query of a rejected operation | test_mcp_trade_recovery::test_mcp_definite_rejection_then_direct_corrected_stop；test_scoped_trade_rounds |
| Query cannot refresh current scope | test_independent_receipt_recovery::test_failed_query_preserves_confirmed_execution_and_marks_advice_stale；历史 episode/现周期对照 |
| Diagnostic error is only a warning | test_independent_admission::test_unrelated_failure_cannot_change_valid_admission |
| A missing order ID still has a usable recovery identity | test_trade_tool_integration::test_market_close_lost_ack_retains_original_query_identity |
| MCP recovery identity is accessible to the caller | test_mcp_trade_recovery::test_mcp_each_blocker_maps_only_within_principal_profile |
| Admission changes between query and write | test_trade_evidence_concurrency；test_independent_admission 的单次刷新/持续冲突用例 |
| Protection survives an unrelated definite failure | test_scoped_trade_rounds::test_same_round_rejection_does_not_block_valid_independent_action |
| Failure does not silently reverse a sequential intention | test_scoped_trade_rounds::test_only_completed_dependency_allows_successor |
| Definite failure does not imply a hidden sequence dependency | test_scoped_trade_rounds::test_same_round_rejection_does_not_block_valid_independent_action；纯准入 open 不受旧拒绝影响 |
| User rejection is not an exchange failure | test_scoped_trade_rounds::test_chat_user_rejection_still_stops_writes_but_allows_read |
| Unsupported tool effect cannot be assumed independent | test_decision_memory_context::test_scheduler_and_chat_stop_multi_tool_calls_after_failed_or_uncertain_result |
| Old request replays after upgrade | test_independent_tool_contracts::test_batch_policy_hash_compatibility_and_metadata_boundaries；test_trade_batch |
| New policy cannot be silently ignored | test_independent_tool_contracts::test_batch_policy_hash_compatibility_and_metadata_boundaries；test_mcp_tool_contracts |
| Corrected stop works without querying first | test_independent_admission::test_corrected_stop_after_definite_rejection_needs_no_unlock；test_mcp_trade_recovery |
| Refreshable evidence is resolved inside the action | test_independent_admission::test_direct_edit_refreshes_its_old_query_error_in_same_call / test_pre_submit_generation_conflict_refreshes_once_without_resending |
| Unresolved submission is not retried to improve availability | test_trade_evidence_concurrency::test_network_is_outside_reservation_transaction_and_post_send_conflict_never_retries；test_independent_admission |

## UI 证据与限制

- [桌面截图](evidence/desktop-trade-reasoning.png)
- [移动截图](evidence/mobile-trade-reasoning.png)

合成 fixture 覆盖原止损拒绝但查询成功、真实未知、目标级限制、建议待刷新、旧格式回执以及已成交但清理待核验。无“全面可交易”提示；长 ID 自动折行，原始 JSON 默认折叠。

截图中的原线上止损拒绝代码尚未取得；不能声称该历史单已自动恢复。旧记录只有可证明无执行影响时才解除限制。所有权历史分页和交易所硬性约束仍按现有契约执行。

## 最终结果

最终全量回归：**1543 passed，26 subtests passed，309.53 秒**。MCP 扁平 wrapper 最后补充的契约与恢复专项：**37 passed，24.26 秒**（专项与全量存在重复，不相加作为独立用例总数）。旧错误文案断言修复及后续取证/跨周期路径专项另有 **59 passed，35.05 秒**。

两份 delta spec 共 **56 个场景**全部列入上面的验证映射；严格校验通过。前端构建、桌面/手机离线验收和 diff 检查通过。当前请求范围内没有待实现或延期项；没有部署、真实交易、线上历史单恢复或 OpenSpec 归档操作。
