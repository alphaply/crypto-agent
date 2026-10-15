## MODIFIED Requirements

### Requirement: Ordered batch uses the same action contracts
`execute_trade_actions` SHALL 以 contract_version=3 提供默认统一批次，以 contract_version=2 保留独立退出规范批次，均使用 action 判别的平铺动作、每批 1 至 20 项。无版本批次 SHALL 按旧契约解析；混合格式在写入前拒绝。整批静态、权限及已知模式检查 SHALL 在首次提交前完成，执行 SHALL 按顺序逐项重新准入，已执行结果不回滚。省略 execution_policy 或指定 stop_on_error 时，failed、pending、unknown 或被阻塞项 SHALL 停止后续交易项并标为 not_executed。支持的显式 independent 策略 SHALL 只跳过依赖未满足或准入失败的项，继续评估其他项；依赖和策略 SHALL 持久化为请求意图。提交入场不等于等待成交；已确认操作附带清理告警不得代替下一动作的实际准入。 工具调用 SHALL 直接使用交易所接口响应形成成功或失败结果，不追加本项提交后的查询、轮询或等待核验；提交结果未知 SHALL 立即停止本批尾项，包括 independent 批次，并保留原身份供后台核验。明确失败时既有显式 independent 依赖语义保持不变。

#### Scenario: Entry is still unfilled
- **WHEN** 默认停止策略的批次先提交限价 open，后续 close 所需的已成交仓位仍不存在
- **THEN** close 不预挂为未来仓位的退出单，报告失败；后续动作标记 not_executed，之前入场回执保留

#### Scenario: Cancellation permissions in a mixed batch
- **WHEN** live MCP 批次包含 open 与 cancel
- **THEN** 系统在首次提交前同时校验 trade 与 cancel 权限；纯撤单批次使用 cancel 权限

#### Scenario: Flat legacy cancellation is not a version signal
- **WHEN** 无版本的旧批次只包含平铺 cancel 动作
- **THEN** 仍按旧契约解析和重放；显式 v2 批次缺少新字段时不得回退为旧格式

#### Scenario: Cleanup completes before the next entry
- **WHEN** 前项市价平仓已成交，执行下一笔新入场前已确认旧周期归零且所有残留订单清理完成
- **THEN** 下一项通过正常权限与敞口检查后执行，不因历史 unknown 文本继续阻塞

#### Scenario: Old close-all order can affect a new entry
- **WHEN** 默认停止策略中平仓本身已确认，但旧周期的条件 all 是否仍活跃未核实，后项请求同方向重新开仓
- **THEN** 后项及之后交易项标记 not_executed，指出旧单身份和需完成的核验，不伪称本次平仓未成交

#### Scenario: A rejected take profit does not suppress independent protection
- **WHEN** independent 批次的止盈被确认拒绝且无执行影响，后项止损无显式前置依赖，且当前仓位和相关订单证据满足准入
- **THEN** 后项止损执行，分别保留止盈失败与止损真实结果，不将整批描述为全部成功

#### Scenario: Explicit dependency preserves cancel-then-create intent
- **WHEN** independent 批次中新建项以 depends_on 引用前面的撤单项，而撤单失败、待核验或尚未 completed
- **THEN** 新建项标为 not_executed 并指出依赖项，其他独立项继续评估；不能因静态参数有效而提前建单

#### Scenario: Invalid policy or dependencies fail before any writes
- **WHEN** 策略不支持、旧版本携带新策略、depends_on 含非整数/布尔值、重复/越界/自身/后向索引，或在 stop_on_error 策略中使用 depends_on
- **THEN** 整批在首次写入前拒绝；independent 的依赖只允许引用索引更小的项并要求其 completed

#### Scenario: Unknown execution still blocks dependent actions in independent mode
- **WHEN** independent 批次前项平仓结果未知，后项需要同方向剩余仓位，即使没有声明 depends_on
- **THEN** 后项仍因实际执行依赖标为 not_executed；independent 不能绕过正常准入

#### Scenario: Replay never resumes skipped tail
- **WHEN** 同一批次操作 ID 在恢复后重放
- **THEN** 仅刷新已执行项证据，原 not_executed 保持未执行，不补发任何尾部；变更策略或依赖复用同 ID 被拒绝

#### Scenario: Paper cancellation retains its existing financial permission
- **WHEN** paper 批次包含撤单或其他金融动作
- **THEN** 使用该 paper namespace/user 的 read 与 trade 权限校验，不额外要求 live cancel scope；内置任务仍使用可信运行授权与审批

#### Scenario: Third-version mixed actions retain caller order
- **WHEN** 合法 v3 请求按 cancel、edit、open 顺序提交三项动作
- **THEN** 系统按给定数组顺序执行并返回三项对应结果，不自动重排、不并行金融写入，也不要求模型在每项之间再发一次调用

### Requirement: Ordered execution remains a bounded foreground request
v3 SHALL 在一次有界调用中逐项执行已声明动作，默认失败、pending、unknown 或未满足依赖后停止并保留未执行尾部；已执行结果 SHALL 不回滚。显式 independent SHALL 仅按已公布能力接受严格的此前 completed 索引依赖，submitted 不满足依赖。系统 SHALL NOT 等待入场未来成交、解释动态前项结果引用、启动后台交易计划、后台续跑未执行尾部或为重试生成新身份。 工具调用 SHALL 直接使用交易所接口响应形成成功或失败结果，不追加本项提交后的查询、轮询或等待核验；提交结果未知 SHALL 立即停止本批尾项，包括 independent 批次，并保留原身份供后台核验。明确失败时既有显式 independent 依赖语义保持不变。

#### Scenario: A failed second item does not erase the first
- **WHEN** v3 首项撤单 completed、第二项新建被明确拒绝、第三项尚未尝试
- **THEN** 批次保留首项 completed、第二项 failed 和第三项 not_executed 的真实结果；总体失败不抹去已成功撤单，不重发前项或隐式回滚

#### Scenario: Submitted entry does not satisfy a close dependency
- **WHEN** 显式 independent 批次中的限价 open 为 submitted，后项 close 通过 depends_on 引用该项
- **THEN** 后项标为 dependency_not_completed 且不执行；调用返回当前结果，不等待成交或将 close 注册为后台未来计划

#### Scenario: A dynamic result reference is rejected before trading
- **WHEN** v3 动作以动态表达式引用前项生成的 order_id、成交量或账户字段
- **THEN** 系统在首次金融写入前拒绝不支持的参数形式，不解释任意代码或隐式结果替换

#### Scenario: Recovery does not issue an unattempted tail
- **WHEN** 网络中断或进程崩溃后恢复 v3 父操作证据，原批次部分子项未执行
- **THEN** 恢复仅核验原已提交身份和事实，保留未执行项；新的管理意图须有明确新请求，不能由后台恢复自动续跑尾部

### Requirement: Financial calls return authoritative acknowledgements without secondary model queries
所有模式及新旧兼容金融入口 SHALL 在一次调用内完成必要的前置权限、目标、仓位、管理及动态准入并执行 open/close/edit/cancel/protect。获得权威受理 ACK 后 SHALL 及时返回 submitted，不为取得返回许可再次 GET 订单/仓位或等待未来成交。ACK 已证明本次动作完成时可报告 completed；已确认成交直接保留，缺证据不能填假零。未确认写入保留 pending/unknown 和原身份。正常后续意图 SHALL 在自身调用内核验所需依赖，不强制模型二次取证。 工具调用 SHALL 直接使用交易所接口响应形成成功或失败结果，不追加本项提交后的查询、轮询或等待核验；提交结果未知 SHALL 立即停止本批尾项，包括 independent 批次，并保留原身份供后台核验。明确失败时既有显式 independent 依赖语义保持不变。

#### Scenario: An accepted limit entry returns before filling
- **WHEN** open 的限价入场获得有效订单 ACK 且尚未成交
- **THEN** 原调用直接返回 submitted 与订单身份，不追加成交轮询或为返回再次 GET 订单/仓位，模型不调用操作查询

#### Scenario: Conditional protection is accepted but waiting
- **WHEN** close/protect 的条件退出获得权威受理 ACK，订单仍为 waiting_trigger 且确认未成交
- **THEN** 返回 submitted、waiting_trigger、filled_amount=0 及必要身份，不因受理报告 completed，也不等待触发或要求模型补查

#### Scenario: A new intent requires current position evidence
- **WHEN** 新的 close/edit/cancel 请求在提交前依赖最新仓位、目标或相关原提交事实
- **THEN** 执行层在本次调用内按预算完成必要只读核验，满足条件才提交；该前置核验不能被误当作禁止的 ACK 后二次取证

#### Scenario: The acknowledgement already proves the requested outcome
- **WHEN** 本次 ACK 本身可靠证明实际成交或指定 edit/cancel 动作已完成
- **THEN** 直接报告相应已确认事实与真实状态，无需为了返回再次 GET；清理或累计成交仍未知时单独表达，不虚构完成范围

#### Scenario: A removed operation-query adapter is called directly
- **WHEN** 调用者尝试 get_trade_operation/get_operation 或其旧兼容工具入口
- **THEN** 注册和可直调适配器均不再提供该能力，入口明确不支持且不读取交易所或产生金融效果；后台内部核验服务及授权 HTTP/UI 审计仍可用

### Requirement: Compact model receipts retain actionable execution evidence
默认模型金融工具结果 SHALL 以简短逐项文本呈现动作、目标、必要数量价格、提交成功、失败及原因或未执行。接口超时 SHALL 显示请求失败及原因，并简述提交结果未知、请勿重复提交；已接受挂单不表述为成交。内部完整结构回执、原身份、版本和恢复证据保留供状态计算及授权审计；默认文本不附嵌套诊断、重复 ID、空字段或恢复计数。单项 UTF-8 不超过4096字节，整批不超过32768字节。

#### Scenario: Compact timeout retains a recovery target
- **WHEN** v3 子项提交后超时且已有客户端身份、尚无交易所订单 ID
- **THEN** 模型文本仅返回“open 请求失败，原因：网络超时，提交结果未知，请勿重复提交”及必要动作参数；父子 operation_id、客户端身份和 unknown/pending 保留于内部证据供后台核验，不在工具调用内查询或等待

#### Scenario: Compact edit keeps replacement outcomes separate
- **WHEN** edit 已确认撤旧，但替代单被拒绝
- **THEN** 模型文本保留必要旧单身份、已知替代订单身份及“撤旧成功、替代下单失败”和具体原因；客户端身份、edit_method 及完整错误码保留于内部证据，不因旧单 canceled 宣告 edit completed

#### Scenario: Full evidence remains accessible after projection
- **WHEN** 用户在现有权限范围通过 HTTP/UI 请求原操作完整详情
- **THEN** 返回完整原始回执、增量证据及真实观察时间；默认投影不删除请求、成交、失败阶段或身份，详情不是模型/MCP 工具且不恢复金融写入

#### Scenario: A legacy conditional close has a compact factual acknowledgement
- **WHEN** 旧 v2 close 回执包含 repeated previous_assessment、规则 blocker 且条件单已受理尚未触发
- **THEN** 默认单项 UTF-8 不超过4096字节，原始回执保持完整；展示 submitted、waiting_trigger 和有证据的 filled_amount=0，不写 completed 或指示二次模型查询

#### Scenario: A full batch respects the aggregate result budget
- **WHEN** 最多二十项动作返回订单身份、异常和一次可选账户摘要
- **THEN** 整个默认序列化 UTF-8 结果不超过32768字节，保留每项真实状态与必要身份且不逐项复制账户、规则或 blocker；不能靠截断原身份或伪造成功满足上限

### Requirement: Admission receipts provide scoped decisions and recovery instructions
交易回执及后台状态 SHALL 提供版本化准入事实，区分允许、带告警允许、拒绝与未评估，保留动作/目标范围、证据时间、具体阻塞及简短告警。后台关联 SHALL 使用调用者可访问的原身份，默认结果说明系统核验状态，不提供模型操作查询入口；用户审计走授权 HTTP/UI。建议不授予新交易权，执行仍重新核验。blocked_actions SHALL 不把目标限制扩大为全局禁止，空列表不表示全面允许，相同动作和证据使用一致结论。 内部结构回执保留上述信息；默认模型文本仅保留本项结果与必要原因，不输出准入树、恢复计数或重复身份。

#### Scenario: Diagnostic error is only a warning
- **WHEN** 旧提交已确认失败且无执行影响，新请求全部实际依赖满足
- **THEN** 新请求允许或带告警允许，保留旧拒绝原因但不返回 trade_recovery_required，不要求模型先查询解锁

#### Scenario: A missing order ID still has a usable recovery identity
- **WHEN** 真正未知提交尚无交易所 ID
- **THEN** 内部保留原 operation_id、客户端身份和具体缺项；模型文本说明该动作请求失败、具体原因、提交结果未知及不可重复提交，不展示空订单 ID 或二次操作查询指令

#### Scenario: MCP recovery identity is accessible to the caller
- **WHEN** MCP 回执引用阻塞的其他操作
- **THEN** 只返回当前 principal/profile 可访问的外部身份和具体后台核验状态，不泄露内部身份或误映射到当前 ID；映射不可得时说明缺项，不提供模型查询入口，用户可按原权限查看 HTTP/UI 审计

#### Scenario: Admission changes between query and write
- **WHEN** 后台观察或正常记录读取后发生新成交、其他写入或归属变化
- **THEN** 新金融调用基于当前证据重新判断，拒绝时指出变化及相关限制，不相信缓存允许标记，也不要求另一个模型查询步骤

## ADDED Requirements

### Requirement: Financial tool calls return without post-submission verification
金融工具 SHALL 按交易所接口返回逐项输出成功或失败及原因，不为确认本项执行结果追加只读查询、轮询、sleep 或重发；后台核验和下单前必要检查 SHALL 保留。受理成功不等于成交；接口异常不证明交易所未受理，内部 SHALL 保留真实提交阶段及原身份。调用 SHALL 汇总已执行结果和未执行尾项后一次返回。

#### Scenario: Cancellation succeeds and entry times out
- **WHEN** 撤单接口确认成功，随后开仓接口超时
- **THEN** 无补查或等待，返回“cancel 订单 xxx 撤单成功；open 数量@价格 请求失败，原因：网络超时，提交结果未知，请勿重复提交；后续项未执行”，后台继续核验原开仓请求，不重发撤单或开仓

#### Scenario: Accepted order returns directly
- **WHEN** 交易所返回有效订单受理响应且尚未成交
- **THEN** 返回“open 订单 xxx 提交成功”，内部保留 submitted，不追加 GET 订单/仓位、等待或轮询，不声称成交

#### Scenario: Confirmed rejection returns directly
- **WHEN** 交易所接口明确拒绝
- **THEN** 返回失败和具体原因，不进行结果核验；默认停止策略将尾项列为未执行

#### Scenario: Response loss does not replay the tail
- **WHEN** 调用断开或结果未知后重放同一批次身份
- **THEN** 复用已保存记录，不在工具调用内查询交易所核验原结果，不重发或续跑尾项；后续证据由后台核验更新

### Requirement: Plan conflicts preserve submission certainty
执行器 SHALL 协调同账户标的管理范围内的计划写入及后台证据更新，避免旧对象覆盖新版本。确认尚未提交的冲突 SHALL 在原动作内加载最新状态、重新校验后继续；可能已提交时交给后台核验原请求，不在工具调用内追加核验，已确认成功不得因后续本地保存或维护冲突降级为提交未知。不得通过取消版本校验或仅延时保存旧对象解决冲突。

#### Scenario: Two sequential entries share a plan
- **WHEN** 第一项入场已保存最新计划，第二项入场使用同一管理范围
- **THEN** 第二项基于最新计划执行，不沿用过期版本或重复第一项，不将第一项成功覆盖为整批未知

#### Scenario: Background writer updates before submission
- **WHEN** 本项尚未提交，后台更新使本地计划版本过期
- **THEN** 在原动作提交前重新加载与执行必要校验，不追加等待轮询，不要求模型生成新请求，不覆盖后台更新

#### Scenario: Persistence conflict follows acceptance
- **WHEN** 交易所已确认受理后本地保存出现版本冲突
- **THEN** 保留受理事实及原身份，处理本地状态同步；不能把已挂单说成明确失败并再次下单

### Requirement: Protective actions retain necessary ownership checks without polling
止损/止盈依赖的本地归属证据索引未完成时，执行器 SHALL 区分索引延迟与真实归属冲突，保留原范围必要的提交前检查与同步，但不追加等待或轮询；必要证据仍不完整 SHALL 返回未提交失败，后台继续同步且不补发保护动作。已具备当前动作所需可信证据时 SHALL 不单凭无关历史索引积压拒绝；权限、管理权、目标及数量约束仍必须满足，不推定缺失证据已经完整。

#### Scenario: Necessary evidence is available before protective submission
- **WHEN** 止损/止盈尚未提交，提交前必要检查与同步已取得完整证据
- **THEN** 完成原保护动作必要准入检查，满足条件后提交一次并返回真实受理结果，不要求模型重新发送

#### Scenario: Index remains incomplete
- **WHEN** 提交前必要检查与同步后保护动作所需归属证据仍不完整且确认尚未提交
- **THEN** 简短返回“止损/止盈失败，原因：持仓归属证据同步未完成，未提交”，默认停止策略列出未执行尾项；不得声称保护已经挂上，后台不得补发该动作

#### Scenario: Genuine ownership conflict remains a rejection
- **WHEN** 核验发现另一管理者持有目标管理权或目标不在授权范围
- **THEN** 明确拒绝该动作，不把冲突当成延迟绕过权限，也不自动接管

#### Scenario: Historical backlog is unrelated to the current order
- **WHEN** 当前目标、仓位数量和管理权已有可信证据，未完成索引仅涉及无关历史
- **THEN** 依据当前动作的实际依赖判断，不将全量历史索引完成作为提交止损/止盈的额外前置条件

