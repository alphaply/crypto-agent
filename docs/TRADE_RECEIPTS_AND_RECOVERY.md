# 独立交易回执与恢复

独立退出请求仍使用 `contract_version=2`。扩展回执使用 `receipt_version=1`，由
`backend/utils/trade_receipts.py` 的 `TradeReceipt` 和纯投影函数定义。

| 操作 status | 含义 | 下一步 |
| --- | --- | --- |
| completed | 本次动作已核实完成；不等于当前仓位为空 | 检查 position、cleanup；下一次交易重新准入 |
| submitted | 订单已接受，仍挂单或等待触发 | 不把挂单描述为成交 |
| pending | 本次动作已有进展，但缺少完成证据 | 查询原 operation_id |
| unknown | 请求可能到达，尚不能确认提交结果 | 按原 client ID 核验，不换 ID 重发 |
| failed | 本次请求明确未执行或被拒绝 | 依据阶段和原因修正新请求 |
| skipped | 已可靠确认无需执行 | 例如目标方向为空仓 |
| not_executed | 显式依赖未完成、实际依赖未核实，或默认批次已停止 | 恢复不会自动重放本项 |

顶层 `error` 属于本次动作；旧单清理错误在 `cleanup.error`。
`filled_amount`、`remaining_amount` 等无法核实时为 null；最后已知成交量和时间分别是
`last_known_filled_amount`、`last_known_fill_at`，不能拿它们冒充当前最终量。
原请求哈希不变；首次工具回执和模型原文保留，最新投影另存修订。

## 市价平仓被旧单清理阻塞

以下为合成示例，不是现场订单的结论：

```json
{
  "receipt_version": 1,
  "contract_version": 2,
  "operation_id": "example-close-1",
  "action": "close",
  "status": "completed",
  "order_id": "regular:example-close",
  "filled_amount": 0.5,
  "execution": {"phase": "verify", "submission_state": "submitted", "fill_status": "verified"},
  "position": {"side": "SHORT", "amount": 0, "status": "verified", "observed_at": 123},
  "cleanup": {"status": "pending", "orders": [{"order_id": "algo:example-stop", "reason_code": "fill_unverified"}]},
  "continuation": {"blocked_actions": ["open", "close"], "side": "SHORT", "assessment_state": "needs_refresh", "advisory": true, "requires_action_assessment": true},
  "recovery": {"tool": "get_trade_operation", "target_operation_id": "example-close-1"}
}
```

1. Agent 读取本次成交结果和具体清理单，不把 cleanup.pending 解释为市价订单未知。
2. 调用 `get_trade_operation(target_operation_id)`；任务、账户和标的由运行环境注入。
   该工具只查询订单与仓位并保存观察，不 create/edit/cancel，也不恢复仓位归属。
3. REAL 维护任务继续处理已有清理意图，刷新原操作投影。撤销 ACK 缺成交量时最多补查
   3 次、总预算 2 秒；单次请求 timeout 限制为剩余预算。持续不明时保留退避时间和 blocker。
4. 相关订单、数量及周期状态核实后，Agent 可以显式提出新的交易。旧批次中
   not_executed 的动作始终保留未执行，不自动补发。

`snapshot.unresolved_orders` 包含终态但成交量待核验的入场/退出单；这些单不算有效保护。
已成交后晚到入场造成的残仓放在 `position`，不会把原 close 改成失败，也不会自动补平。
Hedge 的方向隔离需要当前证据支持；One-way 的共享净仓不能依赖另一方向旧回执放行。

## MCP 与持久化

MCP 使用已有 `get_operation(profile_id, operation_id)`。外层 principal/profile/operation ID
映射到内部 stable ID；不同调用方不能查询或重放对方的操作。普通交易查询与同 ID 重放
刷新同一份证据，read 权限不产生金融写入。

新增表 `trade_cycle_evidence`、`trade_operation_evidence` 和 `trade_receipt_revisions`
保存跨周期证据及原始/后续回执；MCP 外层有对应 links/revisions 表。迁移为添加式，
不更改原请求哈希，不清空周期。网络读取在事务外，短事务比较版本并拒绝倒退成交/终态。

回滚前暂停交易与维护进程，保留新增证据表和备份。不能混跑两版交易 worker，不能通过
清空未知回执、删除周期或更换 ID 恢复交易。旧二进制忽略新表，不删除证据。
恢复旧服务前必须确认它能解释当前周期记录；否则保持只读。离线演练覆盖备份、旧表读取、
重新升级与证据保留，不代表允许旧版继续管理新格式的在途订单。

## 缺失周期的诊断与恢复

`independent_position_cycle_missing` 保留兼容错误码，`details.reason_code` 区分
`no_history`、`account_scope_mismatch`、`competing_ownership`、`successor_cycle_exists`、
`incomplete_history`、`incomplete_ownership_evidence`、`history_read_unverified`、
`pending_order_race` 和 `quantity_mismatch`。可靠空仓返回 skipped。

原账户、任务、标的、方向及订单归属必须一致；逐单最终成交净额必须完整解释当前仓位。
明确标记历史不完整、存在后继周期、归属冲突或订单仍有竞态时不恢复。
已授权交易入口可修复同一个 episode_id，并原子保存审计；只读查询仅返回诊断。
交易所显示有仓位本身不足以证明归属，人工仓位仍使用已有显式接管流程。

## 横向验收矩阵

| 流程 / 故障 | 回执与恢复入口 | 限制 / 离线覆盖 |
| --- | --- | --- |
| 市价全平、部分平仓后清理失败 | completed + cleanup.pending；查询原操作 | 平仓只提交一次；残仓不自动补平；integration、receipt_recovery |
| open/加仓提交超时、落库失败 | unknown 保留 client ID；已知成交保留 completed | 不换 ID 重发；仓位未同步阻止后续写；integration、independent_exits |
| fixed/all 条件 TP/SL | 等待触发 submitted；母子身份与实际量分开 | 六种类型、LONG/SHORT、Hedge/One-way、数量与通道；real_v2、binance_v2 |
| 普通 LIMIT edit 拒绝/超时 | 明确拒绝 failed；超时 unknown；按原单查证 | 原单保留、最多一次 edit；receipt_recovery |
| 条件 cancel_replace | 旧单 canceled 不代表 edit completed | 替代成功才完成；耗尽/拒绝/超时及重启无重复替代；real_v2 |
| cancel ACK 缺成交、倒退或旧 open | pending；有界补查后保存 blocker | 母子总补查 ≤3 次 / 2 秒；后台遵守退避；主动查询可立即读取 |
| batch / legacy aliases | 已执行项刷新，尾部 not_executed | 默认停止；显式 independent 继续无依赖项；batch、scoped_trade_rounds |
| Agent / Chat 多工具轮次 | 独立模式规范工具逐项准入 | 不因前项拒绝全局停止；Chat 用户拒绝仍停止后续写入；scoped_trade_rounds |
| MCP get_operation / 重放 | 外部 ID 关联内部 stable ID，回执同步修订 | principal/profile 隔离，read 零金融写入；mcp_trade_recovery、gateway |
| mock / 旧 all / 其他交易模式 | 共用扩展结构，legacy 保守解析 | mock 账本、旧 all 实际量、现货预算、附带保护均跑全量回归 |
| 缺失周期 | 只读诊断，授权路径恢复完整原周期 | 缺历史、数量矛盾、跨账户/任务或后继周期拒绝；receipt_recovery |

真实未知、旧 all 未核实、读接口持续失败和成交矛盾仍阻止依赖它们的写入。
后台退避只影响自动轮询；人工/模型只读查询可以提前查证，但不产生金融写入。
测试文件完整路径及桌面/移动证据见本次变更的 `verification.md`。

## 证据边界

本次回归使用 fake exchange 和临时数据库，未使用真实交易或模型联网测试。
没有取得截图原单的线上拒绝代码和完整提交证据；离线修复不表示线上已部署或原仓位已核验。

## 按动作准入，无需先查询解锁

普通 open/close/edit/cancel 在同一次调用内读取必要证据、分类旧失败并评估本次动作。
明确拒绝的止损保留 failed，但不会阻止有效的新止损；没有新增预检令牌、手工解锁或额外模型轮次。
真正未知的金融请求不能通过更换 ID 重发。后台继续取证，模型也可主动查询原操作。

| 证据分类 | 判断依据 | 是否限制依赖动作 |
| --- | --- | --- |
| not_accepted | 写入前失败证明，或明确交易所业务拒绝代码；无 ID/成交/子单等相反证据 | 不加入未解决集合，不查询不存在订单 |
| submission_unknown | 无法确认提交、关联撤建/改单未完成、目标撤销在途或读取失败 | 仅限制依赖其执行结果/身份的动作 |
| live_verified | 活跃订单及成交已核验，或原生条件单可靠等待触发 | 正常存在本身不阻塞；数量冲突单独提示 |
| terminal_fill_unverified | 终态缺最终成交证据 | 不得把 ACK 当作零成交 |
| terminal_verified | 终态和最终成交都确认 | 保留审计，停止无意义轮询 |

异常类型、rejected 字样、无 ID、查不到订单或空仓均不能单独证明没有执行。
新记录保存脱敏 submission_evidence（来源、业务代码、no_execution）；旧记录惰性添加
evidence_classification/version，只有明确来源才恢复。旧错误没有足够证据时保持未知。
缺失周期恢复对确定拒绝免除不存在的订单归属链接，其他成交净额与归属要求不变。

| 动作 | 必需事实 | 不单独阻塞的情况 |
| --- | --- | --- |
| open / 入场 edit | 权限、额度、模式、归属、当前敞口、相关在途写入、跨周期清理 | 旧拒绝、无关方向维护告警 |
| close / 条件 fixed/all | 当前可管理仓位、单笔量、相关未知执行、触发/减仓能力；all 独占归属 | 正常在途保护、已知固定档数量冲突、无执行影响失败 |
| exit edit | 目标归属/身份/成交/修改状态与所需仓位 | 不影响目标或仓位的历史错误 |
| cancel | 目标归属、当前身份、子单/撤建关联 | 无关仓位接口失败、其他目标未明成交 |

Hedge 方向分开评估；One-way 共享净仓事实。未知平仓、触发子单未明、正在撤建的目标仍有真实依赖。
reason_code 包括 order_evidence_unverified、position_evidence_unverified、cycle_cleanup_unverified、
one_way_shared_position_unverified。blockers 给出目标/操作、缺失事实、affected_effect 和 recovery；
refresh 说明系统已尝试的取证。单纯 plan.error 不作准入开关。

## 查询与执行结果的两个维度

原 status 与 receipt_version=1 不变；admission.version=1 记录本次提交前的判断。
continuation 是未来建议，包含 rules、作用 scope、evidence_versions、assessment_state 和核验时间。
目标级 edit/cancel 限制只写 rules；blocked_actions=[] 仅表示没有已知类别级限制，绝不是所有请求获准。
历史 execution/position 属于原 episode，current_scope/continuation 使用当前周期；缺少当前周期时 needs_refresh。

查询示例：

```json
{"receipt_version":1,"contract_version":2,"status":"failed","order_status":"rejected",
 "operation_id":"rejected-stop","action":"close",
 "execution":{"phase":"submit","submission_state":"not_submitted","fill_status":"unverified"},
 "position":{"side":"LONG","amount":1,"status":"verified","observed_at":123},
 "cleanup":{"status":"not_required","orders":[]},
 "recovery":{"tool":"get_trade_operation","target_operation_id":"rejected-stop"},
 "query":{"status":"completed","errors":[],"observed_at":123},
 "admission":{"version":1,"decision":"allow","allowed":true},
 "continuation":{"blocked_actions":[],"rules":[],"assessment_state":"verified","advisory":true,"requires_action_assessment":true}}
```

query.status=completed 表示此次取证完成；partial 表示部分事实未返回；failed 表示查询不能完成。
原交易失败保持 failed。无 query 的旧回执不推断查询成功。Agent 运行节点及 UI 分别显示两种状态。
MCP 的每个 blocker 都按当前 principal/profile 单独映射到外部操作，get_operation 指向对应父操作；
无法映射时身份留空并标记 unavailable_in_caller_scope，不暴露内部操作或借用当前操作 ID。

## 独立批次与显式依赖

v2 批次省略 execution_policy 保留旧哈希和 stop_on_error 行为。显式 independent 按顺序分别准入；
failed/pending/unknown 不阻止无依赖项。depends_on 仅用于此策略，必须为唯一的前项整数索引（从 0 开始且小于当前动作索引），不接受 null、布尔值或字符串；无依赖时省略该字段。即使 `depends_on: []` 也要求显式 independent，`independent_exits` 任务模式不会代替这个批次参数。非法批次在首次交易写入前整批拒绝，错误指向具体动作字段；系统不会自动切换策略或删除依赖。
只有 completed 满足依赖；submitted/skipped 不算完成业务前提。整批结构、权限和已知模式在任何写入前检查。
汇总优先 unknown、pending、failed、submitted、completed；未执行项使整批不能报全部成功，summary 给出逐类计数。
恢复只刷新已执行项，不补发尾项。规范 Agent/Chat 多工具列表也逐项判断，不通过方向或价格猜模型的先后意图；
显式业务顺序应使用 depends_on。Chat 仍逐项审批，用户拒绝后停止本轮后续写入。

独立 TP/SL：

```json
{"contract_version":2,"execution_policy":"independent","actions":[
 {"action":"close","side":"LONG","order_type":"take_profit_market","amount":0.2,"trigger_price":120,"reason":"止盈"},
 {"action":"close","side":"LONG","order_type":"stop_market","amount":0.2,"trigger_price":90,"reason":"止损"}]}
```

显式撤后新建：

```json
{"contract_version":2,"execution_policy":"independent","actions":[
 {"action":"cancel","order_id":"algo:123","reason":"替换保护"},
 {"action":"close","side":"LONG","order_type":"stop_market","amount":0.2,"trigger_price":92,"reason":"新保护","depends_on":[0]}]}
```

兼容批次：

```json
{"contract_version":2,"actions":[{"action":"open","side":"LONG","amount":0.2,"price":100,"reason":"入场"}]}
```

## 维护与并发升级

新动作前只加载/恢复归属和只读取证，不运行全周期撤单/撤建维护；提交后维护和 scheduler 继续推进原清理意图。
若本次只读取证已证明空仓、全部相关订单终态且无未明成交，可当次完成旧周期的本地记账并新建周期，无需等待调度器或额外查询。
维护失败不覆盖已确认的本次执行结果。查询没有金融写入。成功订单/仓位读取只清除对应当前错误，审计历史保留。

添加式 trade_scope_generations 以账户和规范标的记录 generation。提交前短事务比较周期版本与 generation，
保存客户端 ID、在途意图并推进 generation，然后在事务外调用交易所。维护写入同样参加预留。
跨进程旧快照不直接提交；提交前最多重新取证一次，再次冲突返回未提交。可能已发送后绝不重试金融写入。
Hedge 对向刷新后可继续，One-way 仍核实共享依赖。该机制不控制外部人工交易。

升级：暂停交易和维护 worker，备份数据库，运行已有 initialize_schema，整体部署后端、调度器和前端。
迁移幂等，不清空历史；新旧写入 worker 不混跑。回滚保留新表与备份，停用 independent 策略能力；
旧版不能解释新请求时保持只读，由兼容版本处理在途状态。临时库重复迁移、备份回读与保留证据由
test_trade_evidence_concurrency 验证；没有执行线上升级或数据库清理。

### 查询冲突与早期拒单记录

只读查询发生证据版本冲突时，内部重新加载并取证，最多尝试三次；不重放金融写入。
仍无法刷新时保留原交易事实，将旧准入建议归入 previous_assessment，当前 rules 和 blocked_actions 不再复用旧限制。
needs_refresh 表示尚未评估，既不代表全面允许，也不代表禁止新交易；Agent 应直接调用已决定执行的管理动作，由工具检查依赖。
页面不会把 needs_refresh 下的历史限制渲染为当前封锁。

早期版本仅在周期 error 保存拒单原因时，按同账户、周期、操作和客户端身份查原始审计中 submitting 到 rejected 的转变。
只有该次转变包含可确认未执行的交易所拒绝代码，才补充拒绝证据；后续无关错误、查不到订单、单次空仓均不作为替代证明。
原始审计与拒单状态保留，不删除记录、不重新发送旧订单。

已归属挂单只改价格时，无关订单的未知执行不再形成连带限制；目标订单自身的未明修改、归属、当前仓位及交易所限制仍校验。
改变剩余数量时继续核验相关敞口与执行量，不能用改价格路径绕过数量检查。

### 决策证据与时间

`operation_evidence` 按可信任务、账户、标的和原操作 ID 读取，不使用最近页作为引用边界。
批次父回执和子操作使用保存的 index 关联；取更新的同身份结果，未执行尾项不转成成交。
跨账户、不存在或不可读分别返回范围不符、未关联、不可用，不用相近数量或空仓推断解决。

`position.observed_at`、`fill_observed_at`、`verified_at` 是实际观察时间；`query.queried_at`
是显式查询时间；`projected_at` 和 `receipt_updated_at` 是本地生成时间。后台本地投影返回
`query.status=not_requested`、`source=local_projection`，不构造交易所客户端。
查询部分失败、本地投影或无当前周期时，旧建议仅在 `previous_assessment` 保留，当前
`rules`/`blocked_actions` 为空且 `assessment_state=needs_refresh`；已确认执行事实保留。

添加式 `trade_receipt_watermarks` 保存账户/操作的证据指纹。指纹覆盖原周期订单、清理和成交依赖，
不包含重试时间、投影时间或无关后继周期。重复维护不增加终结历史修订或更新其时间；
`completed + cleanup.pending` 仍随关联证据推进。水位和回执同事务保存，重启后继续使用。
这些操作只写本地证据；实际交易准入、维护撤单和同 ID 幂等仍由执行层负责。
