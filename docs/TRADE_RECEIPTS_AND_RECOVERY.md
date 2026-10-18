# 交易回执与恢复

默认统一请求使用 `contract_version=3`，旧独立退出请求仍接受 `contract_version=2`。扩展回执使用 `receipt_version=1`，旧独立投影由
`backend/utils/trade_receipts.py` 的 `TradeReceipt` 和纯投影函数定义。

## v3 父子身份及模型文本

交易结果前端直接展示操作、标的、方向、委托数量、价格和成功/失败，移除该结果区的原始 JSON、参数树、审计入口和核验说明；一般查询工具保留原展示。成功表示交易所接受该指令，不表示挂单成交，数量不以已成交量替代。后台原始证据、授权审计接口、实际成交/持仓/盈亏更新和原操作恢复保留。历史归属整理和收益重建不在下单或成功回执返回前同步执行，详见 [工具执行耗时](TRADE_EXECUTION_LATENCY.md)。

整批静态检查通过后、首次金融影响前持久化父意图和全部子项映射。子 ID 在可容纳时为 parent:index；最长外部 ID 使用含可信 owner 的有界哈希，并保存 index→child 映射。v3 具有独立请求版本，相同 ID 的不同意图拒绝，v1/v2 原哈希与原始首次回执保持。

模型可见回执每项一行，包含动作、目标订单或标的、必要数量价格、成功/失败及原因、未执行尾项。重复父子 operation_id、client ID、版本、准入树和恢复计数不常规展开；改单的实际新旧订单身份保留。回执不再设置字节硬预算，完整保留订单身份。内部状态判断和审计继续使用结构化回执，MCP `structuredContent` 保留机器消费结构。完整原始回执和后续证据通过授权 HTTP/UI 按需、有界读取。`include=["account"]` 只在整批末尾附一份账户摘要；读取失败不改变已知交易结果。

所有模式和旧金融入口先完成必要准入并保存意图，直接按交易所接口响应返回，不在本项提交后再次读取订单/仓位、轮询或 sleep，超时也不补查。只证明受理时是 submitted，已经证明动作完成时才是 completed；缺少成交数量时内部保持 null，文本不补零。后续新意图依赖当前订单、数量或仓位时，在该新金融调用内完成必要前置检查。

断线、进程中断或父响应丢失后，系统后台按原映射核验已尝试项，不发出 create/edit/cancel，不补平、换 ID、提交未尝试替代或继续批次尾部。未知项内部保留 unknown/pending，文本说明请求失败、提交结果未知、请勿重复提交；可靠成交结果与旧单 cleanup 告警分别保存。Agent 默认只绑定 execute_trade_actions；get_trade_operation、get_operation 及旧查询适配器已移除，没有替代查询工具或隐藏查询动作。后台观察更新当前上下文、界面和成交记录；记忆更新仍仅使用约定的三类素材。用户审计只通过授权 HTTP/UI；账户作用域哈希不能充当 config_id。

内部任务授权由运行上下文提供。live MCP 混合批次先检查全部 trade/cancel 权限，cancel-only 无权在后项放 open；合法权限下动态余额/杠杆只逐项检查。paper 写入仍按 read+trade 的既有权限边界，cancel 属于 paper trade。绑定聊天逐项审批，一个拒绝终止其后金融动作。

任务配置已删除或不可用时，新 v3 金融意图返回 `configuration_unavailable` / `not_submitted`，不回退为模拟交易。原 v3 意图重放须匹配已保存参数、标的和可信引擎，读取已保存的原结果与本地最新证据，不借重放触发提交后交易所核验；后台交接恢复继续使用获准的接收方账户上下文。

v3 父与子映射保存在 `unified_trade_batches / unified_trade_children`，附带实盘原生退出及保护的提交前意图保存在 `unified_native_intents`；现货原生改单同样保存提交前的目标总量和价格。重启及回滚演练须保留这些新增数据、原请求、原订单/客户端 ID 与未知回执。v3 不借用旧 v1/v2 父合并器重建尾部。

显式交接只在原金融 worker 已停止或隔离后接受。接收方在自身可信账户上下文查询获准的原身份，仍保存原执行者历史；父批次先按接受的标的/方向限制子项读取，不返回其他私有操作或原全账户摘要。未知或缺失范围证据仍报告 unknown，不以交接重新提交。规范 side 始终是 LONG/SHORT，原生买卖方向另存 exchange_side。

交接后删除原任务时，接受的恢复责任所引用的父子请求、回执及相关订单/成交审计继续保留原 actor 和 ID；删除响应给出 `retained_for_recovery` 计数。无关私有历史仍清理，保存证据不会扩张接收方查询权限。没有交接引用的普通删除保持原清理行为。

| 操作 status | 含义 | 下一步 |
| --- | --- | --- |
| completed | 本次动作已核实完成；不等于当前仓位为空 | 检查 position、cleanup；下一次交易重新准入 |
| submitted | 订单已接受，仍挂单或等待触发 | 不把挂单描述为成交 |
| pending | 本次动作已有进展，但缺少完成证据 | 系统后台核验原身份 |
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
  "recovery": {"state": "background", "target_operation_id": "example-close-1"}
}
```

1. Agent 读取本次成交结果和具体清理单，不把 cleanup.pending 解释为市价订单未知。
2. 原调用及时返回，后台自动读取原订单/客户端身份并保存观察，不重放原金融写入。
3. 已有独立维护职责与只读回执核验分别运行；回执核验不执行撤建或未尝试尾项。撤销 ACK 缺成交量时保留真实缺项，后台遵守容量保留额度和退避，不拖住已获得 ACK 的工具返回。
4. 相关订单、数量及周期状态核实后，Agent 可以显式提出新的交易。旧批次中
   not_executed 的动作始终保留未执行，不自动补发。

`snapshot.unresolved_orders` 包含终态但成交量待核验的入场/退出单；这些单不算有效保护。
已成交后晚到入场造成的残仓放在 `position`，不会把原 close 改成失败，也不会自动补平。
Hedge 的方向隔离需要当前证据支持；One-way 的共享净仓不能依赖另一方向旧回执放行。

## MCP 与持久化

MCP 外层 principal/profile/operation ID 映射到内部 stable ID；不同调用方不能读取或重放对方的操作。后台恢复和同 ID 重放使用相同证据，原 ID 重放不会产生第二次金融提交。授权 HTTP/UI 详情保留原始/最新版本与观察时间，以固定版本分页；read 权限不产生金融写入。

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
旧接管入口复用共享管理服务并保留数量核验与原操作回执，正常交易无需先调用它。完整授权后自动建立可见资源管理关联，人工来源和历史成交归属保留，不制造虚假入场、成本或收益；只读查询不会认领。暂停、空仓和周期结束保留历史来源，共管不要求先释放或交接。

## 横向验收矩阵

| 流程 / 故障 | 回执与恢复入口 | 限制 / 离线覆盖 |
| --- | --- | --- |
| 市价全平、部分平仓后清理失败 | completed + cleanup.pending；后台核验 | 平仓只提交一次；残仓不自动补平；integration、receipt_recovery |
| open/加仓提交超时、ACK 后落库失败 | 超时 unknown 保留 client ID；已有 ACK 保留 submitted/completed | 不换 ID 重发；不能将本地保存失败改写成交易所拒绝 |
| fixed/all 条件 TP/SL | 等待触发 submitted；母子身份与实际量分开 | 六种类型、LONG/SHORT、Hedge/One-way、数量与通道；real_v2、binance_v2 |
| 普通 LIMIT edit 拒绝/超时 | 明确拒绝 failed；超时 unknown；按原单查证 | 原单保留、最多一次 edit；receipt_recovery |
| 条件 cancel_replace | 旧单 canceled 不代表 edit completed | 替代成功才完成；耗尽/拒绝/超时及重启无重复替代；real_v2 |
| cancel ACK 缺成交、倒退或旧 open | 匹配目标的撤销成功 ACK 保留撤单成功；未明成交量仍缺失 | 后台核验成交事实，不重发撤单；缺少撤销完成证据仍 pending/unknown |
| batch / legacy aliases | 已执行结果保留，尾部 not_executed | 默认停止；explicit independent 仅保留明确失败时的原继续策略，unknown/pending 停止全批 |
| Agent / Chat v3 批次 | 一个执行工具，逐项动态准入 | 默认失败/未知即停止尾部；绑定 Chat 逐项审批，拒绝停止后续写入；unified_trading |
| 旧 Agent / Chat 多工具轮次 | 兼容独立单工具逐项准入 | 原轮次规则不因前项拒绝全局停止；Chat 用户拒绝仍停止后续写入；scoped_trade_rounds |
| MCP 后台核验 / 同 ID 重放 | 外部 ID 关联内部 stable ID，回执同步修订 | principal/profile 隔离，读取零金融写入；mcp_trade_recovery、gateway |
| mock / 旧 all / 其他交易模式 | 共用扩展结构，legacy 保守解析 | mock 账本、旧 all 实际量、现货预算、附带保护均跑全量回归 |
| 缺失周期 | 只读诊断，授权路径恢复完整原周期 | 缺历史、数量矛盾、跨账户/任务或后继周期拒绝；receipt_recovery |

真实未知、旧 all 未核实、读接口持续失败和成交矛盾仍阻止依赖它们的写入。
后台核验使用有限额度和退避；用户可通过授权 HTTP/UI 查看现有审计，不触发交易写入。
本次变更的开发交付与待验收范围见 [implementation.md](../openspec/changes/simplify-memory-and-ordered-execution/implementation.md)。

## 证据边界

该类回归应使用 fake exchange 和临时数据库，不用真实下单验证。用户接管测试和验证后，最终代码不再追加验收；历史回归结果不能代表最终版本全部通过。
没有取得截图原单的线上拒绝代码和完整提交证据；离线修复不表示线上已部署或原仓位已核验。

## 按动作准入，无需先查询解锁

普通 open/close/edit/cancel 在同一次调用内读取必要证据、分类旧失败并评估本次动作。
明确拒绝的止损保留 failed，但不会阻止有效的新止损；没有新增预检令牌、手工解锁或额外模型轮次。
真正未知的金融请求不能通过更换 ID 重发。后台自动继续取证，模型无需第二次调用。

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
| close / 条件 fixed/all | 当前授权管理范围及真实仓位、单笔量、相关未知执行、触发/减仓能力 | 正常在途保护、已知固定档数量冲突、无执行影响失败 |
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
 "recovery":{"state":"background","target_operation_id":"rejected-stop"},
 "query":{"status":"completed","errors":[],"observed_at":123},
 "admission":{"version":1,"decision":"allow","allowed":true},
 "continuation":{"blocked_actions":[],"rules":[],"assessment_state":"verified","advisory":true,"requires_action_assessment":true}}
```

query.status=completed 表示此次取证完成；partial 表示部分事实未返回；failed 表示查询不能完成。
原交易失败保持 failed。无 query 的旧回执不推断查询成功。Agent 运行节点及 UI 分别显示两种状态。
MCP 的每个 blocker 都按当前 principal/profile 单独映射到可访问的外部原身份；
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
是后台取证时间；`projected_at` 和 `receipt_updated_at` 是本地生成时间。后台本地投影返回
`query.status=not_requested`、`source=local_projection`，不构造交易所客户端。
查询部分失败、本地投影或无当前周期时，旧建议仅在 `previous_assessment` 保留，当前
`rules`/`blocked_actions` 为空且 `assessment_state=needs_refresh`；已确认执行事实保留。

添加式 `trade_receipt_watermarks` 保存账户/操作的证据指纹。指纹覆盖原周期订单、清理和成交依赖，
不包含重试时间、投影时间或无关后继周期。重复维护不增加终结历史修订或更新其时间；
`completed + cleanup.pending` 仍随关联证据推进。水位和回执同事务保存，重启后继续使用。
这些操作只写本地证据；实际交易准入、维护撤单和同 ID 幂等仍由执行层负责。

## 工具内取消核验，后台保留（2026-10-15）

金融工具按解析顺序执行，直接使用交易所接口响应，不为本项结果补查订单/仓位、轮询或 sleep，也不重发超时请求；原定 1 秒/最多 3 次核验不实施。下一项必要的权限、目标、持仓数量及归属检查仍保留。后台继续按原操作身份核验已提交请求，不补发未执行尾项。

模型展示使用逐项文本；内部回执和 MCP structuredContent 保留结构化事实。受理成功表示提交成功，并非成交。默认失败停止后续项；显式 independent 保留明确失败时原依赖策略，但提交结果未知立即停止本批。

```text
1. cancel：regular:123，撤单成功。
2. open：ETH/USDT 数量0.08 价格2335，请求失败。原因：网络超时；提交结果未知，请勿重复提交。
3. open：未执行。原因：前序操作失败或结果未确认。
```

`Ownership evidence indexing incomplete` 是本地归属证据未完成，不能写成交易所拒绝或保护单已挂上。必要索引尚未完成时返回“失败，原因：持仓归属证据同步未完成，未提交”；不在工具内等待三轮。索引已完成订单归属发布、仅剩历史周期统计重建时，该统计积压不再阻塞当前成交同步；真实归属冲突、目标或数量缺项仍拒绝。历史迁移和后台核验不因此取消。

管理发现与执行/后台收据更新共享账户内可重入锁；数据库版本比较仍保留，冲突不能靠覆盖旧对象或重发已受理请求解决。

## 回执展示与后台同步进度（2026-10-15）

决策面板和聊天工具结果将 JSON 展示为字段卡片、逐项操作结果及状态标签，支持展开嵌套字段和原始 JSON。旧纯文本继续按 Markdown 展示。零成交量、缺失值和布尔值分别显示，不把“提交”显示成“成交”；核验查询状态与交易执行状态独立保留。

后台恢复逐条处理原操作身份。v3 父批次只汇总已保存的子回执，子操作通过自身扫描游标核验，避免父子重复查询交易所、延迟后续操作。父批次可能在下一轮扫描才显示子操作的新结果。`query.status=partial/failed` 参与后续恢复及失败退避，已完成交易也可继续补齐查询证据；批次汇总保留子查询的部分失败。恢复不重放金融操作。

`MCP budget exhausted at account_rate_limit; request=background-receipt` 表示本地后台恢复的时间预算在账户请求节流阶段耗尽，不等同于交易所返回限流拒绝。`Ownership evidence indexing incomplete` 表示本地订单归属索引尚未完成。PostgreSQL 迁移 `0005_ownership_observation_revisions.sql` 将当前计划的归属索引失效条件限定为索引实际使用的账户、订单身份及交易意图字段变化；核验时间、重试次数、查询错误和成交观察仍完整保存，但不单独重置归属扫描。新增/删除来源、真实身份或意图变化、无法解析的证据仍触发失效；保护事件和成交历史的版本跟踪不变。

升级需按部署流程执行数据库迁移后再启动新版服务；仅更新前端不能修复后台索引。上述修复不保证网络故障或真实证据缺失时立即同步成功，未核验事实仍按未知展示。


### 解除调用链路资源门槛

归属索引取消单轮时间与总记录数门槛，分页继续读到完成。后台恢复不再共享 10 秒截止时间，MCP 响应超时与物理线程分离。交易所同步取消总调用页数预算；记录/HTTP/缓存/Paper 字节门槛与回执展示硬预算移除。迁移 `0006` 取消大计划映射 UNKNOWN 的条件并重扫旧派生状态。真实网络错误、身份变化和无效证据仍明确报告；并发、单次超时、重试、资金预算、归属与幂等保持。详情见 [运行说明](RUNTIME_CAPACITY.md)。
