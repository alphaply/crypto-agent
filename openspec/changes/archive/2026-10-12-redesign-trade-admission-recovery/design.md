# Design

## Context

动机及用户确认范围见 proposal.md。现有代码的主要耦合点：

- `trade_receipts.unresolved_orders` 把无 ID 记录全部视为未解决；`IndependentExits._collect_order_evidence` 却跳过无 fill_pending 的 rejected 终态。此前内存复现得到同一证据下 `blocked_actions=[]` 和 `assess_admission(..., close).allowed=false`。
- `project_receipt` 根据本次 status 与 cleanup 推断可继续动作；`independent_admission` 根据整个 plan 的 blockers、error 和 state 另行判断，两套规则会分叉。
- `_active_plan` 在准入前调用含清理/撤建副作用的 `_reconcile`；cancel/edit 后也可能被其他订单维护错误覆盖本次结果。部分所有权检查会读取整个标的历史，不能只改最后的 `_admit`。
- batch、Agent、Chat 各自维护停止开关；旧规范明确要求失败停止。新增独立策略必须显式区分，不能静默改变已有批次。
- `diagnose_cycle` 要求每条记录有 ID 并逐条查询，导致明确拒绝也污染历史完整性；已有操作证据、回执修订及版本 CAS 可复用。

## Goals / Non-Goals

**Goals:** 形成一个纯证据分类器和一个动作准入评估器，取证、写入、维护和显示复用其结论；每个拒绝都有可定位事实和恢复路径。让“已拒绝的止损 → 查询 → 新止损”可正常完成，并让真正未知的同方向平仓仍只提交一次。

**Non-Goals:** 不引入通用工作流引擎、策略风控评分或 force 参数；不自动纠正价格/数量、撤其他保护、补平或重放旧未执行项。交易所适配的 fixed/all、reduce-only 和普通/条件身份保持既有规范。现货及附带保护模式保持原行为。

## Decisions

### 0. 将正常交易的调用成本作为验收约束

Agent 只需表达交易意图。标准 open/close/edit/cancel 不新增必填预检、恢复令牌、手工解锁或先查后下的协议；仅必要的显式业务依赖才使用批次 depends_on。服务内部主动完成本次必要的只读取证、旧拒绝分类和版本刷新；可以在预算内解决的前置状态，当次调用解决并继续原请求，不退回模型多消耗一轮。取证仍有界，未知金融写入绝不重放。

阻塞必须同时回答“哪项硬约束/事实、为什么影响本次动作、系统已尝试什么、还缺什么”。不能用“更稳妥”“前项失败”“顺序看起来含糊”或单纯同方向作为额外拒绝原因。相同证据下加入无关失败记录，原本可执行的动作必须仍可执行；这个不变性进入测试。正常路径验收要求一次模型工具调用可完成提交，不需要额外 get_trade_operation；故障路径验证内部刷新后可当次继续，或一次返回具体且可操作的限制。

### 1. 用执行事实分类，而非通过错误文本推断限制

在交易证据层引入纯分类结果：`not_accepted`、`submission_unknown`、`live_verified`、`terminal_fill_unverified`、`terminal_verified`；改单/撤建另外保留阶段及原单、替代、子单关联。分类不访问网络或数据库；输入包括原请求阶段、提交结果、订单状态、实际成交证据及关联写入。

`not_accepted` 必须能证明该次尝试没有执行影响：本地写入前校验失败，或适配器可确认的交易所拒绝且没有相反成交/子单证据。不能仅凭异常类名、rejected 标签、无 ID、查单不存在、过期或空仓推断。新请求持久化脱敏拒绝来源、阶段和代码；不保存认证头或完整私密请求。旧字段 `submission_state=not_submitted` 必须结合产生路径与已保存证据评估；无法证明的旧记录保持待核验，明确缺项。

已证实无执行影响的历史拒绝保留 failed 和拒绝原因，退出未核验集合及交易所轮询；不伪造 exchange fill_verified，不要求不存在的订单归属链接。改单拒绝只结束该次修改意图，原活跃单仍参与依赖；替代失败保留旧单撤销及新单结果两个事实。所有查询、snapshot、周期恢复和后台维护使用同一分类器。

选择此方案而非仅添加 `status != rejected`：后者会放过终态但关联执行仍未知的记录，也不能处理“有 ID 但成交未知”。

### 2. 动作携带真实效果和目标，限制来自依赖交集

内部 `ActionIntent` 包括动作、账户/任务/标的/方向、目标身份、fixed/all、数量/价格变化及 effect：增加敞口、立即减仓、条件保护、修改入场、修改退出、撤销目标。由可信上下文和已核验目标推导，模型不能自行声明低风险效果。

`EvidenceSnapshot` 保存按对象划分的事实、未知项、观测时间和版本。取证失败属于具体订单/仓位/归属/账户模式，不再仅写入全局字符串。纯评估返回 `allow | allow_with_warning | deny | not_evaluated`、依赖证据和原因；`plan.error` 作为历史诊断保留，不直接否决。

| 意图 | 必须新鲜核验的依赖 | 不应单独阻塞的事实 |
| --- | --- | --- |
| open / 增仓 edit | 权限与额度、模式、当前敞口/归属、相关未知提交；跨周期时旧 all 与清理 | 已确认拒绝、无关方向维护告警、已知数量冲突 |
| market/limit close | 可管理仓位和单笔数量、目标方向、相关未明执行/在途减仓竞态、适配器减仓语义 | 无执行影响的旧失败、无关历史维护 |
| 条件 fixed/all 保护 | 上述仓位与竞态、触发能力；all 额外要求独占归属及原生能力 | 明确拒绝的新单、已核实正常在途且无冲突的其他订单 |
| edit 目标 | 身份/归属、目标成交和修改状态；按 entry/exit 与字段变化补相应敞口/仓位依赖 | 不影响目标或所需仓位的其他订单错误 |
| cancel 目标 | 权限、目标归属/当前有效身份、关联替代/子单竞态 | 无关仓位接口错误、其他目标成交待核验 |

同方向未知平仓、未核实触发子单或未完成撤建仍阻止依赖其数量/身份的动作；不能仅因新动作减少风险就跳过证据。Hedge 可隔离已证实无共享冲突的方向，One-way 依赖共享净仓；跨任务独占与账户权限仍是硬边界。已知同时存在的固定退出数量冲突继续按现规范提示，不增加本地预留限制。

`guard.assert_position_owner` 保留权限与独占限制，拆出作用范围及所需证据供评估器使用，避免目标撤单误依赖全标的持仓历史；确需完整归属的动作仍核验完整历史。历史分页/来源缺项要给出具体诊断，本次不将缺页数据猜成完整。

选择按依赖而非“close 全部放行”或策略宽松开关：后两者无法防止未知市价单重复执行，也不能解释目标级限制。

### 3. 取证、准入、执行、维护分层

```text
request --> validate/authorize --> collect required evidence
                                      |
                                      v
                               assess action intent
                                |             |
                              deny          allow
                                |             |
                             receipt    reserve intent --> submit once
                                                         |
                                                         v
                                               verify own execution --> receipt

scheduler --> collect evidence --> assess persisted maintenance intent --> maintain
read query --> collect evidence --> project receipt/advisory (no exchange writes)
```

`_active_plan` 拆开加载/诊断恢复与整周期维护；新动作前只收集依赖证据，不隐式恢复其他订单撤建。缺失周期的已授权恢复保留原 episode、版本和完整归属条件。完成本次请求必需的 cancel-replace 是本次动作自身阶段，继续可恢复。

提交后的本次核验与周期维护隔离，维护异常转独立告警；scheduler 继续既有清理义务。若用户请求的是新周期而清理未完成，返回具体依赖并由维护推进，不在 open 的前置取证阶段夹带撤单。既有有界前台核验和后台退避保留；无关读取不占用本次必需取证的重试预算。成功读取清除对应旧 query_error/position_error，历史错误仍留审计。

并发：继续账户进程锁和短事务 CAS；提交前在短事务中核对依赖版本并持久化稳定客户端 ID 与在途意图，然后才调用交易所。补充账户+标准化标的的持久化 generation 行（添加式表），所有规范写入及维护写入在同一事务更新 generation 和受影响周期版本。其他进程不能用预留前的快照发送请求，必须重新取证；看到在途意图按未知范围限制。generation 变化最多触发一次提交前重新取证，再冲突则返回未提交及明确原因；请求可能已发送后禁止自动重试金融写入。跨方向并发不因 generation 改变永久阻塞，经重新核验可继续。网络不放在数据库事务中；本机制不宣称可以阻止交易所外部人工成交，仍依赖提交前检查和原生减仓约束。

选择分层而非捕获 `_reconcile` 的所有异常后继续：后者会吞掉实际相关竞态和隐式维护副作用。

### 4. 回执保留执行结果，新增结构化准入与查询结果

保持 `receipt_version=1`、规范动作 contract_version=2 及原 status 枚举；添加 `admission.version=1`。`admission` 记录本次请求提交前的判断，含 decision、intent、scope、evidence_versions、assessed_at、blockers、warnings。每条问题有 reason_code、operation/client/order 身份（可缺但不能全为空而无缺项说明）、所影响 effect/target 及 recovery 建议。建议限于查询原操作、等待核验、修改参数、刷新凭据或显式处理归属，不含自动交易承诺。

`continuation` 表达当前证据下对未来动作的限制规则，新增 `assessment_state=verified|needs_refresh`、按目标/效果的 rules 和 scope/version。与准入共用评估器，不能再从原 status 推断。旧 `blocked_actions` 只投影当前明确作用范围内整体被阻塞的动作类别；目标级限制放 rules，并附 `requires_action_assessment=true`。空数组只表示没有已知类别级限制，绝不等于所有参数和目标获准；内部消费者升级为读取结构化规则，每次执行仍重新判断。

`get_trade_operation` 和 MCP 查询添加 `query.status=completed|partial|failed`、errors 和 observed_at；顶层 status 继续表示原交易结果，避免破坏旧客户端。查询执行节点/UI 使用 query.status 展示此次工具读取结果，单列“原交易已拒绝”。旧回执无 query 时不推断查询成功；缺少当前作用范围证据的 continuation 标为 needs_refresh。

历史操作的 execution/position 继续绑定原 episode；当前可用性用单独 current scope，并通过现周期只读依赖取证后提供，不能把旧周期允许状态用于新周期。MCP 每个 blocker 的内部操作身份分别通过当前 principal/profile 的映射解析，不使用当前 stable_id 的字符串替换猜其他操作，也不暴露其他任务记录。

前端在当前核验区显示简短原因、可继续的动作范围及下一步；JSON 保留折叠。Agent 上下文注入相同结构化事实，更新工具说明，不要求模型读长错误串判断是否能下单。

选择添加式字段而非改写顶层 status 为查询成功：后者会让失败的交易被旧客户端误报成功。

### 5. 顺序执行与依赖控制分离，并保留旧契约

v2 envelope 新增 `execution_policy=stop_on_error|independent`。省略时保留原请求形状和哈希；不能在旧请求序列化中自动填默认字段。显式字段参与哈希，即便值为默认值也不得悄悄复用不同形状的既有 ID。

仅 independent 的动作项可带 batch 专用 `depends_on: list[int]`，去除该字段后再传规范动作；单工具不增加此字段。每项索引严格引用更早的项、无重复、不接受布尔值；依赖成功仅指 completed（submitted 不代表成交，skipped 也不自动证明业务前提），再通过真实依赖准入。整批静态/权限/模式预检保留；业务状态逐项刷新。

独立批次按顺序评估所有项：显式依赖未达成或运行时准入冲突为 not_executed，附 `blocked_by_indices` 和具体证据；本项尝试明确拒绝仍 failed。继续下一项不代表重试失败项。不声明 depends_on 表示调用方请求各项独立评估，系统仍强制补上实际执行依赖。聚合 status 沿用优先级 unknown > pending > failed > submitted > completed，有 not_executed 且无更高状态时用 failed，补 summary counts；不新增会被旧消费者误读的“全部成功”状态。

旧批次和默认 v2 完全保留失败停止。父/子稳定 ID、MCP 哈希、请求存储和重放都保留原策略和依赖，旧 not_executed 永不自动补发。

内置独立模式规范多工具列表按顺序对每个已请求动作独立准入。明确拒绝的 TP 后可继续保护已持有仓位；前项失败后的同方向新入场或其他操作，也必须依据当前权限、敞口、目标及执行证据判断，不能猜测模型原本想等待前项成功。明确的业务顺序用批次 depends_on 表达；原工具已经持久化的撤建关联属于具体执行依赖，继续保留。缺少本次必需证据时先内部有界补查，仍不能确认再给出具体 not_executed 原因。不要通过相似价格/方向猜测所有调用是重复单。旧工具兼容路径仍按原策略；Chat 用户拒绝审批仍停止本轮后续金融写入，每个继续项保留既有审批和提交前重新核验。SPOT_DCA、附带保护路径不扩大执行范围。

选择显式策略而非默认改变所有批次：调用方可能依赖原有停止保证；也不引入任意 DAG，只支持已有顺序列表的前向依赖。

### 6. 历史修复通过投影和有界维护推进

读取旧证据时惰性分类，记录 classifier_version、classification_source 与相关证据引用；可更新最新投影但原回执修订不可变。确定拒绝从 blocker 集合退出，真实订单仍需完整核验。缺失周期恢复只对可能产生执行的记录要求交易所身份和归属链接，拒绝记录留在原周期历史里。

一条 query_error 的消失不自动证明整个周期安全；本次动作所需依赖全部满足才允许。不设置“失败超过 N 次自动解除”，不删除旧 error 来伪装恢复，不修改原请求哈希。

## Risks / Trade-offs

- [模型需要先后依赖但未表达] → 在工具说明中提供 depends_on 示例，执行持久化的明确依赖及真实状态约束；不通过猜测意图设置隐藏的全局停止条件。
- [旧 rejected 的证据不足] → 分类保守且指出缺项；不保证所有线上历史记录能自动恢复。
- [查询建议与写入间发生变化] → 证据版本、在途意图预留、generation CAS 与提交前取证；回执解释变化。
- [分层后后台清理遗漏] → 保留 scheduler 调度、原维护意图和重启验收，确认成交不等于周期完成。
- [旧消费者只读 blocked_actions] → 保持参考性质、能力声明和整体发布；服务端准入不能依赖客户端放行判断。
- [所有权取证仍需较大历史范围] → 仅在真正依赖归属的动作读取；完整性不足不伪装允许。历史分页扩容不是本次承诺。
- [更细检查增加实现量] → 只保留一份分类和评估逻辑，复用既有 SQLite 证据及 fake exchange，不新增外部服务。

## Migration Plan

1. 先完成添加式证据字段、scope generation 表、分类/评估及回执模型；旧请求序列化不变。备份数据库，准备旧记录样本与回滚演练。
2. 作为一个发布单元接入规范交易服务、维护、查询、批次、Agent/Chat、MCP 和 UI；所有金融写入路径遵守同一版本/在途预留规则，再启用新规范工具轮次行为及 independent 能力声明。
3. fake exchange 与临时数据库验收后，部署只在独立模式生效；现有批次仍 stop_on_error，历史拒绝惰性修正，不运行批量清空脚本。
4. 如需回滚，暂停相关交易/维护 worker，保留新表、回执及备份，停用 independent 能力；旧版本无法解释已接收新策略请求时保持只读并使用兼容版本恢复，不由旧 worker 接手在途操作。禁止新旧写入 worker 混跑。

## Open Questions

- 截图原止损的真实拒绝代码和线上保存的阶段证据尚未取得。实施时可按原操作身份做单笔只读核对；它决定该历史记录能否自动分类，不改变新规则或离线验收，也不作为通过本次设计验证的前提。
