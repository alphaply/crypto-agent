# 七份 delta 场景核对记录

审查日期：2026-10-14。范围：231个场景（包含同步前置规范时保留的一项旧撤单场景）；验收仅使用隔离数据库和fake exchange，不进行真实交易。归档与发布阶段另行记录。

这是逐场景证据索引，不把测试总数当作全部规范通过的证明。直接覆盖表示所列真实测试断言主要行为；组合审查表示局部测试和生产分支共同支持，不声称存在同样的完整端到端场景；待修复表示独立审查发现真实缺陷。各行链接均指向实际存在的测试函数，末尾摘录真实assert。组合行明确说明未在单个测试中证明的条件，不能仅凭本索引勾选任务。

独立重跑核心八个新测试文件（unified_trading/surface_chain/recovery_identity、mcp_canonical_catalog、paper_ordered_batch、visible_position_management/projection、management_lifecycle_api）：186 passed in43.17s。Paper多venue修订后单独重跑paper_ordered_batch：28 passed in7.95s。数字为当时collection快照，最终新增测试须补跑；所引旧回归结果以本变更最终verification记录为准。收尾定向检查paper_ordered_batch、paper静态拒绝、普通spot cancel、附带未知handoff与identity恢复：46 passed in16.65s；strict验证通过，保留前置规范尚未同步的INFO。

## 模式和身份矩阵

| 模式/角色 | 默认目录 | 能力边界 |
| --- | --- | --- |
| REAL/STRATEGY independent主Agent/small/绑定聊天 | execute_trade_actions、get_trade_operation（2） | 限价入场、六种原退出、fixed/all按原能力、不提供protect |
| REAL/STRATEGY attached_required/optional | 同上（2） | required入场TP/SL必填，optional可省；整仓protect；STRATEGY close保持市价 |
| SPOT_DCA/live spot | 同上（2） | LONG买/卖、限价入场、native TP/SL/OCO与可核验edit，共享资产库存/父预算 |
| 临时只读聊天/Memory/策略摘要 | 0个交易工具 | 保留只读上下文，不claim/开户 |
| live read | 7个核心读取 | 无execute，读取不claim |
| live read+trade和/或cancel且有有效profile | 8个核心名称 | cancel-only仅cancel；profile名字并集，逐次重新鉴权 |
| paper read | 8项 | 七个核心读取+排行榜 |
| paper read+trade | 11项 | 核心八项+bank/configure/leaderboard；原market/limit/保护与sizing |

核心：get_trading_context、search_symbols、get_market、get_news、get_account_state、get_trade_records、execute_trade_actions、get_operation。paper额外paper_bank、configure_paper_account、get_paper_leaderboard。旧单项、execute_trade、adopt、paper适配器隐藏但受原授权直接可调；事件扫描通过/api/paper/events。

## 审查发现及收尾状态

1. Paper允许spot原生限价字段后丢弃：core已补整批静态拒绝及schema一致测试，最终广泛回归和核心定向验收均已通过，见verification记录。
2. Paper同标的多venue目标：已修。close/protect逐项读取原账户、解析唯一当前exposure再取该venue行情；多venue明确failed/not_submitted且无报价/资金/事件影响。八个spot/swap参数场景通过；cancel/edit按原ID，open保留默认venue。
3. 普通外部spot sell取消未知终态预留：manager已补pending资源关联和原身份恢复；新增真实普通卖单取消测试验证预留保留、终态恢复后解锁且只撤一次。
4. docs/MCP.md §5已由root统一为visible_first_claim、按具体动作校验与核验释放后可退休，独立源码读取确认旧默认strict_owned表述已移除。
5. 收尾定向检查已覆盖附带未知提交normal/handoff原native ID核验、普通spot pending取消恢复以及native identity mismatch拒绝（46项通过）；更广的后台恢复与管理worker完整集以root最终verification为准。
6. Paper父意图存储wire缺operation_id：已窄修并通过实际SDK新父/旧已成交父与SQLite INSERT失败例；只在父意图登记区转换连接/SQLite异常，保留原ID与unknown/query.failed。本次submission_state=not_submitted不宣称原父从未成交；意图冲突仍ValueError，postcommit分类不受影响。
7. 已删除任务的v3新意图曾默认回落STRATEGY：已修。缺可信当前配置时拒绝新提交；保存的原父意图严格匹配后仅查询；逐项adapter拒绝使用旧快照继续提交。3个新增反例通过，补丁后定向160项通过；最终广泛1123项结果在该补丁前完成，明确分别记录。
8. 接受未知写入交接后删除原任务曾清理原请求/回执：已修。实际transfer→delete→receiver query及重启验证保留原父/子ID、hash、actor和相关订单/成交/周期/审计；无关私有历史仍删，恢复权限不扩大。新增3例通过，相关删除/恢复/现货定向105项通过。
9. 完整回归发现英文订单核验诊断可能进入旧策略过滤：已窄修具体order/submission描述，新增CPI/4h/价格条件保留与投影拒绝反例；流水线保留unknown原始审计、调用顺序和后台完成状态。最后40个受影响文件462项通过，完整回归的唯一失败已在其中验收，详见verification。

同步前须先同步add-exclusive-position-management，其Verified external changes do not disable independent exits尚未进入主规范。本次apply不执行sync/archive/release。


映射统计：231场景；直接覆盖191, 组合审查40。


## decision-state-context（14场景）

实现对照：[trade_context.py](../../../../backend/utils/trade_context.py), [trade_context_format.py](../../../../backend/utils/trade_context_format.py).

| # | 场景 | 实际测试证据/未单独证明的边界 | 结论 |
| --- | --- | --- | --- |
| 1 | Completed history does not grow the default prompt | [test_current_view_is_invariant_under_completed_history_and_error_prose](../../../../tests/test_decision_state_context.py#L28) | 直接覆盖 |
| 2 | An ordinary independent position | [test_user_three_orders_compact_complete_and_read_only](../../../../tests/test_trade_context_format.py#L38) | 直接覆盖 |
| 3 | Bound chat uses the same state | [test_agent_and_bound_chat_queries_project_resources_without_enrollment](../../../../tests/test_exclusive_position_management.py#L765)；[test_attached_and_spot_formatters_show_authority_and_partial_evidence](../../../../tests/test_visible_management_projection.py#L52) | 组合审查 |
| 4 | An external position has no local cycle | [test_manual_resource_can_be_claimed_without_cycle_or_false_history](../../../../tests/test_visible_management_projection.py#L21) | 直接覆盖 |
| 5 | A visible resource belongs to another manager | [test_competitor_is_local_and_preserves_current_quantities](../../../../tests/test_visible_management_projection.py#L38) | 直接覆盖 |
| 6 | Normal position and three orders | [test_user_three_orders_compact_complete_and_read_only](../../../../tests/test_trade_context_format.py#L38) | 直接覆盖 |
| 7 | Trigger limit, dynamic all and child identity | [test_tiny_exact_quantities_two_prices_parent_and_dynamic_all](../../../../tests/test_trade_context_format.py#L60)；[test_parent_child_identity_and_unknown_quantity_are_not_duplicated](../../../../tests/test_decision_state_context.py#L40) | 直接覆盖 |
| 8 | Missing observations and ownership conflicts | [test_unknown_is_not_zero_and_last_known_ownership_remain](../../../../tests/test_trade_context_format.py#L73)；[test_failed_read_does_not_mean_empty_account_and_terminal_missing_fill_is_issue](../../../../tests/test_decision_state_context.py#L136) | 直接覆盖 |
| 9 | Scheduled agent and bound chat share rendering | [test_agent_and_bound_chat_queries_project_resources_without_enrollment](../../../../tests/test_exclusive_position_management.py#L765)；[test_attached_and_spot_formatters_show_authority_and_partial_evidence](../../../../tests/test_visible_management_projection.py#L52) | 组合审查 |
| 10 | The owner manages a human amended order | [test_manual_quantity_price_and_trigger_changes_replace_observed_values](../../../../tests/test_exclusive_position_management.py#L220)；[test_owned_manual_resource_does_not_need_agent_entry_history](../../../../tests/test_visible_management_projection.py#L31) | 直接覆盖 |
| 11 | An unrelated incomplete observation exists | [test_target_cancel_ignores_unrelated_missing_position](../../../../tests/test_independent_admission.py#L83)；[test_owned_limit_price_edit_ignores_unrelated_unknown_order](../../../../tests/test_independent_admission.py#L23) | 组合审查 |
| 12 | A twenty action paper batch | [test_paper_twenty_item_model_projection_has_one_summary](../../../../tests/test_paper_ordered_batch.py#L225) | 直接覆盖 |
| 13 | Failed and unexecuted steps remain distinguishable | [test_default_error_stop_and_uncertain_replay](../../../../tests/test_unified_trading.py#L160) | 直接覆盖 |
| 14 | Complete evidence requested explicitly | [test_paper_sdk_batch_and_recovery_compact_vs_detail](../../../../tests/test_mcp_canonical_catalog.py#L137) | 直接覆盖 |

## independent-position-exits（24场景）

实现对照：[independent_exits.py](../../../../backend/utils/independent_exits.py), [position_management.py](../../../../backend/utils/position_management.py).

| # | 场景 | 实际测试证据/未单独证明的边界 | 结论 |
| --- | --- | --- | --- |
| 1 | Several entries on the same side | [test_fixed_stages_dynamic_remainder_and_addition](../../../../tests/test_independent_real_v2.py#L162) | 直接覆盖 |
| 2 | One side exits in a hedge position | [test_two_fixed_stages_then_dynamic_all_closes_only_target_direction](../../../../tests/test_independent_mock_v2.py#L35) | 直接覆盖 |
| 3 | An observed baseline and external additions are managed together | [test_fixed_stages_dynamic_remainder_and_addition](../../../../tests/test_independent_real_v2.py#L162)；[test_verified_external_changes_keep_exit_management_available](../../../../tests/test_exclusive_position_management.py#L204) | 直接覆盖 |
| 4 | Two fixed stages followed by the remainder | [test_two_fixed_stages_then_dynamic_all_closes_only_target_direction](../../../../tests/test_independent_mock_v2.py#L35) | 直接覆盖 |
| 5 | Add after exits were placed | [test_fixed_stages_dynamic_remainder_and_addition](../../../../tests/test_independent_real_v2.py#L162) | 直接覆盖 |
| 6 | Split stop losses | [test_split_stop_losses_gap_fixed_fixed_all_and_no_duplicate_fills](../../../../tests/test_independent_mock_v2.py#L60) | 直接覆盖 |
| 7 | Human adds to a claimed position before an all trigger | [test_close_all_uses_current_reduced_quantity_with_real_close_only_semantics](../../../../tests/test_exclusive_position_management.py#L672)；[test_fixed_stages_dynamic_remainder_and_addition](../../../../tests/test_independent_real_v2.py#L162) | 组合审查 |
| 8 | Flat snapshot is temporarily stale | [test_condition_channel_read_failure_makes_coverage_unknown](../../../../tests/test_decision_state_context.py#L89)；[test_market_short_close_keeps_fill_when_cleanup_ack_has_no_fill](../../../../tests/test_trade_tool_integration.py#L67) | 组合审查 |
| 9 | Independent cycle finishes | [test_external_fullclose_cleans_old_orders_and_later_close_skips](../../../../tests/test_exclusive_position_management.py#L383)；[test_authority_survives_flat_pause_and_explicit_release](../../../../tests/test_visible_position_management.py#L149) | 直接覆盖 |
| 10 | Filled market close with incomplete cleanup | [test_market_short_close_keeps_fill_when_cleanup_ack_has_no_fill](../../../../tests/test_trade_tool_integration.py#L67) | 直接覆盖 |
| 11 | Entry fills during cleanup | [test_confirmed_close_with_late_entry_keeps_residual_separate](../../../../tests/test_trade_tool_integration.py#L107) | 直接覆盖 |
| 12 | Human closes a managed position | [test_external_fullclose_cleans_old_orders_and_later_close_skips](../../../../tests/test_exclusive_position_management.py#L383) | 直接覆盖 |
| 13 | Human reopens after flattening | [test_verified_external_flatten_reopen_creates_successor_boundary](../../../../tests/test_exclusive_position_management.py#L401) | 直接覆盖 |
| 14 | External fill arrives during cleanup | [test_late_external_entry_fill_during_cleanup_preserves_residual_without_autoclose](../../../../tests/test_exclusive_position_management.py#L516) | 直接覆盖 |
| 15 | Another manager discovers the flat scope | [test_query_does_not_claim_or_mutate_and_reports_competition](../../../../tests/test_visible_position_management.py#L66)；[test_authority_survives_flat_pause_and_explicit_release](../../../../tests/test_visible_position_management.py#L149) | 组合审查 |
| 16 | Verified flat with no active cycle | [test_missing_cycle_query_diagnoses_but_does_not_restore](../../../../tests/test_independent_receipt_recovery.py#L247)；[test_real_close_skips_confirmed_empty_side_and_continues_batch](../../../../tests/test_trade_tool_integration.py#L283) | 直接覆盖 |
| 17 | History proves the original residual position | [test_late_verified_residual_can_be_explicitly_managed](../../../../tests/test_independent_receipt_recovery.py#L343)；[test_missing_cycle_rejects_incomplete_or_conflicting_evidence](../../../../tests/test_independent_receipt_recovery.py#L267) | 组合审查 |
| 18 | Manual or conflicting position cannot be adopted implicitly | [test_readonly_disabled_and_outside_symbol_cannot_claim](../../../../tests/test_visible_position_management.py#L133)；[test_query_does_not_claim_or_mutate_and_reports_competition](../../../../tests/test_visible_position_management.py#L66) | 直接覆盖 |
| 19 | Rejected attempt does not invalidate complete execution history | [test_missing_cycle_with_confirmed_rejection_preserves_history](../../../../tests/test_independent_admission.py#L132) | 直接覆盖 |
| 20 | Exclusive authorization permits automatic baseline enrollment | [test_startup_baseline_and_three_external_orders_are_readonly_and_deduplicated](../../../../tests/test_exclusive_position_management.py#L183)；[test_visible_direct_request_manages_only_its_hedge_target](../../../../tests/test_visible_position_management.py#L88) | 直接覆盖 |
| 21 | Read-only cycle diagnosis has no management writes | [test_agent_and_bound_chat_queries_project_resources_without_enrollment](../../../../tests/test_exclusive_position_management.py#L765)；[test_missing_cycle_query_diagnoses_but_does_not_restore](../../../../tests/test_independent_receipt_recovery.py#L247) | 直接覆盖 |
| 22 | Human reduction leaves excessive fixed exits | [test_quantity_conflict_does_not_block_explicit_edit_or_cancel](../../../../tests/test_independent_mock_v2.py#L268)；[test_fixed_exit_never_silently_shrinks_after_manual_reduction](../../../../tests/test_exclusive_position_management.py#L662) | 直接覆盖 |
| 23 | External addition changes close-all coverage | [test_fixed_stages_dynamic_remainder_and_addition](../../../../tests/test_independent_real_v2.py#L162) | 直接覆盖 |
| 24 | Enrollment does not satisfy a failed cancel dependency | [test_explicit_dependency_requires_completed_and_independent_continues](../../../../tests/test_unified_trading.py#L170) | 直接覆盖 |

## model-tool-catalog（29场景）

实现对照：[tool_registry.py](../../../../backend/agent/tool_registry.py), [canonical_tools.py](../../../../backend/mcp/canonical_tools.py), [server.py](../../../../backend/mcp/server.py).

| # | 场景 | 实际测试证据/未单独证明的边界 | 结论 |
| --- | --- | --- | --- |
| 1 | Trading agent runs in any supported mode | [test_actual_main_and_small_bind_exactly_two_tools](../../../../tests/test_unified_trading.py#L429)；[test_published_mode_schema_and_default_catalog](../../../../tests/test_unified_trading.py#L91) | 直接覆盖 |
| 2 | Bound task chat requests one cancellation | [test_bound_chat_v3_approves_each_action_and_reject_stops_tail_but_query_available](../../../../tests/test_unified_trading.py#L364) | 组合审查 |
| 3 | Read-only chat sees a manual position | [test_published_mode_schema_and_default_catalog](../../../../tests/test_unified_trading.py#L91) | 直接覆盖 |
| 4 | Memory or summary role is initialized | [test_memory_only_one_call_no_rules_and_cost](../../../../tests/test_memory_agent.py#L77)；[test_actual_audit_includes_input_raw_output_and_no_rule_tools](../../../../tests/test_memory_agent.py#L211) | 直接覆盖 |
| 5 | Fully authorized live client discovers tools | [test_live_key_does_not_receive_paper_catalog_and_cancel_only_keeps_executor](../../../../tests/test_mcp_canonical_catalog.py#L126) | 直接覆盖 |
| 6 | Client inspects the execution schema | [test_paper_catalog_does_not_publish_live_parameters](../../../../tests/test_mcp_canonical_catalog.py#L35)；[test_published_mode_schema_and_default_catalog](../../../../tests/test_unified_trading.py#L91) | 直接覆盖 |
| 7 | Read-only live key discovers tools | [test_live_key_does_not_receive_paper_catalog_and_cancel_only_keeps_executor](../../../../tests/test_mcp_canonical_catalog.py#L126) | 直接覆盖 |
| 8 | Cancellation-only live client submits an entry | [test_live_key_does_not_receive_paper_catalog_and_cancel_only_keeps_executor](../../../../tests/test_mcp_canonical_catalog.py#L126)；[test_new_tools_cannot_bypass_profile_mode_or_cancel_scope](../../../../tests/test_mcp_tool_contracts.py#L127) | 组合审查 |
| 9 | Paper key discovers its default tools | [test_paper_catalog_counts_scopes_and_hidden_compatibility](../../../../tests/test_mcp_canonical_catalog.py#L21)；[test_paper_catalog_does_not_publish_live_parameters](../../../../tests/test_mcp_canonical_catalog.py#L35) | 直接覆盖 |
| 10 | One key has spot and independent swap profiles | [test_live_key_does_not_receive_paper_catalog_and_cancel_only_keeps_executor](../../../../tests/test_mcp_canonical_catalog.py#L126)；[test_explicit_scope_defaults_and_fresh_profiles](../../../../tests/test_mcp_symbol_scope.py#L72)。SDK断言名称并集；context逐profile真实配置路由经源码核对，未在同一个端到端场景证明全部模式参数。 | 组合审查 |
| 11 | Profile or scope changes after discovery | [test_live_key_does_not_receive_paper_catalog_and_cancel_only_keeps_executor](../../../../tests/test_mcp_canonical_catalog.py#L126)；[test_unauthenticated_discovery_and_revoked_key](../../../../tests/test_mcp_gateway.py#L65)；[test_narrowing_and_import_cannot_orphan_unrestricted_orders](../../../../tests/test_mcp_symbol_scope.py#L184)。目录重读、撤销Key和范围保存分别覆盖；没有远端实际MCP客户端缓存实验。 | 组合审查 |
| 12 | New paper conversation needs the bot identity | [test_sdk_directory_bootstrap_cancel_first_and_read_only_recovery](../../../../tests/test_unified_surface_chain.py#L18) | 直接覆盖 |
| 13 | Live key has no authorized profile | [test_sdk_initialization_tool_list_and_scoped_call](../../../../tests/test_mcp_gateway.py#L47)。context空集合分支与无profile catalog由源码核对，未专门构造空集合SDK调用。 | 组合审查 |
| 14 | Client asks for positions only | [test_account_facets_and_records_do_not_leak_or_append_full_account](../../../../tests/test_mcp_canonical_catalog.py#L45) | 直接覆盖 |
| 15 | Client checks an order after submission | [test_records_default_compact_retains_facts_and_detail_returns_original](../../../../tests/test_mcp_canonical_catalog.py#L83)；[test_mcp_get_order_and_get_trade_history_read_only_contracts](../../../../tests/test_mcp_tool_contracts.py#L567) | 直接覆盖 |
| 16 | Paper client reads account history | [test_paper_history_compact_keeps_cash_and_fill_observation](../../../../tests/test_mcp_canonical_catalog.py#L101)；[test_bank_net_equity_limits_and_receipts](../../../../tests/test_mcp_paper.py#L60)；[test_liquidation_cancels_exits_and_notifies_with_debt](../../../../tests/test_mcp_paper_v2.py#L165)。fills投影、bank和liquidation事件分别覆盖；history读取同一账户事件表，未将三类事件装入一个查询。 | 组合审查 |
| 17 | Paper searches an unfamiliar stock token | [test_symbol_search_finds_tencent_on_other_venue_after_default_failure](../../../../tests/test_mcp_paper_v2.py#L209)；[test_symbol_discovery_queries_catalogue_and_maps_mub](../../../../tests/test_mcp_paper_v2.py#L193) | 直接覆盖 |
| 18 | Live client searches outside its selected symbols | [test_symbols_are_canonical_scoped_and_filtered_before_paging](../../../../tests/test_mcp_tool_contracts.py#L274) | 直接覆盖 |
| 19 | Writable paper key discovers all capabilities | [test_paper_catalog_counts_scopes_and_hidden_compatibility](../../../../tests/test_mcp_canonical_catalog.py#L21) | 直接覆盖 |
| 20 | Read-only paper key discovers tools | [test_paper_catalog_counts_scopes_and_hidden_compatibility](../../../../tests/test_mcp_canonical_catalog.py#L21) | 直接覆盖 |
| 21 | Account setup is repeated | [test_paper_configuration_is_explicit_and_never_resets](../../../../tests/test_mcp_canonical_catalog.py#L115) | 直接覆盖 |
| 22 | Bot polls automatic fill events | [test_http_polling_sdk_adapters_and_admin_dashboard](../../../../tests/test_mcp_paper_v2.py#L262)；[test_scan_cursor_empty_pages_and_namespace_isolation](../../../../tests/test_mcp_paper_v2.py#L147) | 直接覆盖 |
| 23 | Bot account is missing during initialization | [test_bot_provision_ranking_and_admin_funding_does_not_inflate_pnl](../../../../tests/test_mcp_paper_v2.py#L127) | 直接覆盖 |
| 24 | Existing client directly calls a hidden adapter | [test_paper_catalog_counts_scopes_and_hidden_compatibility](../../../../tests/test_mcp_canonical_catalog.py#L21)；[test_flat_and_legacy_json_calls_share_one_operation_receipt](../../../../tests/test_mcp_tool_contracts.py#L343) | 直接覆盖 |
| 25 | Old unknown operation is replayed after upgrade | [test_saved_legacy_fingerprints_receipts_and_tail_replay_are_unchanged](../../../../tests/test_unified_trading.py#L394) | 直接覆盖 |
| 26 | Paper batch has several successful steps | [test_paper_twenty_item_model_projection_has_one_summary](../../../../tests/test_paper_ordered_batch.py#L225) | 直接覆盖 |
| 27 | Confirmed close has unresolved cleanup | [test_compact_projection_keeps_unknown_identity_quantity_and_cleanup_truth](../../../../tests/test_unified_trading.py#L270)；[test_market_short_close_keeps_fill_when_cleanup_ack_has_no_fill](../../../../tests/test_trade_tool_integration.py#L67) | 直接覆盖 |
| 28 | Published input contains unsupported financial fields | [test_unknown_fields_invalid_sizing_and_quantity_fail](../../../../tests/test_unified_trading.py#L74)；[test_published_json_schema_rejects_known_unsupported_shapes](../../../../tests/test_unified_trading.py#L650) | 直接覆盖 |
| 29 | Catalog and task binding are verified together | [test_published_mode_schema_and_default_catalog](../../../../tests/test_unified_trading.py#L91)；[test_paper_catalog_counts_scopes_and_hidden_compatibility](../../../../tests/test_mcp_canonical_catalog.py#L21)；[test_live_key_does_not_receive_paper_catalog_and_cancel_only_keeps_executor](../../../../tests/test_mcp_canonical_catalog.py#L126) | 直接覆盖 |

## paper-ordered-trading（39场景）

实现对照：[paper_batch.py](../../../../backend/mcp/paper_batch.py), [paper_orders.py](../../../../backend/mcp/paper_orders.py), [paper.py](../../../../backend/mcp/paper.py).

| # | 场景 | 实际测试证据/未单独证明的边界 | 结论 |
| --- | --- | --- | --- |
| 1 | One call cancels and replaces a known paper order | [test_sdk_directory_bootstrap_cancel_first_and_read_only_recovery](../../../../tests/test_unified_surface_chain.py#L18) | 直接覆盖 |
| 2 | Client sends unsupported orchestration syntax | [test_bounded_action_array](../../../../tests/test_unified_trading.py#L58)；[test_unknown_fields_invalid_sizing_and_quantity_fail](../../../../tests/test_unified_trading.py#L74) | 直接覆盖 |
| 3 | Paper request attempts to select live execution | [test_unknown_fields_invalid_sizing_and_quantity_fail](../../../../tests/test_unified_trading.py#L74)；[test_account_facets_and_records_do_not_leak_or_append_full_account](../../../../tests/test_mcp_canonical_catalog.py#L45) | 直接覆盖 |
| 4 | Order belongs to another paper account | [test_paper_batch_namespace_and_user_order_isolation](../../../../tests/test_paper_ordered_batch.py#L140) | 直接覆盖 |
| 5 | Later action has an invalid sizing combination | [test_paper_preflight_invalid_later_action_has_no_effect_and_is_durable](../../../../tests/test_paper_ordered_batch.py#L61) | 直接覆盖 |
| 6 | Cancellation releases resources for a later entry | [test_paper_cancel_releases_reservation_before_next_action](../../../../tests/test_paper_ordered_batch.py#L52) | 直接覆盖 |
| 7 | Read-only key submits an otherwise valid batch | [test_live_paper_and_readonly_permissions](../../../../tests/test_mcp_paper.py#L158) | 直接覆盖 |
| 8 | Parent operation ID has the maximum allowed length | [test_paper_parent_ids_replay_and_changed_intent](../../../../tests/test_paper_ordered_batch.py#L37) | 直接覆盖 |
| 9 | Identical batch is retried after key rotation | [test_paper_parent_ids_replay_and_changed_intent](../../../../tests/test_paper_ordered_batch.py#L37) | 直接覆盖 |
| 10 | Retry changes action order or dependency policy | [test_paper_parent_ids_replay_and_changed_intent](../../../../tests/test_paper_ordered_batch.py#L37)；[test_conflicting_parent_intent_and_context_never_dispatch](../../../../tests/test_unified_trading.py#L190) | 组合审查 |
| 11 | Edit fails after its old reservation was tentatively removed | [test_cancel_and_edit_release_old_reservation](../../../../tests/test_mcp_paper_v2.py#L47)；[test_failed_trade_rolls_back_everything_and_operation_can_be_retried](../../../../tests/test_mcp_paper.py#L181)。旧edit失败回滚和v3逐项提交组合证据，未精准在移除预留之后注入v3 edit异常。 | 组合审查 |
| 12 | Later market trade fails after a successful cancellation | [test_paper_cancel_releases_reservation_before_next_action](../../../../tests/test_paper_ordered_batch.py#L52)；[test_paper_failed_quote_and_invalid_fractional_leverage_are_not_unknown](../../../../tests/test_paper_ordered_batch.py#L211) | 组合审查 |
| 13 | Public quote is slow while another account is active | [test_paper_quote_network_does_not_hold_write_transaction](../../../../tests/test_paper_ordered_batch.py#L127) | 直接覆盖 |
| 14 | Public market lookup fails before account mutation | [test_paper_failed_quote_and_invalid_fractional_leverage_are_not_unknown](../../../../tests/test_paper_ordered_batch.py#L211) | 直接覆盖 |
| 15 | Preflight rejects a validly identified parent request | [test_paper_preflight_invalid_later_action_has_no_effect_and_is_durable](../../../../tests/test_paper_ordered_batch.py#L61) | 直接覆盖 |
| 16 | Intent storage is unavailable | [test_sdk_intent_storage_error_keeps_original_id_and_does_not_claim_prior_failure](../../../../tests/test_paper_ordered_batch.py#L446)；[test_sqlite_failure_during_intent_insert_rolls_back_without_dispatch](../../../../tests/test_paper_ordered_batch.py#L486) | 直接覆盖 |
| 17 | Cancel fails before its dependent replacement | [test_paper_limit_acceptance_is_not_fill_or_dependency_completion](../../../../tests/test_paper_ordered_batch.py#L71)；[test_explicit_dependency_requires_completed_and_independent_continues](../../../../tests/test_unified_trading.py#L170) | 直接覆盖 |
| 18 | Dependencies are invalid | [test_dependency_indices_cannot_be_ambiguous](../../../../tests/test_unified_trading.py#L80) | 直接覆盖 |
| 19 | Independent action is affected by uncertain account state | [test_commit_evidence_failure_is_unknown_then_original_child_recovers](../../../../tests/test_paper_ordered_batch.py#L285)。不可读commit的unknown与不续跑直接覆盖；independent后项重新读取权威账本的准入按生产提交分支核对。 | 组合审查 |
| 20 | Worker crashes after a child commit before returning the parent summary | [test_paper_crash_after_child_commit_never_repeats_or_runs_tail](../../../../tests/test_paper_ordered_batch.py#L85) | 直接覆盖 |
| 21 | Worker crashes after parent reservation but before the first action | [test_paper_crash_after_parent_registration_never_starts_tail](../../../../tests/test_paper_ordered_batch.py#L101) | 直接覆盖 |
| 22 | Retried terminal parent contains skipped tail items | [test_paper_crash_after_child_commit_never_repeats_or_runs_tail](../../../../tests/test_paper_ordered_batch.py#L85) | 直接覆盖 |
| 23 | Two clients submit the same parent simultaneously | [test_paper_same_parent_concurrent_retries_have_one_fill](../../../../tests/test_paper_ordered_batch.py#L117)；[test_cross_process_retry_tracks_physical_worker_and_has_one_fill](../../../../tests/test_paper_ordered_batch.py#L335) | 直接覆盖 |
| 24 | Matcher fills an order before a queued edit commits | [test_concurrent_matchers_do_not_double_fill](../../../../tests/test_mcp_paper_v2.py#L251)；[test_cancel_and_edit_release_old_reservation](../../../../tests/test_mcp_paper_v2.py#L47)。matcher去重与edit updated_at CAS分别覆盖；没有线程屏障精准构造v3 edit报价后被matcher成交。 | 组合审查 |
| 25 | Cancel targets an already terminal order | [test_cancel_and_edit_release_old_reservation](../../../../tests/test_mcp_paper_v2.py#L47)。生产cancel保留非open原终态；引用回归未单独构造v3已filled终态的取消。 | 组合审查 |
| 26 | Budget expires after an earlier committed action | [test_paper_expired_budget_preserves_committed_prefix_and_final_query](../../../../tests/test_paper_ordered_batch.py#L191) | 直接覆盖 |
| 27 | HTTP connection ends while the worker is still active | [test_active_worker_queries_are_pending_and_do_not_duplicate_submission](../../../../tests/test_paper_ordered_batch.py#L313) | 直接覆盖 |
| 28 | Commit acknowledgement is unavailable | [test_commit_evidence_failure_is_unknown_then_original_child_recovers](../../../../tests/test_paper_ordered_batch.py#L285) | 直接覆盖 |
| 29 | Spot buy uses a quote budget | [test_budget_units_margin_vs_notional_and_atomic_close_all](../../../../tests/test_mcp_paper_v2.py#L114) | 直接覆盖 |
| 30 | Perpetual entry uses margin sizing | [test_budget_units_margin_vs_notional_and_atomic_close_all](../../../../tests/test_mcp_paper_v2.py#L114) | 直接覆盖 |
| 31 | Same instrument is available on several venues | [test_multi_venue_exit_requires_explicit_target_and_replays_without_financial_effect](../../../../tests/test_paper_ordered_batch.py#L382)；[test_single_nondefault_venue_exit_resolves_the_existing_holding](../../../../tests/test_paper_ordered_batch.py#L413) | 直接覆盖 |
| 32 | Limit entry is followed by immediate position protection | [test_limit_entry_attaches_protection_only_after_fill](../../../../tests/test_mcp_paper_v2.py#L101)；[test_paper_limit_acceptance_is_not_fill_or_dependency_completion](../../../../tests/test_paper_ordered_batch.py#L71) | 直接覆盖 |
| 33 | One OCO protection leg fills | [test_oco_protection_closes_correct_side_once](../../../../tests/test_mcp_paper_v2.py#L63) | 直接覆盖 |
| 34 | A single protection leg is set or updated | [test_paper_protection_and_cycle_reopen_remain_independent](../../../../tests/test_paper_ordered_batch.py#L151)。单腿v3和双腿OCO分别覆盖；省略已有TP保留经合并分支核对，没有独立组合调用断言。 | 组合审查 |
| 35 | Close-all ends a cycle before the side is reopened | [test_paper_protection_and_cycle_reopen_remain_independent](../../../../tests/test_paper_ordered_batch.py#L151)；[test_old_protection_cannot_close_reopened_position](../../../../tests/test_mcp_paper_v2.py#L78) | 直接覆盖 |
| 36 | Several paper steps complete in one call | [test_paper_twenty_item_model_projection_has_one_summary](../../../../tests/test_paper_ordered_batch.py#L225) | 直接覆盖 |
| 37 | Client queries a parent after losing its response | [test_paper_sdk_batch_and_recovery_compact_vs_detail](../../../../tests/test_mcp_canonical_catalog.py#L137) | 直接覆盖 |
| 38 | Old paper operation is retried through its original adapter | [test_legacy_paper_fingerprints_and_partial_cancel_receipts](../../../../tests/test_paper_ordered_batch.py#L164) | 直接覆盖 |
| 39 | User changes a preference or borrows funds | [test_paper_configuration_is_explicit_and_never_resets](../../../../tests/test_mcp_canonical_catalog.py#L115)；[test_bank_net_equity_limits_and_receipts](../../../../tests/test_mcp_paper.py#L60) | 直接覆盖 |

## spot-inventory-reconciliation（16场景）

实现对照：[spot_management.py](../../../../backend/utils/spot_management.py), [spot_orders.py](../../../../backend/mcp/spot_orders.py).

| # | 场景 | 实际测试证据/未单独证明的边界 | 结论 |
| --- | --- | --- | --- |
| 1 | Pre-existing balance does not block inventory query | [test_mcp_spot_inventory_tolerates_preexisting_balance_and_bnb_fees](../../../../tests/test_mcp_tool_contracts.py#L498)；[test_spot_baseline_origin_and_oco_crossquote_reservation](../../../../tests/test_visible_position_management.py#L524) | 直接覆盖 |
| 2 | Sell amount is bounded by safe inventory | [test_coexisting_manual_holdings_tolerated_and_bounded](../../../../tests/test_mcp_spot_exits.py#L186)；[test_spot_baseline_origin_and_oco_crossquote_reservation](../../../../tests/test_visible_position_management.py#L524) | 组合审查 |
| 3 | First sale manages visible pre-existing balance | [test_spot_baseline_origin_and_oco_crossquote_reservation](../../../../tests/test_visible_position_management.py#L524)；[test_spot_transfer_recovers_and_cancels_original_native_exit_without_source_config](../../../../tests/test_visible_position_management.py#L321)。真实基线和退出adapter分开覆盖；未在一个测试串中证明未经预claim的底仓首笔v3 close。 | 组合审查 |
| 4 | Competing profile sees the same inventory | [test_spot_baseline_origin_and_oco_crossquote_reservation](../../../../tests/test_visible_position_management.py#L524) | 直接覆盖 |
| 5 | Structured breakdown returned on variance | [test_visible_spot_historical_fees_and_external_fills_remain_separate](../../../../tests/test_visible_position_management.py#L349) | 直接覆盖 |
| 6 | Historical attribution differs from current managed quantity | [test_visible_spot_historical_fees_and_external_fills_remain_separate](../../../../tests/test_visible_position_management.py#L349) | 直接覆盖 |
| 7 | Incomplete evidence preserves known inventory facts | [test_okx_live_algo_without_execution_size_keeps_reservation](../../../../tests/test_mcp_spot_exits.py#L414)；[test_spot_baseline_origin_and_oco_crossquote_reservation](../../../../tests/test_visible_position_management.py#L524) | 组合审查 |
| 8 | Cross-quote requests cannot sell the same inventory twice | [test_spot_baseline_origin_and_oco_crossquote_reservation](../../../../tests/test_visible_position_management.py#L524)；[test_cross_quote_foreign_profile_evidence_blocks_even_equal_balance](../../../../tests/test_mcp_spot_exits.py#L203) | 直接覆盖 |
| 9 | Cross-process profiles compete for one asset | [test_cross_process_claim_only_one_wins](../../../../tests/test_visible_position_management.py#L547)；[test_spot_baseline_origin_and_oco_crossquote_reservation](../../../../tests/test_visible_position_management.py#L524)。跨进程原子认领和跨报价共键分别覆盖，未组合成同一个跨进程跨报价场景。 | 组合审查 |
| 10 | Market types and simulated namespaces do not share stock | [test_physical_labels_are_venue_scoped_and_binance_spot_swap_share_identity](../../../../tests/test_visible_position_management.py#L121)；[test_paper_batch_namespace_and_user_order_isolation](../../../../tests/test_paper_ordered_batch.py#L140) | 直接覆盖 |
| 11 | Native OCO reserves its quantity once | [test_binance_oco_is_one_native_group_and_one_inventory_reservation](../../../../tests/test_mcp_spot_exits.py#L313)；[test_spot_baseline_origin_and_oco_crossquote_reservation](../../../../tests/test_visible_position_management.py#L524) | 直接覆盖 |
| 12 | Cancel first makes verified inventory available to the next action | [test_partial_exit_and_cancel_release_only_unfilled_owned_amount](../../../../tests/test_mcp_spot_exits.py#L230)；[test_spot_cancel_first_is_not_blocked_by_later_zero_allowance](../../../../tests/test_unified_trading.py#L685) | 组合审查 |
| 13 | Uncertain cancellation cannot release stock early | [test_regular_spot_cancel_keeps_inventory_reserved_until_fills_are_verified](../../../../tests/test_visible_position_management.py#L366)；[test_okx_terminal_algo_needs_explicit_zero_execution_to_release](../../../../tests/test_mcp_spot_exits.py#L395) | 直接覆盖 |
| 14 | Another process reserves after a displayed snapshot | [test_reconciled_fill_between_read_and_reservation_does_not_restore_capacity](../../../../tests/test_mcp_spot_exits.py#L285) | 直接覆盖 |
| 15 | Manual removal reduces a requested sale | [test_precision_cannot_zero_inflate_or_corrupt_reserved_quantity](../../../../tests/test_mcp_spot_exits.py#L270)；[test_coexisting_manual_holdings_tolerated_and_bounded](../../../../tests/test_mcp_spot_exits.py#L186) | 组合审查 |
| 16 | Human modifies an enrolled spot order | [test_owned_exit_cannot_be_manually_enlarged_without_detection](../../../../tests/test_mcp_spot_exits.py#L277)；[test_price_only_edit_uses_latest_manual_quantity_and_partial_fills](../../../../tests/test_exclusive_position_management.py#L235) | 组合审查 |

## trading-tool-contracts（76场景）

实现对照：[trading_contracts.py](../../../../backend/agent/trading_contracts.py), [unified_trading.py](../../../../backend/agent/unified_trading.py), [unified_dispatch.py](../../../../backend/agent/unified_dispatch.py).

| # | 场景 | 实际测试证据/未单独证明的边界 | 结论 |
| --- | --- | --- | --- |
| 1 | Repeated entry calls | [test_one_call_multi_exit_and_replay](../../../../tests/test_trade_tool_integration.py#L161) | 直接覆盖 |
| 2 | Context cannot be overridden | [test_unknown_fields_invalid_sizing_and_quantity_fail](../../../../tests/test_unified_trading.py#L74)；[test_gateway_identity_conflicts_fail_before_any_reservation](../../../../tests/test_mcp_tool_contracts.py#L377) | 直接覆盖 |
| 3 | Default writer contains a single action | [test_published_mode_schema_and_default_catalog](../../../../tests/test_unified_trading.py#L91) | 直接覆盖 |
| 4 | Partially filled entry modification | [test_v3_independent_remaining_edit_uses_latest_partial_fill](../../../../tests/test_unified_trading.py#L624)；[test_native_edit_remaining_quantity_accounts_for_racing_fills](../../../../tests/test_independent_real_v2.py#L202) | 直接覆盖 |
| 5 | Conditional replacement | [test_conditional_replacement_conflict_does_not_resize_or_block_cycle](../../../../tests/test_independent_real_v2.py#L473) | 直接覆盖 |
| 6 | Dynamic all price edit | [test_all_edit_retains_dynamic_quantity_and_timeout_recovers](../../../../tests/test_independent_real_v2.py#L252) | 直接覆盖 |
| 7 | Fields are valid for the current phase | [test_algo_and_regular_id_collision_and_parent_child_edit_cancel](../../../../tests/test_independent_real_v2.py#L215) | 直接覆盖 |
| 8 | Omitted field preserves a verified manual change | [test_price_only_edit_uses_latest_manual_quantity_and_partial_fills](../../../../tests/test_exclusive_position_management.py#L235) | 直接覆盖 |
| 9 | Entry cannot be disguised as an exit edit | [test_canonical_edit_guards_classify_the_owned_target](../../../../tests/test_mcp_gateway.py#L180) | 直接覆盖 |
| 10 | Authorized edit uses cancel and replace | [test_edit_role_is_owned_not_supplied_and_replay_needs_no_live_target](../../../../tests/test_mcp_tool_contracts.py#L146) | 直接覆盖 |
| 11 | External creation is not a separate edit denial | [test_price_only_edit_uses_latest_manual_quantity_and_partial_fills](../../../../tests/test_exclusive_position_management.py#L235) | 直接覆盖 |
| 12 | Cancel conditional exit | [test_algo_and_regular_id_collision_and_parent_child_edit_cancel](../../../../tests/test_independent_real_v2.py#L215) | 直接覆盖 |
| 13 | Trigger precedes cancellation | [test_cancel_parent_follows_trigger_race_but_edit_does_not](../../../../tests/test_independent_real_v2.py#L236) | 直接覆盖 |
| 14 | Cancel a human-created order in an exclusive scope | [test_same_numeric_regular_and_algo_id_remain_distinct](../../../../tests/test_exclusive_position_management.py#L258)；[test_mcp_cancel_scope_and_principal_isolation_survive_enrollment](../../../../tests/test_exclusive_position_management.py#L540)。旧显式范围的外部单核验撤单与权限分别覆盖；不声称一个测试同时证明原调用首次纳管的全部分支。 | 组合审查 |
| 15 | First financial use can cancel a manual order | [test_mcp_cancel_scope_and_principal_isolation_survive_enrollment](../../../../tests/test_exclusive_position_management.py#L540) | 组合审查 |
| 16 | Visibility does not defeat an existing manager | [test_query_does_not_claim_or_mutate_and_reports_competition](../../../../tests/test_visible_position_management.py#L66) | 直接覆盖 |
| 17 | Entry is still unfilled | [test_canonical_batch_cannot_exit_unfilled_entry](../../../../tests/test_trade_tool_integration.py#L234) | 直接覆盖 |
| 18 | Cancellation permissions in a mixed batch | [test_new_tools_cannot_bypass_profile_mode_or_cancel_scope](../../../../tests/test_mcp_tool_contracts.py#L127) | 直接覆盖 |
| 19 | Paper cancellation retains its existing financial permission | [test_live_paper_and_readonly_permissions](../../../../tests/test_mcp_paper.py#L158) | 直接覆盖 |
| 20 | Flat legacy cancellation is not a version signal | [test_saved_legacy_fingerprints_receipts_and_tail_replay_are_unchanged](../../../../tests/test_unified_trading.py#L394) | 直接覆盖 |
| 21 | Cleanup completes before the next entry | [test_verified_finished_cleanup_opens_new_cycle_without_scheduler_or_query](../../../../tests/test_independent_admission.py#L217) | 直接覆盖 |
| 22 | Old close-all order can affect a new entry | [test_close_cleanup_recovery_then_explicit_new_entry_once](../../../../tests/test_independent_receipt_recovery.py#L135) | 直接覆盖 |
| 23 | A rejected take profit does not suppress independent protection | [test_independent_batch_continues_and_replay_never_sends_again](../../../../tests/test_scoped_trade_rounds.py#L78) | 直接覆盖 |
| 24 | Explicit dependency preserves cancel-then-create intent | [test_explicit_dependency_requires_completed_and_independent_continues](../../../../tests/test_unified_trading.py#L170) | 直接覆盖 |
| 25 | Invalid policy or dependencies fail before any writes | [test_dependency_indices_cannot_be_ambiguous](../../../../tests/test_unified_trading.py#L80)；[test_whole_batch_static_failure_has_zero_dispatch_and_claim](../../../../tests/test_unified_trading.py#L150) | 直接覆盖 |
| 26 | Unknown execution still blocks dependent actions in independent mode | [test_independent_batch_unknown_only_blocks_dependent_writes](../../../../tests/test_scoped_trade_rounds.py#L90) | 直接覆盖 |
| 27 | Replay never resumes skipped tail | [test_worker_crash_recovers_attempted_items_without_tail_replay](../../../../tests/test_unified_trading.py#L213) | 直接覆盖 |
| 28 | Third-version mixed actions retain caller order | [test_attached_strategy_open_edit_protect_cancel_replay_is_atomic](../../../../tests/test_unified_trading.py#L286) | 直接覆盖 |
| 29 | Historical limit exit remains a limit | [test_saved_legacy_fingerprints_receipts_and_tail_replay_are_unchanged](../../../../tests/test_unified_trading.py#L394) | 直接覆盖 |
| 30 | New take profit limit does not fall back | [test_unknown_fields_invalid_sizing_and_quantity_fail](../../../../tests/test_unified_trading.py#L74) | 直接覆盖 |
| 31 | Persisted interpretation survives restart | [test_upgrade_preserves_ids_episode_fill_indexes_receipts_and_legacy_meaning](../../../../tests/test_independent_exit_migration.py#L44)；[test_legacy_zero_snapshot_does_not_become_dynamic_on_replay](../../../../tests/test_independent_real_v2.py#L287) | 直接覆盖 |
| 32 | Hidden legacy dispatcher is not a new batch identity | [test_saved_legacy_fingerprints_receipts_and_tail_replay_are_unchanged](../../../../tests/test_unified_trading.py#L394) | 直接覆盖 |
| 33 | Example and schema agreement | [test_workflow_json_v3_examples_use_actual_capability_validator](../../../../tests/test_unified_trading.py#L381) | 直接覆盖 |
| 34 | Attached and spot capabilities are described separately | [test_published_mode_schema_and_default_catalog](../../../../tests/test_unified_trading.py#L91) | 直接覆盖 |
| 35 | Automatic management does not ask for adoption first | [test_published_mode_schema_and_default_catalog](../../../../tests/test_unified_trading.py#L91)；[test_manual_resource_can_be_claimed_without_cycle_or_false_history](../../../../tests/test_visible_management_projection.py#L21) | 直接覆盖 |
| 36 | Protection survives an unrelated definite failure | [test_same_round_rejection_does_not_block_valid_independent_action](../../../../tests/test_scoped_trade_rounds.py#L30) | 直接覆盖 |
| 37 | Failure does not silently reverse a sequential intention | [test_independent_batch_unknown_only_blocks_dependent_writes](../../../../tests/test_scoped_trade_rounds.py#L90)；[test_explicit_dependency_requires_completed_and_independent_continues](../../../../tests/test_unified_trading.py#L170) | 直接覆盖 |
| 38 | Definite failure does not imply a hidden sequence dependency | [test_same_round_rejection_does_not_block_valid_independent_action](../../../../tests/test_scoped_trade_rounds.py#L30) | 直接覆盖 |
| 39 | User rejection is not an exchange failure | [test_chat_user_rejection_still_stops_writes_but_allows_read](../../../../tests/test_scoped_trade_rounds.py#L50) | 直接覆盖 |
| 40 | Unsupported tool effect cannot be assumed independent | [test_scheduler_and_chat_stop_multi_tool_calls_after_failed_or_uncertain_result](../../../../tests/test_decision_memory_context.py#L122) | 组合审查 |
| 41 | Batch approval retains action granularity | [test_bound_chat_v3_approves_each_action_and_reject_stops_tail_but_query_available](../../../../tests/test_unified_trading.py#L364) | 直接覆盖 |
| 42 | Live key cannot select the paper engine | [test_unknown_fields_invalid_sizing_and_quantity_fail](../../../../tests/test_unified_trading.py#L74)；[test_gateway_identity_conflicts_fail_before_any_reservation](../../../../tests/test_mcp_tool_contracts.py#L377) | 直接覆盖 |
| 43 | Paper key remains isolated | [test_paper_batch_namespace_and_user_order_isolation](../../../../tests/test_paper_ordered_batch.py#L140) | 直接覆盖 |
| 44 | Multi-symbol spot preserves one task budget | [test_actual_spot_v3_multi_buy_shares_one_parent_allowance](../../../../tests/test_unified_trading.py#L662) | 直接覆盖 |
| 45 | A batch cannot span different managers | [test_unknown_fields_invalid_sizing_and_quantity_fail](../../../../tests/test_unified_trading.py#L74)；[test_conflicting_parent_intent_and_context_never_dispatch](../../../../tests/test_unified_trading.py#L190) | 直接覆盖 |
| 46 | Live opening remains limit-only | [test_paper_market_capability_does_not_expand_live](../../../../tests/test_unified_trading.py#L109) | 直接覆盖 |
| 47 | Paper opening accepts its existing market capability | [test_paper_parent_ids_replay_and_changed_intent](../../../../tests/test_paper_ordered_batch.py#L37) | 直接覆盖 |
| 48 | Spot direction has no short-position interpretation | [test_known_paper_capabilities_are_static](../../../../tests/test_unified_trading.py#L122)；[test_published_mode_schema_and_default_catalog](../../../../tests/test_unified_trading.py#L91) | 直接覆盖 |
| 49 | Attached entry keeps its configured protection policy | [test_published_json_schema_rejects_known_unsupported_shapes](../../../../tests/test_unified_trading.py#L650) | 直接覆盖 |
| 50 | Fixed and all quantities retain their modes | [test_conditional_all_edit_replaces_identity_and_preserves_dynamic_quantity](../../../../tests/test_independent_mock_v2.py#L183)；[test_fixed_exit_never_silently_shrinks_after_manual_reduction](../../../../tests/test_exclusive_position_management.py#L662) | 直接覆盖 |
| 51 | Spot native OCO preserves one reservation | [test_binance_oco_is_one_native_group_and_one_inventory_reservation](../../../../tests/test_mcp_spot_exits.py#L313) | 直接覆盖 |
| 52 | Paper paired protection uses its own order identities | [test_oco_protection_closes_correct_side_once](../../../../tests/test_mcp_paper_v2.py#L63) | 直接覆盖 |
| 53 | Independent live protection is represented as close | [test_published_mode_schema_and_default_catalog](../../../../tests/test_unified_trading.py#L91) | 直接覆盖 |
| 54 | Paper changes one protection leg while keeping the other | [test_paper_protection_and_cycle_reopen_remain_independent](../../../../tests/test_paper_ordered_batch.py#L151)。单腿v3、原双腿OCO分别覆盖；省略已有TP保留经add_protection合并分支核对，没有独立组合调用断言。 | 组合审查 |
| 55 | Paper entry carries optional protection | [test_limit_entry_attaches_protection_only_after_fill](../../../../tests/test_mcp_paper_v2.py#L101) | 直接覆盖 |
| 56 | Cancel releases resources before opening admission | [test_cancel_first_dynamic_rejection_keeps_completed_prefix](../../../../tests/test_unified_trading.py#L138) | 直接覆盖 |
| 57 | Later permission failure rejects every item | [test_new_tools_cannot_bypass_profile_mode_or_cancel_scope](../../../../tests/test_mcp_tool_contracts.py#L127)。旧gateway权限拒绝与v3预检分开覆盖；未专门在完整SDK串测试v3 cancel-first缺trade零撤单。 | 组合审查 |
| 58 | Invalid later static combination causes no early write | [test_whole_batch_static_failure_has_zero_dispatch_and_claim](../../../../tests/test_unified_trading.py#L150) | 直接覆盖 |
| 59 | Pure cancellation survives an opening gate | [test_current_step_entry_gate_precedes_claim_and_does_not_block_prior_cancel](../../../../tests/test_unified_trading.py#L720) | 直接覆盖 |
| 60 | Visible manual position is managed in the same action | [test_visible_direct_request_manages_only_its_hedge_target](../../../../tests/test_visible_position_management.py#L88) | 直接覆盖 |
| 61 | Read-only discovery does not win ownership | [test_query_does_not_claim_or_mutate_and_reports_competition](../../../../tests/test_visible_position_management.py#L66) | 直接覆盖 |
| 62 | Same profile principals share management but not private receipts | [test_mcp_cancel_scope_and_principal_isolation_survive_enrollment](../../../../tests/test_exclusive_position_management.py#L540) | 直接覆盖 |
| 63 | Third-version replay preserves partial results | [test_default_error_stop_and_uncertain_replay](../../../../tests/test_unified_trading.py#L160) | 直接覆盖 |
| 64 | A version change cannot reuse an old operation | [test_conflicting_parent_intent_and_context_never_dispatch](../../../../tests/test_unified_trading.py#L190)。request含版本且run_once拒绝变更意图，未专门构造v2同ID改v3。 | 组合审查 |
| 65 | Legacy omitted policy retains its exact identity | [test_saved_legacy_fingerprints_receipts_and_tail_replay_are_unchanged](../../../../tests/test_unified_trading.py#L394) | 直接覆盖 |
| 66 | A failed second item does not erase the first | [test_cancel_first_dynamic_rejection_keeps_completed_prefix](../../../../tests/test_unified_trading.py#L138) | 直接覆盖 |
| 67 | Submitted entry does not satisfy a close dependency | [test_explicit_dependency_requires_completed_and_independent_continues](../../../../tests/test_unified_trading.py#L170) | 直接覆盖 |
| 68 | A dynamic result reference is rejected before trading | [test_unknown_fields_invalid_sizing_and_quantity_fail](../../../../tests/test_unified_trading.py#L74) | 直接覆盖 |
| 69 | Recovery does not issue an unattempted tail | [test_worker_crash_recovers_attempted_items_without_tail_replay](../../../../tests/test_unified_trading.py#L213) | 直接覆盖 |
| 70 | Compact timeout retains a recovery target | [test_compact_projection_keeps_unknown_identity_quantity_and_cleanup_truth](../../../../tests/test_unified_trading.py#L270) | 直接覆盖 |
| 71 | Compact edit keeps replacement outcomes separate | [test_failed_replacement_does_not_recover_as_successful_cancel](../../../../tests/test_independent_receipt_recovery.py#L442) | 直接覆盖 |
| 72 | Full evidence remains accessible after projection | [test_paper_sdk_batch_and_recovery_compact_vs_detail](../../../../tests/test_mcp_canonical_catalog.py#L137) | 直接覆盖 |
| 73 | Three actions produce at most one account summary | [test_account_summary_failure_preserves_financial_status_and_is_single](../../../../tests/test_unified_trading.py#L259) | 直接覆盖 |
| 74 | Summary read failure does not rewrite a completed trade | [test_account_summary_failure_preserves_financial_status_and_is_single](../../../../tests/test_unified_trading.py#L259) | 直接覆盖 |
| 75 | The receiver inspects an accepted unresolved operation | [test_canonical_handoff_recovery_is_scoped_and_read_only](../../../../tests/test_mcp_canonical_catalog.py#L194)；[test_swap_handoff_query_uses_receiver_account_after_source_configuration_retirement](../../../../tests/test_visible_position_management.py#L208) | 直接覆盖 |
| 76 | A former owner retries after ownership changes | [test_transfer_keeps_original_identity_and_recovery_is_narrow](../../../../tests/test_visible_position_management.py#L190) | 直接覆盖 |

## visible-position-management（33场景）

实现对照：[visible_management.py](../../../../backend/utils/visible_management.py), [position_management.py](../../../../backend/utils/position_management.py), [scheduler.py](../../../../backend/app/core/scheduler.py).

| # | 场景 | 实际测试证据/未单独证明的边界 | 结论 |
| --- | --- | --- | --- |
| 1 | Manage a visible manual position directly | [test_visible_direct_request_manages_only_its_hedge_target](../../../../tests/test_visible_position_management.py#L88)；[test_startup_baseline_and_three_external_orders_are_readonly_and_deduplicated](../../../../tests/test_exclusive_position_management.py#L183) | 直接覆盖 |
| 2 | Account and symbol boundaries cannot be overridden | [test_trusted_symbol_boundary_and_unique_default](../../../../tests/test_unified_trading.py#L127)；[test_readonly_disabled_and_outside_symbol_cannot_claim](../../../../tests/test_visible_position_management.py#L133) | 直接覆盖 |
| 3 | Concurrent processes discover the same position | [test_cross_process_claim_only_one_wins](../../../../tests/test_visible_position_management.py#L547) | 直接覆盖 |
| 4 | A conflicting side does not block an independent scope | [test_hedge_unused_direction_is_not_preclaimed_but_oneway_shares_both](../../../../tests/test_visible_position_management.py#L79) | 直接覆盖 |
| 5 | Invalid batch cannot reserve a management scope | [test_whole_batch_static_failure_has_zero_dispatch_and_claim](../../../../tests/test_unified_trading.py#L150) | 直接覆盖 |
| 6 | Selected task discovers both hedge directions | [test_visible_direct_request_manages_only_its_hedge_target](../../../../tests/test_visible_position_management.py#L88)；[test_hedge_direction_can_have_separate_manager_but_oneway_cannot](../../../../tests/test_exclusive_position_management.py#L296)。Hedge协调和单侧管理分开验证；后台同时发现双方向采用生产分支组合审查。 | 组合审查 |
| 7 | One-way opposite requests share ownership | [test_hedge_unused_direction_is_not_preclaimed_but_oneway_shares_both](../../../../tests/test_visible_position_management.py#L79) | 直接覆盖 |
| 8 | Existing order is discovered outside a preset list | [test_disabled_unrestricted_profiles_keep_all_owned_perpetual_symbols_for_maintenance](../../../../tests/test_mcp_symbol_scope.py#L201)；[test_startup_baseline_and_three_external_orders_are_readonly_and_deduplicated](../../../../tests/test_exclusive_position_management.py#L183)。all动态维护配置与真实发现纳管分开验证；后台只认领实际观察资源。 | 组合审查 |
| 9 | First authorized entry has no existing exposure | [test_all_scope_live_v3_explicit_symbol_and_private_child_recovery](../../../../tests/test_mcp_canonical_catalog.py#L165) | 组合审查 |
| 10 | Searching markets does not reserve them | [test_sdk_directory_bootstrap_cancel_first_and_read_only_recovery](../../../../tests/test_unified_surface_chain.py#L18)；[test_query_does_not_claim_or_mutate_and_reports_competition](../../../../tests/test_visible_position_management.py#L66) | 直接覆盖 |
| 11 | Bound chat manages its task order | [test_bound_chat_v3_approves_each_action_and_reject_stops_tail_but_query_available](../../../../tests/test_unified_trading.py#L364) | 直接覆盖 |
| 12 | Read-only principal shares a writable profile | [test_new_tools_cannot_bypass_profile_mode_or_cancel_scope](../../../../tests/test_mcp_tool_contracts.py#L127)；[test_readonly_disabled_and_outside_symbol_cannot_claim](../../../../tests/test_visible_position_management.py#L133) | 组合审查 |
| 13 | Profiles use aliases of the same credentials | [test_same_credential_cannot_bypass_owner_with_a_conflicting_account_alias](../../../../tests/test_visible_position_management.py#L113) | 直接覆盖 |
| 14 | Different keys use one saved physical account | [test_same_physical_account_multiple_keys_conflict](../../../../tests/test_visible_position_management.py#L103) | 直接覆盖 |
| 15 | Query an unclaimed external order | [test_agent_and_bound_chat_queries_project_resources_without_enrollment](../../../../tests/test_exclusive_position_management.py#L765)；[test_query_does_not_claim_or_mutate_and_reports_competition](../../../../tests/test_visible_position_management.py#L66) | 直接覆盖 |
| 16 | Disabled configuration appears in capability discovery | [test_readonly_disabled_and_outside_symbol_cannot_claim](../../../../tests/test_visible_position_management.py#L133) | 直接覆盖 |
| 17 | Existing position has protective and entry orders | [test_startup_baseline_and_three_external_orders_are_readonly_and_deduplicated](../../../../tests/test_exclusive_position_management.py#L183) | 直接覆盖 |
| 18 | Lost acknowledgement becomes visible | [test_lost_ack_is_matched_to_original_agent_order_not_second_external_record](../../../../tests/test_exclusive_position_management.py#L499) | 直接覆盖 |
| 19 | Human increases and decreases back to the same amount | [test_paginated_external_fills_are_complete_deduplicated_and_checkpointed](../../../../tests/test_exclusive_position_management.py#L419)；[test_verified_external_changes_keep_exit_management_available](../../../../tests/test_exclusive_position_management.py#L204) | 直接覆盖 |
| 20 | A human changes only order quantity | [test_price_only_edit_uses_latest_manual_quantity_and_partial_fills](../../../../tests/test_exclusive_position_management.py#L235) | 直接覆盖 |
| 21 | Partial history does not become a synthetic fill | [test_fill_failure_keeps_successful_checkpoint_and_can_resume](../../../../tests/test_exclusive_position_management.py#L447)；[test_unexplained_position_difference_keeps_fact_pending_without_fake_fill](../../../../tests/test_exclusive_position_management.py#L284) | 直接覆盖 |
| 22 | Human reduces quantity before a fixed close | [test_fixed_exit_never_silently_shrinks_after_manual_reduction](../../../../tests/test_exclusive_position_management.py#L662) | 直接覆盖 |
| 23 | Unsupported external order remains visible | [test_unknown_hedge_direction_stays_unsupported_instead_of_guessed](../../../../tests/test_exclusive_position_management.py#L466)；[test_unenrolled_exchange_order_keeps_capability_separate_from_management](../../../../tests/test_exclusive_position_management.py#L743) | 直接覆盖 |
| 24 | A human flattens and later reopens | [test_verified_external_flatten_reopen_creates_successor_boundary](../../../../tests/test_exclusive_position_management.py#L401)；[test_authority_survives_flat_pause_and_explicit_release](../../../../tests/test_visible_position_management.py#L149) | 直接覆盖 |
| 25 | Decisions pause while the process restarts | [test_authority_survives_flat_pause_and_explicit_release](../../../../tests/test_visible_position_management.py#L149)；[test_repeated_schema_initialization_preserves_unknown_parent_and_child](../../../../tests/test_trading_runtime_compatibility.py#L12) | 直接覆盖 |
| 26 | Delete a task with an unknown submission | [test_explicit_task_delete_checks_before_purging_original_operation](../../../../tests/test_exclusive_config_lifecycle.py#L109)；[test_pending_actions_reject_scope_retirement_without_plans_and_keep_database](../../../../tests/test_exclusive_config_lifecycle.py#L81) | 直接覆盖 |
| 27 | Transfer a live scope to another authorized task | [test_transfer_keeps_original_identity_and_recovery_is_narrow](../../../../tests/test_visible_position_management.py#L190)；[test_swap_handoff_query_uses_receiver_account_after_source_configuration_retirement](../../../../tests/test_visible_position_management.py#L208) | 直接覆盖 |
| 28 | An old worker is still making a financial submission | [test_visible_ack_does_not_release_live_process_but_crash_can_recover_proof](../../../../tests/test_management_recovery.py#L283)；[test_transfer_keeps_original_identity_and_recovery_is_narrow](../../../../tests/test_visible_position_management.py#L190)。running/file-lock/version-fence组合证明；manager收尾修订后需补跑定向检查。 | 组合审查 |
| 29 | Existing owner migrates before a new configuration discovers exposure | [test_legacy_migration_only_retires_never_bound_reservations](../../../../tests/test_visible_position_management.py#L162) | 直接覆盖 |
| 30 | Legacy explicit baseline survives the default change | [test_upgrade_preserves_ids_episode_fill_indexes_receipts_and_legacy_meaning](../../../../tests/test_independent_exit_migration.py#L44)；[test_legacy_migration_only_retires_never_bound_reservations](../../../../tests/test_visible_position_management.py#L162)。显式baseline、旧plan迁移和重复启动组合覆盖，未单一构造strict adopt+unknown再升级。 | 组合审查 |
| 31 | Completed history does not claim a new current resource | [test_legacy_migration_only_retires_never_bound_reservations](../../../../tests/test_visible_position_management.py#L162)。生产_existing_owner排除无scope的DONE历史；测试主要区分已绑定认领和未绑定reservation，未单独构造纯历史+新资源。 | 组合审查 |
| 32 | Two spot pairs share the same base asset | [test_spot_baseline_origin_and_oco_crossquote_reservation](../../../../tests/test_visible_position_management.py#L524) | 直接覆盖 |
| 33 | The same symbol exists in two paper namespaces | [test_paper_batch_namespace_and_user_order_isolation](../../../../tests/test_paper_ordered_batch.py#L140) | 直接覆盖 |

## 关键断言摘录

来自实际测试源码（每条最多240字符，完整断言见链接），仅说明证据本身，不代替组合或缺项说明。

- [test_missing_cycle_with_confirmed_rejection_preserves_history](../../../../tests/test_independent_admission.py#L132)：`assert diagnosis['restorable']`；`assert candidate['episode_id'] == plan['episode_id']`；`assert candidate['exits'][0]['operation_id'] == 'rejected-stop'`

- [test_owned_limit_price_edit_ignores_unrelated_unknown_order](../../../../tests/test_independent_admission.py#L23)：`assert result['status'] == 'completed'`；`assert result['admission']['allowed']`；`assert exchange.orders[target['id']]['price'] == 85`

- [test_target_cancel_ignores_unrelated_missing_position](../../../../tests/test_independent_admission.py#L83)：`assert assess_admission(plan, 'cancel', hedged=True, target=target)['allowed']`；`assert not assess_admission(plan, 'close', hedged=True)['allowed']`；`assert continuation_for(plan)['assessment_state'] == 'needs_refresh'`

- [test_verified_finished_cleanup_opens_new_cycle_without_scheduler_or_query](../../../../tests/test_independent_admission.py#L217)：`assert result['status'] == 'submitted' and result['episode_id'] != closed['episode_id']`；`assert len(exchange.calls) == 3`

- [test_account_facets_and_records_do_not_leak_or_append_full_account](../../../../tests/test_mcp_canonical_catalog.py#L45)：`assert 'balances' not in view and 'orders' not in view`；`assert view['positions'] == []`

- [test_all_scope_live_v3_explicit_symbol_and_private_child_recovery](../../../../tests/test_mcp_canonical_catalog.py#L165)：`assert asyncio.run(server._tool_manager.call_tool('execute_trade_actions', request))['status'] == 'completed'`；`assert len(effects) == 1`；`assert denied['state'] == 'not_found'`

- [test_canonical_handoff_recovery_is_scoped_and_read_only](../../../../tests/test_mcp_canonical_catalog.py#L194)：`assert service.operation(caller, 'swap', 'unrelated-private-op')['state'] == 'not_found'`；`assert calls == [('mcp:swap', recovery_id)]`；`dispatch.assert_not_called()`

- [test_live_key_does_not_receive_paper_catalog_and_cancel_only_keeps_executor](../../../../tests/test_mcp_canonical_catalog.py#L126)：`assert set(names) == set(canonical_tools.CORE_NAMES) and len(names) == 8`；`assert len(asyncio.run(server.list_tools())) == 7`

- [test_paper_catalog_counts_scopes_and_hidden_compatibility](../../../../tests/test_mcp_canonical_catalog.py#L21)：`assert server._tool_manager.get_tool('paper_trade') is not None`；`assert len(names) == 8`；`assert not names & {'execute_trade_actions', 'paper_bank', 'configure_paper_account'}`

- [test_paper_catalog_does_not_publish_live_parameters](../../../../tests/test_mcp_canonical_catalog.py#L35)：`assert execution.inputSchema['properties']['contract_version']['const'] == 3`；`assert 'discriminator' in execution.inputSchema['properties']['actions']['items']`；`assert 'profile_id' not in tool.inputSchema.get('properties', {})`

- [test_paper_configuration_is_explicit_and_never_resets](../../../../tests/test_mcp_canonical_catalog.py#L115)：`assert updated['account']['cash'] == 1100 and updated['account']['debt'] == 100`；`assert updated['account']['leverage'] == 5`；`assert updated['account']['leaderboard_visible'] is False`

- [test_paper_history_compact_keeps_cash_and_fill_observation](../../../../tests/test_mcp_canonical_catalog.py#L101)：`assert compact['payload']['cash_after'] == 899.9`；`assert 'namespace' not in compact`；`assert full['namespace'] == caller['paper_namespace']`

- [test_paper_sdk_batch_and_recovery_compact_vs_detail](../../../../tests/test_mcp_canonical_catalog.py#L137)：`assert result['status'] == 'completed' and 'account' not in result['results'][0]`；`assert original['results'][0]['account']['spot'][0]['amount'] == 1`；`assert original['query']['status'] == 'completed'`

- [test_records_default_compact_retains_facts_and_detail_returns_original](../../../../tests/test_mcp_canonical_catalog.py#L83)：`assert short['management']['owner'] == 'mcp:swap' and short['fee']['cost'] == .01`；`assert 'account' not in short and 'exchange_debug' not in short`；`assert canonical_tools.records(live, 'order', profile_id='swap', symbol='BTC/USDT:USDT', order_id='regular:target', detail=True)['order'] == raw`

- [test_sdk_directory_bootstrap_cancel_first_and_read_only_recovery](../../../../tests/test_unified_surface_chain.py#L18)：`assert paper.get_account(caller, '123456')['cash'] == balance`；`assert len(paper.history(caller, '123456')) == fills_before`；`assert conn.execute('SELECT count(*) FROM paper_accounts').fetchone()[0] == 0`

- [test_condition_channel_read_failure_makes_coverage_unknown](../../../../tests/test_decision_state_context.py#L89)：`assert state.coverage['uncovered']['LONG'] is None`；`assert any(i['reason_code'] == 'trigger_orders_unavailable' for i in state.issues)`；`assert partial.positions[0]['amount'] == .15`

- [test_current_view_is_invariant_under_completed_history_and_error_prose](../../../../tests/test_decision_state_context.py#L28)：`assert len(expected.orders) == 1`；`assert render_orders(view(account, [plan, *history])) == render_orders(expected)`；`assert '禁止加仓' not in render_orders(view(account, [plan]))`

- [test_failed_read_does_not_mean_empty_account_and_terminal_missing_fill_is_issue](../../../../tests/test_decision_state_context.py#L136)：`assert '未知' in render_positions(state)`；`assert any(i['reason_code'] == 'fill_unverified' for i in state.issues)`；`assert not any(o['order_id'] == 'algo:sl' and o['ownership'] == 'task' for o in state.orders)`

- [test_parent_child_identity_and_unknown_quantity_are_not_duplicated](../../../../tests/test_decision_state_context.py#L40)：`assert state.orders[0]['remaining'] is None`；`assert state.coverage['uncovered']['LONG'] == 0`；`assert view(account, [plan]).coverage['uncovered']['LONG'] is None`

- [test_scheduler_and_chat_stop_multi_tool_calls_after_failed_or_uncertain_result](../../../../tests/test_decision_memory_context.py#L122)：`assert run.call_count == 1`；`assert len(chat_outputs) == 3 and all('not_executed' in output.content for output in chat_outputs[1:])`；`assert run.call_count == 2 and approve.call_count == 1`

- [test_tiny_exact_quantities_two_prices_parent_and_dynamic_all](../../../../tests/test_trade_context_format.py#L60)：`assert value in text`

- [test_unknown_is_not_zero_and_last_known_ownership_remain](../../../../tests/test_trade_context_format.py#L73)：`assert '未知（最后已知 0.07）' in text and '归属未核验' in text and '事实待核验' in text`；`assert '多仓止盈市价 | 未知 |' in text and '多仓止损市价 | 0 |' in text`；`assert '交易所快照 未知' in text`

- [test_user_three_orders_compact_complete_and_read_only](../../../../tests/test_trade_context_format.py#L38)：`assert row in text`；`assert text.count(order['order_id']) == 1`；`assert omitted not in text`

- [test_edit_role_is_owned_not_supplied_and_replay_needs_no_live_target](../../../../tests/test_mcp_tool_contracts.py#L146)：`assert describe.call_count == 1 and dispatch.call_count == 1`；`assert denied['status'] == 'failed' and dispatch.call_count == 1`；`assert rejected_hint['status'] == 'failed'`

- [test_flat_and_legacy_json_calls_share_one_operation_receipt](../../../../tests/test_mcp_tool_contracts.py#L343)：`dispatch.assert_called_once()`；`assert args.args[2:4] == ('mcp:spot', 'BTC/USDT')`；`assert args.kwargs['operation_id'].startswith('mcp:')`

- [test_gateway_identity_conflicts_fail_before_any_reservation](../../../../tests/test_mcp_tool_contracts.py#L377)：`assert result['status'] == 'failed'`；`dispatch.assert_not_called()`；`assert conn.execute('SELECT COUNT(*) FROM mcp_operations').fetchone()[0] == 0`

- [test_mcp_get_order_and_get_trade_history_read_only_contracts](../../../../tests/test_mcp_tool_contracts.py#L567)：`assert 'info' not in trades[0]`；`assert order_read['id'] == 'order-101'`；`assert len(trades_read) == 1`

- [test_mcp_spot_inventory_tolerates_preexisting_balance_and_bnb_fees](../../../../tests/test_mcp_tool_contracts.py#L498)：`assert result['untracked_holdings'] == 8.0`；`assert result['available_quantity'] == 2.0`；`assert result['exchange_free'] == 10.0`

- [test_new_tools_cannot_bypass_profile_mode_or_cancel_scope](../../../../tests/test_mcp_tool_contracts.py#L127)：`assert mixed['status'] == 'failed' and 'cancel' in mixed['error']`；`dispatch.assert_not_called()`；`assert service.execute(cancel_only, 'swap', request['symbol'], 'execute_trade_actions',                            {'contract_version': 2, 'actions': actions[1:]}, 'cancel-only-0001')['status'] == 'submitted'`

- [test_symbols_are_canonical_scoped_and_filtered_before_paging](../../../../tests/test_mcp_tool_contracts.py#L274)：`assert service.query(caller, 'symbols', profile_id='swap', keyword='ETH', quote='USDT')['total'] == 1`；`assert service.query(caller, 'symbols', profile_id='spot', keyword='ETH')['symbols'][0]['symbol'] == 'ETH/USDT'`；`dispatch.assert_not_called()`

- [test_algo_and_regular_id_collision_and_parent_child_edit_cancel](../../../../tests/test_independent_real_v2.py#L215)：`assert edited['edit_method'] == 'native' and ex.regular[child]['amount'] == .4`；`assert canceled['filled_amount'] == .1 and canceled['actual_remaining'] == pytest.approx(.3)`；`assert ex.regular[child]['status'] == 'canceled'`

- [test_all_edit_retains_dynamic_quantity_and_timeout_recovers](../../../../tests/test_independent_real_v2.py#L252)：`assert len(ex.calls) == count`；`assert result['quantity_mode'] == 'all' and result['amount'] is None`；`assert result['order_id'] != original['order_id'] and result['edit_method'] == 'cancel_replace'`

- [test_cancel_parent_follows_trigger_race_but_edit_does_not](../../../../tests/test_independent_real_v2.py#L236)：`assert replay['status'] == 'failed' and replay['error_code'] == 'order_already_triggered'`；`assert len(ex.calls) == count`；`assert ex.regular[edited['exchange_order_id']]['status'] == 'canceled'`

- [test_conditional_replacement_conflict_does_not_resize_or_block_cycle](../../../../tests/test_independent_real_v2.py#L473)：`assert replay['status'] == 'failed' and replay['error_code'] == 'quantity_conflict'`；`assert len(ex.calls) == count`；`assert next_stop['amount'] == .4`

- [test_fixed_stages_dynamic_remainder_and_addition](../../../../tests/test_independent_real_v2.py#L162)：`assert replay['amount'] is None and replay['remaining'] is None`；`assert replay['actual_remaining'] == 0 and replay['resolved_amount'] == pytest.approx(.9)`；`assert ex.positions['LONG'] == 0`

- [test_legacy_zero_snapshot_does_not_become_dynamic_on_replay](../../../../tests/test_independent_real_v2.py#L287)：`assert replay['amount'] == 1 and replay['quantity_mode'] == 'fixed'`；`assert replay['order_type'] == 'limit' and replay['order_id'].startswith('regular:')`；`assert len(ex.algo) == 0`

- [test_native_edit_remaining_quantity_accounts_for_racing_fills](../../../../tests/test_independent_real_v2.py#L202)：`assert edited['edit_method'] == 'native'`；`assert edited['amount'] == pytest.approx(.7) and edited['remaining_amount'] == pytest.approx(.4)`；`assert len(ex.calls) == count`

- [test_close_cleanup_recovery_then_explicit_new_entry_once](../../../../tests/test_independent_receipt_recovery.py#L135)：`assert historical['position']['episode_id'] == original['episode_id']`；`assert historical['position']['status'] == 'historical'`；`assert len(exchange.calls) == 4`

- [test_failed_replacement_does_not_recover_as_successful_cancel](../../../../tests/test_independent_receipt_recovery.py#L442)：`assert receipt['status'] == 'failed' and callback.call_count == 1`；`assert receipt['reconciliation_evidence'][0]['order_status'] == 'canceled'`；`assert 'exceeds current position' in receipt['reconciliation_evidence'][0]['error']`

- [test_late_verified_residual_can_be_explicitly_managed](../../../../tests/test_independent_receipt_recovery.py#L343)：`assert stop['status'] == 'submitted'`；`assert final['status'] == 'completed' and final['filled_amount'] == pytest.approx(.3)`；`assert [r['amount'] for r in market_calls] == [1, .3]`

- [test_missing_cycle_query_diagnoses_but_does_not_restore](../../../../tests/test_independent_receipt_recovery.py#L247)：`assert closed['filled_amount'] == pytest.approx(.8)`；`assert len(exchange.calls) == before + 1`；`assert not conn.execute("SELECT 1 FROM real_protection_plans WHERE config_id='cfg'").fetchone()`

- [test_missing_cycle_rejects_incomplete_or_conflicting_evidence](../../../../tests/test_independent_receipt_recovery.py#L267)：`assert result['error_code'] == 'independent_position_cycle_missing'`；`assert result['details']['reason_code'] == {'quantity': 'quantity_mismatch', 'history': 'incomplete_history', 'ownership': 'incomplete_ownership_evidence'}[case]`；`assert len(exchange.calls) == before`

- [test_upgrade_preserves_ids_episode_fill_indexes_receipts_and_legacy_meaning](../../../../tests/test_independent_exit_migration.py#L44)：`assert limit['realized_pnl']==2`；`assert conn.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_legacy_episode'").fetchone()`；`assert json.dumps([dict(row) for row in conn.execute('SELECT * FROM mock_exit_orders ORDER BY order_id')],sort_keys=True)==before`

- [test_explicit_task_delete_checks_before_purging_original_operation](../../../../tests/test_exclusive_config_lifecycle.py#L109)：`assert response.status_code in {400, 409}, response.text`；`assert contents(path) == before`；`assert runtime.get_config_by_id('owner') is not None`

- [test_pending_actions_reject_scope_retirement_without_plans_and_keep_database](../../../../tests/test_exclusive_config_lifecycle.py#L81)：`assert contents(path) == before`

- [test_conditional_all_edit_replaces_identity_and_preserves_dynamic_quantity](../../../../tests/test_independent_mock_v2.py#L183)：`assert store.describe_order(order['order_id'])['order_status']=='canceled'`；`assert store.edit(order['order_id'],trigger_price=95,current_price=100,operation_id='edit')==changed`；`assert len(store.snapshot()['exits'])==1`

- [test_quantity_conflict_does_not_block_explicit_edit_or_cancel](../../../../tests/test_independent_mock_v2.py#L268)：`assert store.snapshot()['quantity_conflict']`；`assert not store.snapshot()['quantity_conflict']`；`assert len(store.snapshot()['exits'])==1`

- [test_split_stop_losses_gap_fixed_fixed_all_and_no_duplicate_fills](../../../../tests/test_independent_mock_v2.py#L60)：`assert store.describe_order(pending['order_id'])['order_status']=='canceled'`；`assert bar(store,high=100,low=60,step=3)=={'filled':0,'closed':0}`；`assert conn.execute('SELECT balance FROM mock_accounts').fetchone()[0]==pytest.approx(9988.5)`

- [test_two_fixed_stages_then_dynamic_all_closes_only_target_direction](../../../../tests/test_independent_mock_v2.py#L35)：`assert actual['filled_amount']==pytest.approx(.5)`；`assert actual['order_status']=='filled' and actual['order_id'].startswith('regular:')`；`assert actual['amount'] is None and actual['remaining'] is None and actual['actual_remaining']==0`

- [test_actual_audit_includes_input_raw_output_and_no_rule_tools](../../../../tests/test_memory_agent.py#L211)：`assert latest["details"]["rule_receipts"] == result.rule_receipts`；`assert latest["tools"] == []`；`assert strategy_material(source) in latest["messages"][1]["content"]`

- [test_memory_only_one_call_no_rules_and_cost](../../../../tests/test_memory_agent.py#L77)：`assert locked["rule_id"] not in supplied and disabled["rule_id"] not in supplied`；`assert "当前完整规则" not in supplied`；`assert environment == [{"symbol": "BTC/USDT", "config_id": "cfg", "model": "mock-model", "prompt_tokens": 12, "completion_tokens": 8}]`

- [test_canonical_edit_guards_classify_the_owned_target](../../../../tests/test_mcp_gateway.py#L180)：`entry.assert_called_once()`；`owner.assert_not_called()`；`owner.assert_called_once()`

- [test_sdk_initialization_tool_list_and_scoped_call](../../../../tests/test_mcp_gateway.py#L47)：`assert 'fake' not in response.text`；`assert 'Missing MCP scope: cancel' in denied.text`；`assert gateway.get('/api/mcp', headers={'Authorization': 'Bearer ' + key}).status_code == 401`

- [test_unauthenticated_discovery_and_revoked_key](../../../../tests/test_mcp_gateway.py#L65)：`assert metadata['code_challenge_methods_supported'] == ['S256']`；`assert metadata['token_endpoint'] == 'http://localhost:7860/oauth/token'`；`assert rpc(gateway, key['secret'], 'tools/list').status_code == 401`

- [test_active_worker_queries_are_pending_and_do_not_duplicate_submission](../../../../tests/test_paper_ordered_batch.py#L313)：`assert queried['status'] == 'pending'`；`assert batch(caller, [opening()])['status'] == 'pending'`；`assert future.result(timeout=5)['status'] == 'completed'`

- [test_commit_evidence_failure_is_unknown_then_original_child_recovers](../../../../tests/test_paper_ordered_batch.py#L285)：`assert recovered['results'][0]['status'] == 'completed'`；`assert recovered['results'][1]['status'] == 'not_executed'`；`assert paper.get_account(caller, '123456')['positions'][0]['amount'] == 1`

- [test_cross_process_retry_tracks_physical_worker_and_has_one_fill](../../../../tests/test_paper_ordered_batch.py#L335)：`assert output.get(timeout=10)['status'] == 'completed'`；`assert process.exitcode == 0`；`assert paper.get_account(caller, '123456')['positions'][0]['amount'] == 1`

- [test_legacy_paper_fingerprints_and_partial_cancel_receipts](../../../../tests/test_paper_ordered_batch.py#L164)：`assert paper.trade(caller, '123456', 'spot', request['symbol'], request['action'], float(request['amount']), item['operation_id']) == result`；`assert result['account']['cash'] == item['expected']['cash']`；`assert result['account']['spot'][0]['amount'] == item['expected']['spot_amount']`

- [test_multi_venue_exit_requires_explicit_target_and_replays_without_financial_effect](../../../../tests/test_paper_ordered_batch.py#L382)：`assert next(row for row in holdings if row['exchange'] == 'bitget')['amount'] == (.5 if action == 'close' else 1)`；`assert {order['exchange'] for order in accepted['results'][0]['orders']} == {'bitget'}`；`assert accepted['results'][0]['fill']['exchange'] == 'bitget'`

- [test_paper_batch_namespace_and_user_order_isolation](../../../../tests/test_paper_ordered_batch.py#L140)：`assert result['status'] == 'failed'`；`assert paper_orders.get_orders(caller, 'another')['orders'][0]['state'] == 'open'`；`assert paper_batch.operation({**caller, 'paper_namespace': 'other'}, '123456', 'paper-parent-0001') is None`

- [test_paper_cancel_releases_reservation_before_next_action](../../../../tests/test_paper_ordered_batch.py#L52)：`assert [item['status'] for item in result['results']] == ['completed', 'completed']`；`assert paper.get_account(caller, '123456')['reserved_cash'] == 0`

- [test_paper_crash_after_child_commit_never_repeats_or_runs_tail](../../../../tests/test_paper_ordered_batch.py#L85)：`assert [item['status'] for item in recovered['results']] == ['completed', 'not_executed']`；`assert paper.get_account(caller, '123456')['positions'][0]['amount'] == 1`

- [test_paper_crash_after_parent_registration_never_starts_tail](../../../../tests/test_paper_ordered_batch.py#L101)：`assert recovered['results'][0]['status'] == 'not_executed'`；`assert paper.get_account(caller, '123456')['positions'] == []`

- [test_paper_expired_budget_preserves_committed_prefix_and_final_query](../../../../tests/test_paper_ordered_batch.py#L191)：`assert [row['status'] for row in result['results']] == ['completed', 'not_executed']`；`assert paper_batch.operation(caller, '123456', result['operation_id'])['results'][0]['status'] == 'completed'`

- [test_paper_failed_quote_and_invalid_fractional_leverage_are_not_unknown](../../../../tests/test_paper_ordered_batch.py#L211)：`assert result['status'] == 'failed'`；`assert result['results'][0]['submission'] == 'not_submitted'`；`assert paper_batch.operation(caller, '123456', result['operation_id']) == result`

- [test_paper_limit_acceptance_is_not_fill_or_dependency_completion](../../../../tests/test_paper_ordered_batch.py#L71)：`assert result['results'][1]['status'] == 'not_executed'`；`assert result['results'][1]['blocked_by'] == [0]`；`assert paper.get_account(caller, '123456')['positions'] == []`

- [test_paper_parent_ids_replay_and_changed_intent](../../../../tests/test_paper_ordered_batch.py#L37)：`assert batch({**caller, 'id': 'rotated-key'}, [opening()], operation_id='p' * 160) == first`；`assert len(quotes) == before`；`assert paper.get_account(caller, '123456')['positions'][0]['amount'] == 1`

- [test_paper_preflight_invalid_later_action_has_no_effect_and_is_durable](../../../../tests/test_paper_ordered_batch.py#L61)：`assert not any(item['attempted'] for item in result['results'])`；`assert paper.get_account(caller, '123456')['positions'] == []`；`assert paper_batch.operation(caller, '123456', result['operation_id']) == result`

- [test_paper_protection_and_cycle_reopen_remain_independent](../../../../tests/test_paper_ordered_batch.py#L151)：`assert result['status'] == 'completed'`；`assert paper.get_account(caller, '123456')['positions'][0]['amount'] == 2`；`assert paper_orders.get_orders(caller, '123456')['orders'] == []`

- [test_paper_quote_network_does_not_hold_write_transaction](../../../../tests/test_paper_ordered_batch.py#L127)：`assert batch(caller, [opening()])['status'] == 'completed'`

- [test_paper_same_parent_concurrent_retries_have_one_fill](../../../../tests/test_paper_ordered_batch.py#L117)：`assert recovered['status'] == 'completed'`；`assert paper.get_account(caller, '123456')['positions'][0]['amount'] == 1`

- [test_paper_twenty_item_model_projection_has_one_summary](../../../../tests/test_paper_ordered_batch.py#L225)：`assert len(projected['results']) == 20 and 'account_summary' in projected`；`assert all('account' not in row and 'request' not in row for row in projected['results'])`

- [test_sdk_intent_storage_error_keeps_original_id_and_does_not_claim_prior_failure](../../../../tests/test_paper_ordered_batch.py#L446)：`assert recovered['results'][0]['operation_id'] == original['results'][0]['operation_id']`；`assert len(quotes) == quote_count`；`assert paper_batch.operation(caller, '123456', parent) is None`

- [test_single_nondefault_venue_exit_resolves_the_existing_holding](../../../../tests/test_paper_ordered_batch.py#L413)：`assert closed['results'][0]['fill']['exchange'] == 'bitget'`；`assert quotes[-1] == (symbol, market, 'bitget')`；`assert paper.get_account(caller, '123456')['spot' if market == 'spot' else 'positions'] == []`

- [test_sqlite_failure_during_intent_insert_rolls_back_without_dispatch](../../../../tests/test_paper_ordered_batch.py#L486)：`assert result['submission_state'] == 'not_submitted' and result['query']['status'] == 'failed'`；`assert quotes == [] and paper.get_account(caller, '123456')['positions'] == []`；`assert paper_batch.operation(caller, '123456', result['operation_id']) is None`

- [test_attached_and_spot_formatters_show_authority_and_partial_evidence](../../../../tests/test_visible_management_projection.py#L52)：`assert '已纳管' in text and '管理者 cfg' in text and 'close' in text and 'cancel/edit' in text`；`assert 'flat exposure unconfirmed' in format_positions_to_agent_friendly([], observation={'error': 'offline'})`；`assert 'no-orders status unconfirmed' in format_orders_to_agent_friendly([], observation={'error': 'offline'})`

- [test_competitor_is_local_and_preserves_current_quantities](../../../../tests/test_visible_management_projection.py#L38)：`assert state.positions[0]['amount'] == .02 and not state.positions[0]['managed']`；`assert not state.positions[0]['can_claim'] and state.orders[0]['owner'] == 'another-task'`；`assert '其他管理者占用' in render_orders(state)`

- [test_manual_resource_can_be_claimed_without_cycle_or_false_history](../../../../tests/test_visible_management_projection.py#L21)：`assert position['supported_actions'] == ['close'] and order['supported_actions'] == ['cancel', 'edit']`；`assert not any(issue['reason_code'] == 'ownership_unverified' for issue in state.issues)`；`assert '可自动认领' in text`

- [test_owned_manual_resource_does_not_need_agent_entry_history](../../../../tests/test_visible_management_projection.py#L31)：`assert state.positions[0]['ownership'] == 'task'`；`assert state.orders[0]['managed'] and state.orders[0]['owner'] == 'cfg'`；`assert '已纳管' in render_positions(state)`

- [test_visible_ack_does_not_release_live_process_but_crash_can_recover_proof](../../../../tests/test_management_recovery.py#L283)：`assert management.recover_reservations(service, SYMBOL) == 1`；`assert replay['status'] == 'submitted'`；`assert process.exitcode == 0`

- [test_repeated_schema_initialization_preserves_unknown_parent_and_child](../../../../tests/test_trading_runtime_compatibility.py#L12)：`assert blocked['status'] == 'blocked' and blocked['read_only']`；`assert any(row['reason'] == 'unresolved_or_live_v3_operation' for row in blocked['blockers'])`；`assert trading_compatibility_report(conn, supported_contract_version=3)['status'] == 'compatible'`

- [test_binance_oco_is_one_native_group_and_one_inventory_reservation](../../../../tests/test_mcp_spot_exits.py#L313)：`assert stock['available_quantity'] == pytest.approx(0.99)`；`assert result['status'] == 'cancelled'`；`assert spot.writes[-1][0] == 'cancel_oco'`

- [test_coexisting_manual_holdings_tolerated_and_bounded](../../../../tests/test_mcp_spot_exits.py#L186)：`assert stock['available_quantity'] == pytest.approx(9.99)`；`assert row['status'] == 'open'`；`assert len(spot.writes) == 1`

- [test_cross_quote_foreign_profile_evidence_blocks_even_equal_balance](../../../../tests/test_mcp_spot_exits.py#L203)：`assert spot.writes == []`

- [test_okx_live_algo_without_execution_size_keeps_reservation](../../../../tests/test_mcp_spot_exits.py#L414)：`assert stock['reserved_quantity'] == 3 and stock['exits'][0]['status'] == 'open'`

- [test_okx_terminal_algo_needs_explicit_zero_execution_to_release](../../../../tests/test_mcp_spot_exits.py#L395)：`assert exits._rows('mcp:test')[0]['state'] == 'unknown'`；`assert stock['reserved_quantity'] == 0`；`assert stock['exits'][0]['status'] == 'cancelled'`

- [test_owned_exit_cannot_be_manually_enlarged_without_detection](../../../../tests/test_mcp_spot_exits.py#L277)：`assert len(spot.writes) == 1`

- [test_partial_exit_and_cancel_release_only_unfilled_owned_amount](../../../../tests/test_mcp_spot_exits.py#L230)：`assert stock['reserved_quantity'] == 2`；`assert result['status'] == 'cancelled' and result['filled'] == 1`；`assert exits.inventory('mcp:test', 'BTC/USDT')['available_quantity'] == pytest.approx(8.99)`

- [test_precision_cannot_zero_inflate_or_corrupt_reserved_quantity](../../../../tests/test_mcp_spot_exits.py#L270)：`assert spot.writes == []`

- [test_reconciled_fill_between_read_and_reservation_does_not_restore_capacity](../../../../tests/test_mcp_spot_exits.py#L285)：`assert result['status'] == 'open'`；`assert len(spot.writes) == 2`

- [test_disabled_unrestricted_profiles_keep_all_owned_perpetual_symbols_for_maintenance](../../../../tests/test_mcp_symbol_scope.py#L201)：`assert runtime['enabled'] is False`；`assert runtime['mcp_owned_symbols'] == ['ETH/USDT:USDT', 'SOL/USDC:USDC']`；`assert seen == runtime['mcp_owned_symbols']`

- [test_explicit_scope_defaults_and_fresh_profiles](../../../../tests/test_mcp_symbol_scope.py#L72)：`assert service.query(caller, 'balance', profile_id='spot')['total'] == {'USDT': 1000}`；`assert any(tool['name'] == 'open_position_spot_dca' for tool in service.query(caller, 'tools', profile_id='spot'))`；`assert all(row['symbol_scope'] == 'all' for row in service.list_profiles(caller))`

- [test_narrowing_and_import_cannot_orphan_unrestricted_orders](../../../../tests/test_mcp_symbol_scope.py#L184)：`assert store.get('profile', 'spot')['symbol_scope'] == 'all'`；`assert store.get('profile', 'spot')['symbol_scope'] == 'selected'`

- [test_canonical_batch_cannot_exit_unfilled_entry](../../../../tests/test_trade_tool_integration.py#L234)：`assert [row['status'] for row in receipt['results']] == ['submitted', 'failed', 'not_executed']`；`assert len(simulation.snapshot()['entries']) == 1`

- [test_confirmed_close_with_late_entry_keeps_residual_separate](../../../../tests/test_trade_tool_integration.py#L107)：`assert result['position']['amount'] == pytest.approx(.3)`；`assert result['cleanup']['status'] == 'pending'`；`assert len(exchange.calls) == 3`

- [test_market_short_close_keeps_fill_when_cleanup_ack_has_no_fill](../../../../tests/test_trade_tool_integration.py#L67)：`assert stop['order_id'] in [item['order_id'] for item in result['cleanup']['orders']]`；`assert result['recovery']['target_operation_id'] == 'market-close-short'`；`assert sum(row['type'] == 'market' and 'stopLossPrice' not in row['params']                for row in exchange.calls) == 1`

- [test_one_call_multi_exit_and_replay](../../../../tests/test_trade_tool_integration.py#L161)：`assert len(simulation.snapshot()['exits']) == 3`；`assert sorted(x['remaining'] for x in simulation.snapshot()['exits']) == [1, 1, 2]`；`assert simulation.snapshot()['positions'][0]['amount'] == 1.5`

- [test_real_close_skips_confirmed_empty_side_and_continues_batch](../../../../tests/test_trade_tool_integration.py#L283)：`assert result['results'][0]['reason'] == 'no_position'`；`assert first['results'][0]['status'] == 'skipped'`；`assert first['results'][0]['reason'] == 'no_position'`

- [test_account_summary_failure_preserves_financial_status_and_is_single](../../../../tests/test_unified_trading.py#L259)：`summary.assert_called_once()`；`assert 'account' not in compact['results'][0]['result']`；`assert sum('account_summary' in row for row in compact['results']) == 0`

- [test_actual_main_and_small_bind_exactly_two_tools](../../../../tests/test_unified_trading.py#L429)：`assert [tool.name for tool in model.tools] == ['execute_trade_actions', 'get_trade_operation']`；`assert model.tools[0].args_schema['properties']['contract_version']['const'] == 3`；`assert result.messages[-1].content == 'observed'`

- [test_actual_spot_v3_multi_buy_shares_one_parent_allowance](../../../../tests/test_unified_trading.py#L662)：`assert len(spot_runtime.calls) == 1`；`assert [list(row) for row in cycles] == [['shared-budget', 100]]`；`assert conn.execute('SELECT COUNT(DISTINCT cycle_id) FROM spot_budget_reservations').fetchone()[0] == 1`

- [test_attached_strategy_open_edit_protect_cancel_replay_is_atomic](../../../../tests/test_unified_trading.py#L286)：`assert call(batch(opening(stop_loss=90, take_profit=110)), 'open') == opened`；`assert list(row) == [.3, 100, 90, 120, 'CANCELLED']`；`assert conn.execute('SELECT COUNT(*) FROM mock_trade_operations').fetchone()[0] == 4`

- [test_bound_chat_v3_approves_each_action_and_reject_stops_tail_but_query_available](../../../../tests/test_unified_trading.py#L364)：`assert json.loads(result['messages'][1].content)['query']['status'] == 'completed'`；`assert approvals.call_count == 2 and dispatched.call_count == 1`；`assert [call.args[0]['action_index'] for call in approvals.call_args_list] == [0, 1]`

- [test_bounded_action_array](../../../../tests/test_unified_trading.py#L58)：pytest.raises/fixture验证，见函数全文。

- [test_cancel_first_dynamic_rejection_keeps_completed_prefix](../../../../tests/test_unified_trading.py#L138)：`assert [call.args[0].action for call in dispatch.call_args_list] == ['cancel', 'open']`；`assert execute(payload, dispatch) == result`；`assert dispatch.call_count == 2`

- [test_compact_projection_keeps_unknown_identity_quantity_and_cleanup_truth](../../../../tests/test_unified_trading.py#L270)：`assert compact['results'][1]['blocked_by_indices'] == [0]`；`assert len(json.dumps(compact)) < len(json.dumps(raw)) / 4`；`assert raw['results'][0]['result']['account']['balance'] == 123`

- [test_conflicting_parent_intent_and_context_never_dispatch](../../../../tests/test_unified_trading.py#L190)：`dispatch.assert_called_once()`

- [test_current_step_entry_gate_precedes_claim_and_does_not_block_prior_cancel](../../../../tests/test_unified_trading.py#L720)：`assert [row['status'] for row in result['results']] == ['completed', 'failed']`；`assert claim_entries == ['guarded:0']`；`assert gate.call_args.kwargs['only_capability'] is True`

- [test_default_error_stop_and_uncertain_replay](../../../../tests/test_unified_trading.py#L160)：`assert [row['status'] for row in result['results']] == [status, 'not_executed']`；`assert execute(payload, dispatch)['results'] == result['results']`；`dispatch.assert_called_once()`

- [test_dependency_indices_cannot_be_ambiguous](../../../../tests/test_unified_trading.py#L80)：pytest.raises/fixture验证，见函数全文。

- [test_explicit_dependency_requires_completed_and_independent_continues](../../../../tests/test_unified_trading.py#L170)：`assert dispatch.call_count == expected_calls`；`assert result['results'][1]['status'] == 'not_executed'`；`assert result['results'][1]['blocked_by_indices'] == [0]`

- [test_known_paper_capabilities_are_static](../../../../tests/test_unified_trading.py#L122)：pytest.raises/fixture验证，见函数全文。

- [test_paper_market_capability_does_not_expand_live](../../../../tests/test_unified_trading.py#L109)：`assert parsed.actions[0].order_type == 'market'`

- [test_published_json_schema_rejects_known_unsupported_shapes](../../../../tests/test_unified_trading.py#L650)：`assert unified.validate_for_config(batch(action), cfg, engine=engine)`

- [test_published_mode_schema_and_default_catalog](../../../../tests/test_unified_trading.py#L91)：`assert schema['properties']['contract_version']['const'] == 3`；`assert [tool.name for tool in chat_graph._get_chat_tools(cfg)] == [tool.name for tool in tools]`；`assert not chat_graph._get_chat_tools({**cfg, 'read_only': True})`

- [test_saved_legacy_fingerprints_receipts_and_tail_replay_are_unchanged](../../../../tests/test_unified_trading.py#L394)：`assert rows[child['operation_id']]['request_hash'] == child['fingerprint']`；`assert intents[child['operation_id']] == child['request']`；`assert fixture['name'] + ':2' not in rows`

- [test_spot_cancel_first_is_not_blocked_by_later_zero_allowance](../../../../tests/test_unified_trading.py#L685)：`assert [row['status'] for row in result['results']] == ['completed', 'failed']`；`dispatched.assert_called_once()`；`assert current_spot_cycle_id.get() is None`

- [test_trusted_symbol_boundary_and_unique_default](../../../../tests/test_unified_trading.py#L127)：`assert unified.validate_for_config(batch(cancel()), CFG).actions[0].symbol == 'ETH/USDT'`；`assert unified.validate_for_config(batch(cancel(symbol='BTC/USDT:USDT')), unrestricted).actions[0].symbol == 'BTC/USDT:USDT'`；`assert unified.validate_for_config(batch(cancel(symbol='ETH/USDT:USDT')), {**CFG, 'mode': 'REAL'}).actions[0].symbol == 'ETH/USDT:USDT'`

- [test_unknown_fields_invalid_sizing_and_quantity_fail](../../../../tests/test_unified_trading.py#L74)：pytest.raises/fixture验证，见函数全文。

- [test_v3_independent_remaining_edit_uses_latest_partial_fill](../../../../tests/test_unified_trading.py#L624)：`assert edited['status'] == 'completed'`；`assert conn.execute('SELECT amount FROM mock_orders').fetchone()[0] == .5`

- [test_whole_batch_static_failure_has_zero_dispatch_and_claim](../../../../tests/test_unified_trading.py#L150)：`assert result['status'] == 'failed' and result['phase'] == 'preflight'`；`dispatch.assert_not_called()`；`assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='unified_trade_children'").fetchone()`

- [test_worker_crash_recovers_attempted_items_without_tail_replay](../../../../tests/test_unified_trading.py#L213)：`assert [row['status'] for row in recovered['results']] == ['completed', 'unknown', 'not_executed']`；`assert dispatch.call_count == 2`

- [test_workflow_json_v3_examples_use_actual_capability_validator](../../../../tests/test_unified_trading.py#L381)：`assert len(unified_examples) >= 2`；`assert unified.validate_for_config(value, {**CFG, 'mode': 'REAL'})`

- [test_authority_survives_flat_pause_and_explicit_release](../../../../tests/test_visible_position_management.py#L149)：`assert management.management_status('owner')['scopes'][0]['claim_id'] == claim['claim_id']`；`assert result['status'] == 'released'`；`assert management.claim_management(tool('other'), SYMBOL, 'LONG')['owner'] == 'other'`

- [test_cross_process_claim_only_one_wins](../../../../tests/test_visible_position_management.py#L547)：`assert sorted(r[0] for r in results) == ['conflict', 'owned']`；`assert process.exitcode == 0`

- [test_hedge_unused_direction_is_not_preclaimed_but_oneway_shares_both](../../../../tests/test_visible_position_management.py#L79)：`assert management.claim_management(tool('other'), SYMBOL, 'SHORT')['side'] == 'SHORT'`

- [test_legacy_migration_only_retires_never_bound_reservations](../../../../tests/test_visible_position_management.py#L162)：`assert claim['resource_bound'] is bound`；`assert conn.execute("SELECT state FROM visible_management_claims").fetchone()[0] == 'owned'`；`assert conn.execute("SELECT state FROM visible_management_claims").fetchone()[0] == 'released'`

- [test_physical_labels_are_venue_scoped_and_binance_spot_swap_share_identity](../../../../tests/test_visible_position_management.py#L121)：`assert management.physical_account_scope(swap) == management.physical_account_scope(spot)`；`assert management.physical_account_scope(swap) != management.physical_account_scope(okx)`；`assert management.claim_management(okx, SYMBOL, 'LONG')['owner'] == 'other'`

- [test_query_does_not_claim_or_mutate_and_reports_competition](../../../../tests/test_visible_position_management.py#L66)：`assert management.management_status('owner')['scopes'] == []`；`assert projected['state'] == 'conflict' and projected['owner'] == 'owner'`；`assert projected['claim_id'] == claim['claim_id']`

- [test_readonly_disabled_and_outside_symbol_cannot_claim](../../../../tests/test_visible_position_management.py#L133)：`assert management.management_status('owner')['scopes'] == []`

- [test_regular_spot_cancel_keeps_inventory_reserved_until_fills_are_verified](../../../../tests/test_visible_position_management.py#L366)：`assert resolved['status']=='completed'`；`assert visible_inventory(mt,'BTC/USDT')['available_quantity']==pytest.approx(9.99)`；`assert len(venue.writes)==1`

- [test_same_credential_cannot_bypass_owner_with_a_conflicting_account_alias](../../../../tests/test_visible_position_management.py#L113)：`assert len(management.management_status('owner')['scopes']) == 1`；`assert management.management_status('other')['scopes'] == []`

- [test_same_physical_account_multiple_keys_conflict](../../../../tests/test_visible_position_management.py#L103)：`assert failure.value.details['claim']['claim_id'] == claim['claim_id']`

- [test_spot_baseline_origin_and_oco_crossquote_reservation](../../../../tests/test_visible_position_management.py#L524)：`assert visible_inventory(mt, 'ETH/USDT')['claim_owner'] is None`；`assert stock['verified_owned'] == 0 and stock['managed_quantity'] == 1.2`；`assert stock['reserved_quantity'] == .4 and stock['available_quantity'] == pytest.approx(.8)`

- [test_spot_transfer_recovers_and_cancels_original_native_exit_without_source_config](../../../../tests/test_visible_position_management.py#L321)：`assert cancelled['status'] == 'cancelled'`；`assert spot_orders._rows('owner')[0]['config_id'] == 'owner'`；`assert spot_orders._rows('receiver') == []`

- [test_swap_handoff_query_uses_receiver_account_after_source_configuration_retirement](../../../../tests/test_visible_position_management.py#L208)：`assert venue.writes == before`；`assert management.lookup_management_recovery('unrelated', grant['recovery_id']) is None`；`assert config_id == 'receiver', 'retired source configuration was used for recovery credentials'`

- [test_transfer_keeps_original_identity_and_recovery_is_narrow](../../../../tests/test_visible_position_management.py#L190)：`assert result['status'] == 'transferred'`；`assert grant['operation_id'] == 'original-child' and grant['source_owner'] == 'owner'`；`assert management.lookup_management_recovery('unrelated', grant['recovery_id']) is None`

- [test_visible_direct_request_manages_only_its_hedge_target](../../../../tests/test_visible_position_management.py#L88)：`assert result['status'] == 'submitted'`；`assert management.management_status('short-owner')['scopes'][0]['side'] == 'SHORT'`；`assert len(venue.writes) == 1`

- [test_visible_spot_historical_fees_and_external_fills_remain_separate](../../../../tests/test_visible_position_management.py#L349)：`assert inventory['fee_adjustment'] == pytest.approx(.01)`；`assert inventory['external_holdings'] == pytest.approx(2)`；`assert inventory['managed_quantity'] == pytest.approx(11.99)`

- [test_bot_provision_ranking_and_admin_funding_does_not_inflate_pnl](../../../../tests/test_mcp_paper_v2.py#L127)：`assert paper.get_account(caller, '123456')['spot'] == []`；`assert competition.bot_account(caller)['spot'][0]['amount'] == 1`；`assert competition.leaderboard(caller)['total'] == 1`

- [test_budget_units_margin_vs_notional_and_atomic_close_all](../../../../tests/test_mcp_paper_v2.py#L114)：`assert first['fill']['amount'] == 10`；`assert first['account']['used_margin'] == 100`；`assert notional['fill']['amount'] == 1 and notional['account']['used_margin'] == 10`

- [test_cancel_and_edit_release_old_reservation](../../../../tests/test_mcp_paper_v2.py#L47)：`assert paper.get_account(caller, '123456')['reserved_cash'] == 160.16`；`assert orders.cancel(caller, '123456', order['order_id'], 'cancel-limit-001') == cancelled`；`assert paper.get_account(caller, '123456')['reserved_cash'] == 0`

- [test_concurrent_matchers_do_not_double_fill](../../../../tests/test_mcp_paper_v2.py#L251)：`assert paper.get_account(caller, '123456')['spot'][0]['amount'] == 1`；`assert len(competition.scan(caller)['events']) == 1`

- [test_http_polling_sdk_adapters_and_admin_dashboard](../../../../tests/test_mcp_paper_v2.py#L262)：`assert client.get('/api/mcp/paper/dashboard', headers=hdr).status_code == 401`；`assert client.get('/api/mcp/paper/dashboard', headers=admin).json()['leaderboard']`；`assert not result.get('isError'), result`

- [test_limit_entry_attaches_protection_only_after_fill](../../../../tests/test_mcp_paper_v2.py#L101)：`assert len(orders.get_orders(caller, '123456')['orders']) == 1`；`assert len(orders.get_orders(caller, '123456')['orders']) == 2`；`assert paper.get_account(caller, '123456')['positions'] == []`

- [test_liquidation_cancels_exits_and_notifies_with_debt](../../../../tests/test_mcp_paper_v2.py#L165)：`assert not orders.get_orders(caller, '123456')['orders']`；`assert event['kind'] == 'liquidation' and event['payload']['debt_after'] == 100`

- [test_oco_protection_closes_correct_side_once](../../../../tests/test_mcp_paper_v2.py#L63)：`assert len(filled) == 1 and filled[0]['order_type'] == kind`；`assert len(orders.get_orders(caller, '123456', 'cancelled')['orders']) == 1`；`assert len([e for e in competition.scan(caller)['events'] if e['kind'] == 'order_filled']) == 1`

- [test_old_protection_cannot_close_reopened_position](../../../../tests/test_mcp_paper_v2.py#L78)：`assert paper.get_account(caller, '123456')['positions'][0]['amount'] == 1`；`assert not orders.get_orders(caller, '123456')['orders']`

- [test_scan_cursor_empty_pages_and_namespace_isolation](../../../../tests/test_mcp_paper_v2.py#L147)：`assert competition.scan(caller, feed['next_cursor'])['events'] == []`；`assert competition.scan(other)['events'] == []`；`assert competition.scan(caller)['events'] == []`

- [test_symbol_discovery_queries_catalogue_and_maps_mub](../../../../tests/test_mcp_paper_v2.py#L193)：`assert found['symbols'][0]['catalogue_verified'] is True`；`assert paper_market.search('美光', 'spot', 'binance')['total'] == 1`；`assert paper_market.search('not-listed', 'spot', 'binance')['total'] == 0`

- [test_symbol_search_finds_tencent_on_other_venue_after_default_failure](../../../../tests/test_mcp_paper_v2.py#L209)：`assert found['symbols'][0]['exchange'] == 'bitget'`；`assert found['symbols'][0]['symbol'] == 'TENCENT/USDT:USDT'`；`assert len(found['queried_exchanges']) == 4`

- [test_agent_and_bound_chat_queries_project_resources_without_enrollment](../../../../tests/test_exclusive_position_management.py#L765)：`assert not state.issues`；`assert exchange.writes == []`；`assert {entry[2] for entry in exchange.reads if isinstance(entry, tuple) and entry[:2] == ('book', 'algo')} == set(OKX_ALGO_ORDER_TYPES)`

- [test_close_all_uses_current_reduced_quantity_with_real_close_only_semantics](../../../../tests/test_exclusive_position_management.py#L672)：`assert submitted[0][1] == .01`；`assert submitted[0][2]['reduceOnly'] is True`；`assert exchange.quantities['LONG'] == 0`

- [test_external_fullclose_cleans_old_orders_and_later_close_skips](../../../../tests/test_exclusive_position_management.py#L383)：`assert all(order['status'] == 'canceled' for order in exchange.orders.values())`；`assert result['status'] == 'skipped'`；`assert exchange.writes == writes`

- [test_fill_failure_keeps_successful_checkpoint_and_can_resume](../../../../tests/test_exclusive_position_management.py#L447)：`assert exchange.writes == []`；`assert failed['through_ms'] == checkpoint`；`assert not failed['complete']`

- [test_fixed_exit_never_silently_shrinks_after_manual_reduction](../../../../tests/test_exclusive_position_management.py#L662)：`assert exchange.quantities['LONG'] == .01`；`assert exchange.writes == []`

- [test_hedge_direction_can_have_separate_manager_but_oneway_cannot](../../../../tests/test_exclusive_position_management.py#L296)：`assert plan(other, 'SHORT')['state'] == 'ACTIVE'`；`assert management_snapshot(other.config_id)['scopes'][0]['side'] == 'SHORT'`

- [test_late_external_entry_fill_during_cleanup_preserves_residual_without_autoclose](../../../../tests/test_exclusive_position_management.py#L516)：`assert exchange.quantities['LONG'] == .005`；`assert plan(service)['state'] == 'ACTIVE'`；`assert not [write for write in exchange.writes if write[0] == 'create']`

- [test_lost_ack_is_matched_to_original_agent_order_not_second_external_record](../../../../tests/test_exclusive_position_management.py#L499)：`assert records[0]['id'] == 'managed-1'`；`assert records[0].get('source') != 'external'`；`assert len([write for write in exchange.writes if write[0] == 'create']) == 1`

- [test_manual_quantity_price_and_trigger_changes_replace_observed_values](../../../../tests/test_exclusive_position_management.py#L220)：`assert records['tp']['amount'] == .015`；`assert records['stop']['trigger_price'] == 2460`；`assert exchange.writes == []`

- [test_mcp_cancel_scope_and_principal_isolation_survive_enrollment](../../../../tests/test_exclusive_position_management.py#L540)：`assert result['status'] == 'failed', result`；`assert 'cancel' in result.get('message', result.get('error', '')) or 'scope' in str(result)`；`assert exchange.writes == []`

- [test_paginated_external_fills_are_complete_deduplicated_and_checkpointed](../../../../tests/test_exclusive_position_management.py#L419)：`assert exchange.writes == []`；`assert conn.execute('SELECT COUNT(*) FROM execution_fills').fetchone()[0] == fill_count`；`assert state['complete'] and state['through_ms'] == exchange.now`

- [test_price_only_edit_uses_latest_manual_quantity_and_partial_fills](../../../../tests/test_exclusive_position_management.py#L235)：`assert target['amount'] == .015`；`assert target['filled'] == .005`；`assert target['price'] == 2580`

- [test_startup_baseline_and_three_external_orders_are_readonly_and_deduplicated](../../../../tests/test_exclusive_position_management.py#L183)：`assert len(plan(service)['exits']) == 2 and len(plan(service)['entries']) == 1`；`assert plan(service)['episode_id'] == first['episode_id']`；`assert conn.execute('SELECT COUNT(*) FROM execution_fills').fetchone()[0] == 0`

- [test_unenrolled_exchange_order_keeps_capability_separate_from_management](../../../../tests/test_exclusive_position_management.py#L743)：`assert payload['pending_orders'][0]['management_state'] in {'owned', 'eligible_unclaimed'}`；`assert payload['pending_orders'][0]['supported_actions'] == ['cancel', 'edit']`；`assert exchange.writes == []`

- [test_unexplained_position_difference_keeps_fact_pending_without_fake_fill](../../../../tests/test_exclusive_position_management.py#L284)：`assert plan(service)['management']['sync_status'] == 'partial'`；`assert exchange.writes == []`；`assert conn.execute('SELECT COUNT(*) FROM execution_fills').fetchone()[0] == 0`

- [test_unknown_hedge_direction_stays_unsupported_instead_of_guessed](../../../../tests/test_exclusive_position_management.py#L466)：`assert plan(service)['entries'] == []`；`assert 'regular:unknown-direction' in plan(service)['management']['unsupported_orders']`；`assert exchange.writes == []`

- [test_verified_external_changes_keep_exit_management_available](../../../../tests/test_exclusive_position_management.py#L204)：`assert len([write for write in exchange.writes if write[0] == 'create']) == 1`；`assert replay == result`；`assert len([write for write in exchange.writes if write[0] == 'create']) == 1`

- [test_verified_external_flatten_reopen_creates_successor_boundary](../../../../tests/test_exclusive_position_management.py#L401)：`assert exchange.orders['regular', 'successor-entry']['status'] == 'open'`；`assert exchange.orders['algo', 'old-all']['status'] == 'canceled' or current['management'].get('pending_orders')`；`assert not [write for write in exchange.writes if write[0] == 'create']`

- [test_bank_net_equity_limits_and_receipts](../../../../tests/test_mcp_paper.py#L60)：`assert result['account']['cash'] == 1300`；`assert paper.operation(caller, '123456', 'borrow-stable')['account']['debt'] == 500`；`assert len(paper.history(caller, '123456')) == 3`

- [test_failed_trade_rolls_back_everything_and_operation_can_be_retried](../../../../tests/test_mcp_paper.py#L181)：`assert paper.get_account(caller, '123456')['spot'] == []`；`assert len(paper.history(caller, '123456')) == 1`；`assert paper.trade(caller, '123456', 'spot', 'ETH/USDT', 'buy', 10, 'new-spot-buy')['status'] == 'completed'`

- [test_live_paper_and_readonly_permissions](../../../../tests/test_mcp_paper.py#L158)：pytest.raises/fixture验证，见函数全文。

- [test_chat_user_rejection_still_stops_writes_but_allows_read](../../../../tests/test_scoped_trade_rounds.py#L50)：`assert json.loads(output[1].content)['status'] == 'not_executed'`；`assert json.loads(output[2].content)['query']['status'] == 'failed'`；`assert len(approvals) == 1 and len(exchange.calls) == 1`

- [test_independent_batch_continues_and_replay_never_sends_again](../../../../tests/test_scoped_trade_rounds.py#L78)：`assert [r['status'] for r in result['results']] == ['failed', 'submitted']`；`assert result['summary']['submitted'] == 1`；`assert len(attempts) == 2`

- [test_independent_batch_unknown_only_blocks_dependent_writes](../../../../tests/test_scoped_trade_rounds.py#L90)：`assert [r['status'] for r in result['results']] == ['unknown', 'not_executed', 'completed']`；`assert result['status'] == 'unknown' and len(exchange.calls) == 3`；`assert len(exchange.calls) == 3`

- [test_same_round_rejection_does_not_block_valid_independent_action](../../../../tests/test_scoped_trade_rounds.py#L30)：`assert [r['status'] for r in results] == ['failed', 'submitted']`；`assert len(attempts) == 2 and len(exchange.calls) == 2`；`assert len(approvals) == 2`
