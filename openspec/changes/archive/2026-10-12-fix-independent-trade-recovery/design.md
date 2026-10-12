# Design

## Context

动机见 proposal.md。当前不存在未完成的其他 OpenSpec 变更；本变更承接已归档的独立交易工具 v2，不重新设计交易策略。

### 已确认的代码事实与证据边界

| 路径 | 当前行为 | 影响 |
| --- | --- | --- |
| `independent_exits.close/open` | 提交本次订单后调用整个周期的 `_reconcile`；`_active_plan` 也先整周期核验 | 本次写入前、写入后及旧单清理异常混在同一调用里 |
| `_cancel`、`_record_cancel_fill` | ACK 缺成交量时立即抛异常，未进入后面的补查循环 | 很快可查明的撤单也被直接上抛；该 ACK 不能被当成零成交，原防护必须保留 |
| `_result`、`_v2_result` | `plan.error`、EXITING 等参与单笔 pending 判断 | 一个清理问题污染本次订单回执 |
| `independent_tools.execute_action` | 通用异常只返回 status/version/error；仅 ValueError 保留部分结构错误 | 订单身份、阶段和已确认成交丢失；权限错误或原生 edit 明确拒绝也可能成为 unknown |
| `trade_operations.tool_result_status` | pending 统一转成 unknown，not_executed 转 skipped；字符串兜底猜状态 | 状态含义在适配、批处理、存储和 UI 中失真 |
| `_reconciled_receipt` | 周期有 error 或 EXITING 就拒绝恢复匹配记录 | 单笔已确认成交无法独立修复旧回执 |
| `snapshot` | 终态退出单直接跳过，终态入场单也不在 entries 中 | fill_pending 可能只有总告警，没有可查询的具体单 |
| `scheduler` REAL 维护路径 | 调用 `reconcile_all`，没有串接普通交易持久化回执刷新 | 后台已恢复的事实不保证进入原操作结果 |
| MCP `execute/operation` | 已存在操作直接返回缓存；专门恢复主要针对 adoption | 普通 open/close/edit/cancel 的外层回执可能长期陈旧 |
| Agent/Chat tools 节点 | 按状态停止后续工具；独立模式推荐目录没有按原操作核验工具 | 提示模型“核验原 ID”，但缺少明确可调用入口 |
| `real_protection_plans` | 每任务/标的/方向保存当前周期，新周期覆盖当前行 | 恢复不能只依赖当前周期，需要保留历史身份与证据 |

现有测试已覆盖撤单缺成交、累计量倒退、晚到入场、触发竞态和重启不重复下单，但未闭合“真实回执 -> Agent 停止原因 -> 自动对账 -> 原回执刷新 -> 合法下一笔交易”链路。截屏对应线上调用未出现在本地数据库；本地最新运行记录为 2026-09-07。因此“该笔平仓已成交、具体哪张旧单阻塞”仍未被现场数据确认，方案覆盖两种真实情况，不能据此手工将线上 unknown 改成 completed。

### 模型探测事实（2026-10-12，无凭据）

三组用户提供凭据对 `https://api.bankofai.io/v1`、`claude-sonnet-5.5` 使用同一小型调度题进行受限测试：Chat Completions low/high 均无有效推理文本且报告 reasoning_tokens=0；原生 Messages high 分别报告 145/148/139 个 thinking tokens，流式分别为 143/186/143，但文本全部为空。原生 low 均报告 0。第一组非法 effort 在两个协议也返回 200，说明仅凭成功响应不能证明严格档位执行。项目实际构造请求包含 effort，原生解析器能保留正数用量。

这是网关接口的本次观察，不是所有任务、账户或未来版本的保证，也不能反推线上历史请求。已有文档 `docs/REASONING_AND_TOOL_CALLS.md` 描述了隐藏推理情形，但当前 UI 的无文本且零用量分支不显示任何说明。B AI 的 [模型说明](https://docs.b.ai/llmservice/models/claude-sonnet-5-5/) 和 [API 说明](https://docs.b.ai/llmservice/api/) 是协议核对来源；探测结果与文档不一致时保留“未验证”，不得静默声称支持。

## Goals / Non-Goals

**Goals:**

- 首先交付可验证的市价平仓恢复闭环：已成交事实保留，真实阻塞被定位，证据补齐后可继续交易。
- 使用统一回执投影及准入判断覆盖单项、批次、两个 Agent graph、模拟和 MCP，避免每层分别解释错误字符串。
- 将只读证据收集与已授权维护写入分开，保证新增查询能力不扩大交易权限。
- 提供可独立验收的推理诊断阶段，不延迟交易恢复阶段交付。

**Non-Goals:**

- 不因“提升可交易性”跳过原单核验，不自动补平、重入、重新分配 fixed 档位或接管外部仓位。
- 不把 completed 的市价订单等同于当前仓位为零，不把 canceled 母单等同于子单零成交。
- 不重写现货/附带保护引擎；共用状态解析改动须通过这些模式的兼容测试。
- 不自动切换存量 B AI 渠道，不生成或索取模型隐藏思考来填充 UI，不进行额外实盘下单验证。

## Decisions

### 1. 回执基于本次意图，周期维护独立表达

交易请求保持 `contract_version=2`，新增 `receipt_version=1` 标识结构回执扩展。新增共享投影模块（建议 `backend/utils/trade_receipts.py`），接受已收集证据，不发交易所请求。

保留顶层原有 order_id、order_status、数量等字段，扩展以下部分：

| 字段 | 含义 |
| --- | --- |
| `operation_id / action / episode_id` | 原请求和周期身份；查询工具自身调用 ID 与目标 operation_id 分开 |
| `status` | 本次动作结果，使用规范中的七种状态；`pending` 只针对本次动作，不表示任意周期告警 |
| `execution` | `phase`（preflight/submit/verify/cancel/amend/replace）、`submission_state`（not_submitted/submitted/unknown）、真实数量、核验状态与当前订单身份 |
| `position` | 当前方向数量、observed_at、verified/stale/unknown；订单累计成交与当前仓位分开 |
| `cleanup` | not_required/pending/completed/failed，相关订单、缺失证据与错误 |
| `continuation` | 作用范围、限制动作、reason_code、阻塞身份、assessed_at；只是建议，写入前必须重新核验 |
| `recovery` | 原操作查询入口、目标 ID、待核验项、last_attempt_at、next_retry_at、attempt_count |

例如下面是**合成验收样例，不是截图订单的实盘结论**：

```json
{
  "contract_version": 2,
  "receipt_version": 1,
  "operation_id": "example-close-1",
  "action": "close",
  "status": "completed",
  "order_id": "regular:example-close",
  "order_status": "filled",
  "filled_amount": 0.5,
  "pending": false,
  "execution": {"phase": "verify", "submission_state": "submitted", "fill_status": "verified"},
  "position": {"side": "SHORT", "amount": 0, "status": "verified"},
  "cleanup": {"status": "pending", "orders": [{"order_id": "algo:example-stop", "cancel_confirmed": true, "fill_status": "unverified"}]},
  "continuation": {"blocked_actions": ["open:SHORT"], "reason_code": "cycle_cleanup_unverified"},
  "recovery": {"tool": "get_trade_operation", "target_operation_id": "example-close-1"}
}
```

所有未知最终量使用 null/明确核验标志，最后已知成交另列并带时间。顶层 error 仅承载本次动作错误，清理错误放在 cleanup；避免旧解析器把嵌套告警重新解释为本次 unknown。一次已确认平仓即使后来出现入场残仓也仍是已确认，position 反映残仓。

拒绝“统一改成成功”的方案：它会掩盖真正的提交超时。拒绝仅改善错误文案：它不能修复状态传播、查询和准入。

### 2. 按写入阶段分类，保存发生异常前的事实

服务内部维护本次 operation context，在交易所调用前持久化意图和稳定 client ID；在获得响应时单独记录已知身份/成交，再进行周期维护。异常携带 stage、目标身份、已知证据和明确的错误代码，由统一投影生成回执。

- 提交前的参数、权限、数量校验失败：failed/not_submitted。
- 可验证的交易所拒绝：failed，保留原单；不能把所有 `ccxt.ExchangeError` 不加区分地视为明确拒绝。
- 请求可能已送达而未查明：unknown，查询原 client ID；不新建第二个请求。
- 已知执行过程尚需明确的后续证据：pending；订单正常等待触发/挂单是 submitted，不是 unknown。
- 平仓已确认但 cleanup 不完整：本次 status 独立，使用 continuation 限制相关后续动作。

同时修复 `_submit` 中交易所调用、响应处理和落库处于同一 try 范围的问题：提交后持久化失败不能被描述为交易所拒绝。预写意图支持重启按 client ID 恢复；数据库完全不可写时不承诺已经持久化新证据，向调用方保留能返回的身份并维持保守阻塞。

### 3. 只读收集、维护写入和回执投影分层

在现有服务上提取证据读取和状态投影，不另造完整交易引擎：

```text
Agent / Chat / MCP
        |
        v
original operation --> read-only evidence --> receipt projection
                                ^                   |
                                |                   v
scheduler --> authorized maintenance --> evidence store --> current context
```

只读收集允许 fetch_order/fetch_positions 及必要的成交历史查询，可更新本地核验缓存，不调用 `_reconcile/_cleanup/_replace` 等含交易写入的方法。新内置工具使用 `get_trade_operation(target_operation_id)`；config_id、symbol、account 由可信运行上下文注入。MCP 复用 `get_operation(profile_id, operation_id)`，使用同一读取服务，保留 principal 隔离和 read 权限。不存在/不归属的 ID 返回结构化错误，不泄露其他任务数据。

撤销 ACK 缺成交时先有界补查原身份：默认最多 3 次、总查证预算 2 秒（单请求受剩余 deadline 限制），查明则同次调用恢复；未查明就返回明确 pending，后台按退避继续。后台每个周期都记录尝试时间和具体 blocker，不在模型循环中紧密重试。一个订单读取失败后继续收集其他订单只读事实，写入依赖仍逐项检查。

终态已撤销但 fill_pending 的记录进入 `unresolved_orders`，并在操作查询、账户上下文、MCP 查询和 UI 中展示；不塞回“有效保护”统计。已确认未触发的母单终态可以按经验证的接口语义结束核验；只含 algoId/code 的 ACK 不够，触发/子单冲突优先继续查证。

### 4. 后续动作依据依赖准入，不依据一条全局错误字符串

新增共享准入函数，分离于 `tool_result_status`。每次交易写入前在原账户锁下重新读取证据并核验依赖；不信任先前回执中的 allowed 标记或模型自己的判断。

| 已知状态 | 可继续 | 必须阻止 |
| --- | --- | --- |
| 原市价提交结果未知 | 只读查询；与其无关且重新核验通过的另一作用域动作 | 同方向重复 close、新入场以及依赖剩余量的保护/修改 |
| 原 close 已成交，旧 all 是否仍有效未知 | 只读查询、已授权维护；Hedge 下经独立核验无共享冲突的另一方向 | 可能受旧 all 影响的同方向新入场 |
| 原 close 已终态，晚到入场残仓已核实，相关在途竞态已排除 | 显式对真实残仓减仓/保护、可验证目标撤单 | 自动按旧数量补平；未经核验的新周期 |
| 全部相关终态和成交量、空仓均核验，周期 DONE | 正常新入场，继续正常交易规则 | 无额外历史错误限制 |
| 归属不明或缺失周期且不能恢复 | 查询和诊断；原有显式接管流程 | 自动 adopt、新建假周期、假定可平全仓 |

One-way 模式的对向动作可能共享净仓位，不套用 Hedge 的方向隔离；账户锁和已有跨任务独占限制继续生效。已知固定订单数量冲突仍按现有规范报告，不新增本地总量排他预留。

模型同一轮的交易列表仍顺序执行：本次 failed/pending/unknown 后停止剩余交易项；只读恢复查询可以继续。completed 附带 cleanup.pending 时，对下一交易项做实际准入，拒绝后后续交易项 not_executed，写入前每项都重检。恢复后模型可以在新的决策轮显式选择动作；**不自动重放之前 not_executed 的批次尾部**。聊天交易保留既有审批，新增只读查询不走金融写入审批。

### 5. 原操作证据独立于当前周期，跨入口恢复一致

保留 `trade_action_runs` 和 `trade_operation_requests` 的请求身份及哈希；增加版本化的操作证据/回执修订存储（建议 `trade_operation_evidence`），按 config_id、account_scope、symbol、operation_id、episode_id、order reference 关联。记录首次回执与后续核验修订，维护可单调更新的最新投影；当前周期覆盖不得删除旧操作证据。

写入证据采用短事务和版本比较，网络查询在事务外；账户锁延续现有粒度，多进程通过数据库 compare-and-set 避免旧快照回写。累计成交不倒退；确认事实只能追加冲突说明，不能被旧 open 响应覆盖。证据必须验证账户作用域和意图：cancel 确认目标撤销，edit 核验原生修改或替代结果，不用“旧单 canceled”冒充替代成功。

REAL scheduler 维护结束后投影本次受影响原操作；`run_once` 同 ID 重放只读恢复，不再次调用金融 callback。MCP 将外部 principal/profile/operation_id 与内部 stable_id 做明确关联，普通交易 `get_operation` 和同 ID 重放也更新外层回执。恢复一个批次仅更新已执行项的证据，其未执行尾部保持 not_executed。

首次回执和历史模型原文不改写；UI 增加“当前核验结果”与核验时间，通过 operation_id 关联最新投影。下一轮 Agent 上下文优先注入当前事实，同时保留历史原始消息，避免模型继续相信过时 unknown。

### 6. 缺失周期恢复使用已有订单证据，不把交易所仓位当成归属

诊断读取当前/历史周期、`trade_operation_requests`、订单归属及成交账本，区分 MISSING/DONE/账户不匹配/其他任务冲突/同任务历史可恢复；错误保留 `independent_position_cycle_missing` 并增加明确子原因。

只有稳定账户标识、任务、标准化标的、方向、原周期订单集合与完整成交记录能解释当前仓位，且不存在未核实并发入场/退出、后继周期或其他任务归属冲突时，后台或已授权交易路径才可用原 episode_id 原子修复原周期；不新建交易所订单。只读查询只给出诊断与可恢复证据，不实施周期归属变更。历史不足、分页不完整或数量矛盾一律保留阻塞并列出缺项。人工仓位仍走已有 adopt 授权，不提示 Agent 为消除错误而重复开仓。

### 7. 横向流程检查和验收矩阵

| 流程 | 已识别问题/风险 | 计划验证 |
| --- | --- | --- |
| 市价全平、部分平仓 | 提交后清理错误覆盖成交 | filled + cleanup pending；真实 unknown；晚到残仓；最终恢复交易 |
| open/加仓 | `_reconcile` 可在已提交之后失败 | 不丢身份；accepted 未成交不伪装 filled；重启只查不补 |
| 固定/all 条件 TP/SL | 母单缺字段、触发后子单关联 | waiting_trigger 可正常 submitted；触发未知可定位；数量/方向不变 |
| 普通 LIMIT edit | 明确拒绝上抛通用异常；修改后查询失败 | rejected != unknown；原单保留；超时后原身份核验 |
| 条件 cancel_replace | 旧单成功不代表新单成功 | 子单竞态、目标耗尽、替代拒绝/超时、重启不重复替代 |
| cancel | ACK 缺字段立即抛错；终态待核验不可见 | 有界补查、累计量倒退、终态单证据持续可见 |
| batch/模型多个工具调用 | 各层停止与状态含义不一致 | 规范状态完整保留；阻塞交易但可查询；尾部不自动恢复执行 |
| MCP | 外层缓存恢复不完整 | principal 隔离、内外身份映射、查询/重放一致、read 零交易写入 |
| 共用旧入口/其他模式 | 状态解析改动可能改变原逻辑 | 旧请求哈希、旧 all 数量解释、现货预算与附带保护回归 |

复用 `tests/test_independent_exits.py`、`test_independent_real_v2.py`、`test_independent_binance_v2.py`、`test_independent_receipt_recovery.py`、`test_trade_tool_integration.py`、`test_trade_batch.py`、`test_mcp_tool_contracts.py`、`test_mcp_gateway.py` 等。所有金融回归使用 fake exchange 和临时数据库；测试须断言交易所 create/edit/cancel 调用次数，而不仅断言回执文案。

### 8. 推理诊断独立交付，复用已有解析与原生协议

新增受控 `reasoning_diagnostics` 数据结构：provider/model/protocol、requested_effort、sent_reasoning_parameters、request_outcome、text_available、reported_reasoning_tokens、evidence_state。只允许固定字段，禁止整个 headers/extra_body/prompt 进入日志；extra_body 对推理参数的实际覆盖需反映在最终发送参数中。

在 `build_chat_model` 及每轮实际请求边界记录最终参数，包含 fallback 后实际模型；在已有提取器归一化 usage，持久化到运行、总结和聊天消息可扩展元数据（缺少扩展字段的表使用可空 JSON 列迁移）。ReasoningBlock 在没有文本/正数 tokens 时可依据诊断显示“已请求，未获得有效上游证据”；没有诊断的历史数据不反推。

保留现有 OpenAI 渠道配置。提供原生 Messages 的明确配置说明和按已验证模型能力的参数校验，不按名称自动切协议；对 Sonnet 5.5 的关闭思考、none effort 和采样参数兼容性给出诊断，不沿用所有模型共用的错误承诺。当前三组测试不足以认证所有档位，UI 只显示请求值和证据。

离线样本覆盖 chat 缺字段/空白、native 空 thinking+签名、最终 SSE usage、工具续接及模型回退，签名使用合成值。可选在线诊断每次最多 6 个小请求，输出上限每请求 2048 tokens、超时 45 秒、无自动补测；仅用户显式触发，凭据仅在进程中使用。

## Risks / Trade-offs

- [completed 被误解成周期安全] → 顶层只说明本次动作，所有写入入口统一检查 continuation 依赖；未升级的内部执行消费者不得先行启用新回执。
- [扩大可继续范围造成旧 all 误伤新仓] → 同方向新入场要求旧周期残留订单及成交全部核实；Hedge 隔离也保留共享账户约束。
- [查证带来延迟/限流] → 前台总 deadline、后台退避、按受影响操作投影；不因缺少 reasoning 再请求模型。
- [历史数据不足，线上个案无法自动修复] → 明确缺失证据，不把“可诊断”承诺为所有历史仓位都可自动接管。
- [多进程及重启期间证据竞争] → 短事务、版本更新、稳定 client ID，测试旧快照、重启和并发查询。
- [共用状态归类改变现货行为] → 版本化结构回执优先，legacy 解析保守兼容；共用消费者成套更新并回归。
- [网关响应可能变化] → 保存脱敏测试时间/协议，展示观测事实，现有渠道不自动迁移。

## Migration Plan

1. 先添加可空/新增表的证据存储和兼容读取，备份现有数据库；不重算历史请求哈希、不修改订单身份。
2. 以当前周期和可验证历史为来源惰性补齐证据。历史缺字段记 unknown，不填造成交量，不批量重置旧周期。
3. 将服务投影、状态归类、Agent/Chat 执行节点、MCP 和 scheduler 作为交易阶段同一发布单元；先通过 fake exchange 的市价平仓端到端验收，再做只读现场核对。
4. UI 对旧/新回执都兼容；保留原始历史消息，新增最新核验展示。平仓阶段可以先交付，模型诊断阶段随后交付。
5. 回滚时先暂停受影响自动交易维护，停止新旧进程混跑，保留新增证据表及审计，不删除或回退交易所订单。仅当旧版能解释当前周期记录时恢复旧服务，否则保持只读诊断直到兼容版本恢复；绝不为回滚重发交易。

## Open Questions

- 线上截图对应的任务、交易所、订单原始 ACK 与周期事件尚未取得；实施时通过原 tool-call/operation ID 和任务时间窗只读核对，可确定该个案是提交前阻塞还是提交后清理。此证据决定历史数据修复对象，不改变本方案的状态、权限或验收范围。
- 具体网关对 xhigh/max、工具轮次隐藏摘要的表现未全部探测；本期只保证参数可观测与事实展示，不把未测试档位列为已验证能力。
