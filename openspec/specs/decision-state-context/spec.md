# decision-state-context Specification

## Purpose

为交易决策和绑定任务聊天提供一致、可追溯的当前交易状态，使模型围绕当前持仓、挂单及实际相关异常作出策略判断。将执行核验交给执行层，并使策略记忆与历史执行记录分离，避免旧回执和已失效限制持续影响新决策。

## Requirements

### Requirement: Default trading context describes current state
REAL/STRATEGY 独立退出模式的默认决策和绑定任务聊天 SHALL 使用同一当前交易状态语义，展示当前持仓、活跃挂单、保护覆盖、数量冲突和相关异常。完整历史操作回执、回执修订列表和旧准入建议 SHALL NOT 被逐轮默认注入。市场、消息及有效策略背景继续可用；本轮工具结果和用户明确请求的历史查询不属于该默认注入限制。

#### Scenario: Completed history does not grow the default prompt
- **WHEN** 当前账户和相关异常完全相同，仅新增或刷新 100 条已完成且无残留影响的历史操作
- **THEN** 默认交易状态文本不变，不出现这些操作的回执 JSON、恢复计数或历史限制

#### Scenario: An ordinary independent position
- **WHEN** 当前有 LONG 0.15、一笔未成交入场和三笔已核验等待触发的退出单
- **THEN** 默认上下文逐单展示当前有效身份、类型、方向、价格及数量语义，正常订单只展示一次，历史开仓和平仓回执不再额外展开

#### Scenario: Bound chat uses the same state
- **WHEN** 定时决策和绑定任务聊天使用相同账户证据
- **THEN** 两者呈现相同持仓、挂单和异常事实，聊天不会再追加一份历史恢复记录块，既有审批保持有效

### Requirement: Current exceptions identify material execution uncertainty
当前异常 SHALL 来自结构化证据，并说明作用范围、订单或原操作身份、缺失事实和可能受影响的动作。选择范围 SHALL 包括会影响当前仓位、活跃订单或新请求的旧周期残留，不以操作年龄或最近记录条数排除真实异常。正常挂单、无执行影响的拒绝、已解决事项及单纯历史错误文本 SHALL NOT 被标为待核验；数据源读取失败 SHALL 与已确认无异常区分。

#### Scenario: A normal trigger is not an exception
- **WHEN** 条件单已核实等待触发，未发生未知提交、成交或修改
- **THEN** 它作为活跃挂单展示，不因为 submitted 或尚未成交而显示待核验异常

#### Scenario: An old all order remains material
- **WHEN** 旧周期的条件 all 是否仍活跃未确认，可能影响同方向新仓位，即使它已排在最近 20 条操作之外
- **THEN** 当前异常仍包含该单身份和同方向影响，不因隐藏历史回执而遗漏，也不扩大为无关方向禁令

#### Scenario: Terminal outcome lacks final fills
- **WHEN** 一张订单已取消但最终累计成交尚未确认，相关仓位数量仍依赖此事实
- **THEN** 活跃挂单列表不重复展示该单，但异常保留身份、最后已知量、待核实事实与时间，不把最后已知量作为最终成交量

#### Scenario: Evidence source fails
- **WHEN** 读取当前订单或周期证据失败
- **THEN** 展示具体范围的数据不可用或陈旧，不把空返回、无可用回执或默认 false 表述为“无异常”

### Requirement: Context freshness follows underlying observations
当前状态 SHALL 区分账户、订单和周期证据的真实观察时间及读取完整性。缓存重组或回执更新时间 SHALL NOT 替代证据观察时间。没有当前证据支持的历史允许或禁止建议 SHALL NOT 作为本轮准入结论；缺失或冲突的证据按其范围呈现，最终动作仍由执行层核验。

#### Scenario: Recently rendered receipt has old evidence
- **WHEN** 10 月 13 日生成的回执引用 10 月 12 日的仓位判断且当前范围未刷新
- **THEN** 该判断保持旧观察时间和未评估标记，不以新回执时间把它呈现为当前交易限制或当前许可

#### Scenario: Mixed generations cannot masquerade as one snapshot
- **WHEN** 账户快照与本地周期证据发生无法解释的数量或版本冲突
- **THEN** 对相关范围显示证据不一致，已确认订单事实保留，不拼出貌似一致且已核验的仓位结论

### Requirement: Execution owns verification and recovery
默认上下文 SHALL 将核验及恢复表示为执行层负责的工作，不要求决策模型例行查询每笔历史操作或先获得恢复许可。模型可直接表达新的交易意图，工具在本次调用内按既有预算和依赖核验；背景恢复 SHALL 继续原已授权意图，不自动重放未知写入、旧未执行项或新增交易策略。

#### Scenario: Direct management after an old rejection
- **WHEN** 旧止损明确未被接受，Agent 根据当前仓位直接请求有效新止损
- **THEN** 系统在该工具调用内核验并处理，不要求 Agent 先查询旧操作解锁

#### Scenario: Unknown submission still prevents a duplicate
- **WHEN** 同方向平仓结果仍未知，新请求依赖该剩余数量
- **THEN** 执行层保留具体限制并继续按原身份恢复，移除默认历史回执不导致重复平仓

### Requirement: Memory resolves execution concerns by identity and evidence
每轮、滚动及历史记忆整理 SHALL 取得被引用操作的最新可用证据，按账户、任务、标的、方向、周期和订单/操作身份对应。旧疑虑被较新证据解决后 SHALL 从决策可见的当前异常及记忆执行引用中退出，即使原事件已在摘要窗口外。无匹配、证据不完整或查询失败不得推定解决；原始记忆和回执保留可追溯。

#### Scenario: A restriction resolves outside the four-hour window
- **WHEN** 旧记忆引用四小时前的 unknown 操作，当前证据已确认其成交、相关清理及残留影响均解决
- **THEN** 下一次决策不再获得该操作导致的“不新开仓”限制，记忆整理使用原身份的解决证据而非最近条数筛选

#### Scenario: Similar positions are not the same event
- **WHEN** 旧疑虑涉及一笔 0.37 SHORT，而最新 completed 回执属于另一笔 0.30 SHORT 或其他账户/周期
- **THEN** 系统不以新回执解除旧疑虑，也不把旧自由文本直接提升为整个账户的当前交易禁令

#### Scenario: Completed close still has relevant cleanup
- **WHEN** 平仓已 completed，但关联保护单清理仍未知且可能影响新仓位
- **THEN** 记忆保留平仓已完成的事实，仅剩余清理影响进入当前异常，不把整笔平仓继续记为未知

#### Scenario: A parent batch contains an unexecuted tail
- **WHEN** 记忆引用批次，后来证据仅确认已提交子操作的结果
- **THEN** 原父子身份准确对应，原 not_executed 子项仍为未执行，不能通过批次恢复推定全部完成

### Requirement: Strategy memory does not carry executable restrictions
决策可见的短期记忆和近期已发布策略摘要 SHALL 分开承载策略背景与可关联的执行引用。市场假设、价位、持仓理由、策略等待条件及历史教训可以保留；持仓/订单现状和执行限制由当前状态视图统一提供。已解决的执行限制 SHALL NOT 经旧摘要、记忆失败回退或自定义占位符重新进入默认输入；源正文与摘要完整保留，投影不得按字符硬截断。近期摘要 SHALL 选取最近三条有效已发布策略内容，保留真实来源时间，不把较旧的已发布内容表述为最新三轮决策。

#### Scenario: Keep a strategy condition while removing a resolved execution ban
- **WHEN** 原文同时包含“等四小时收盘确认”和“旧订单 unknown，因此禁止加仓”，后者已核验解决
- **THEN** 策略等待条件保留，旧执行禁令消失，当前异常按最新证据生成

#### Scenario: Recent summaries cannot resurrect a resolved issue
- **WHEN** 短期记忆已更新，但近期摘要仍记载相同旧禁令
- **THEN** 默认近期策略背景不再次引入该禁令，原始摘要仍可在历史记录中查看

#### Scenario: Background resolution does not wait for a language model
- **WHEN** 执行证据已解决事项，但记忆整理模型失败或队列尚未运行
- **THEN** 下一次决策读取时相应执行限制已经退出，仍有效的已发布策略内容继续使用，不回退注入旧自由文本

### Requirement: Legacy narrative transition is explicit and non-destructive
旧记忆及近期摘要 SHALL 保留原文，并通过有版本和来源关联的策略视图过渡。未完成投影的旧执行描述 SHALL NOT 自动成为当前事实或许可依据；无法关联的历史疑虑保持未解决记录，其当前影响只依据可核验的范围呈现。升级及整理失败 SHALL 保留已有有效策略内容，不阻止当前状态读取、触发交易重放或删除旧证据。没有有效已发布内容的来源通过有界后台转换补齐，管理端区分未生成、排队、失败及可重试状态，Prompt 不逐条注入整理占位符。

#### Scenario: First decision after upgrade
- **WHEN** 任务只有含旧全局禁令的自由文本记忆，尚无可用策略视图
- **THEN** 当前持仓、挂单和可核验异常正常呈现；旧混合正文不直接注入，仅说明尚无已发布策略背景，后台转换来源，管理端可查看及重试失败

#### Scenario: Manual edit invalidates the derived view
- **WHEN** 用户修改或删除作为投影来源的记忆或摘要正文
- **THEN** 原文按现有编辑行为保存或删除，对应旧视图失效；不能冒用旧来源版本或重新注入新混合正文，其他有效来源仍可使用

### Requirement: Published strategy remains available during consolidation
短期记忆和近期策略摘要 SHALL 从已成功校验并发布的内容读取。新增来源、排队、重试、失败及服务重启 SHALL NOT 清空同任务、账户、标的范围内仍有效的已发布内容。新内容仅在校验和持久化完成后可见，读者不会看到部分发布结果。未完成候选不得覆盖较新内容；发布时间不得代替策略来源的截至时间。

#### Scenario: Pending and failed new sources retain previous strategy
- **WHEN** 已有有效记忆和三条策略摘要，较新的来源进入排队或转换失败
- **THEN** 决策继续得到原已发布记忆和摘要及各自截至时间，正文不被“整理中/暂不可用”替换

#### Scenario: Publication races with decisions and manual edits
- **WHEN** 整理结果发布期间发生决策读取、手工编辑或更新来源到达
- **THEN** 读取只看到完整旧版本或完整新版本；过期来源结果不覆盖人工修改和较新内容

#### Scenario: Scope changes and restarts
- **WHEN** 服务重启或切换任务账户、交易标的
- **THEN** 重启可继续读取持久化有效内容；不同账户或标的的旧内容不得回退到当前任务

### Requirement: Strategy sources and execution evidence fail independently
短期记忆、近期策略摘要及执行引用解析 SHALL 分别处理失败。一项读取或解析失败 SHALL NOT 隐藏其他已读取的有效策略内容。执行证据不可用仍按原身份报告不确定性，不推定旧事项已解决，也不把历史文字重新升级为当前交易许可或禁令。

#### Scenario: Execution reference lookup fails
- **WHEN** 记忆和摘要可读，但原操作证据查询失败
- **THEN** 策略正文保留，执行层相关异常说明证据不可用及范围

#### Scenario: One strategy source is unreadable
- **WHEN** 记忆或摘要中的一个来源损坏或读取失败
- **THEN** 其余有效来源继续展示，管理端保留故障状态，不将整份背景替换为通用占位符

### Requirement: Current trade facts use compact readable text
当前持仓和挂单 SHALL 使用确定性文本或表格渲染，保留原订单身份、方向、用途、状态、精确数量以及分别标识的限价和触发价。正常项目只展示一次，省略空值和冗余内部元数据；未知、零、fixed 和触发时 all 必须可区分。真实观察时间按数据来源集中展示，差异和陈旧性显式保留；保护覆盖、数量冲突及当前执行异常简洁单列。格式整理 SHALL NOT 发起模型调用或修改执行状态。

#### Scenario: Normal position and three orders
- **WHEN** 持仓为多仓 0.07、均价 2415、浮盈 7.93163651，挂有止盈市价 0.07 @2545、止损市价 0.07 @2357 和限价入场 0.1 @2385
- **THEN** 持仓用一行可读文字、订单用逐单表格展示，完整订单 ID 原样保留，不展开 client_id、重复 snapshot 字段、null 或浮点时间戳；浮盈可明确约数展示，交易数量和价格不作破坏精度的截断

#### Scenario: Trigger limit, dynamic all and child identity
- **WHEN** 条件限价单包含触发价和成交限价，all 单数量在触发时确定，或条件单已有活动子单
- **THEN** 两种价格分别保留，all 表述为触发时该方向全部仓位，子单及必要父单身份保持准确，不将未知数量写成零

#### Scenario: Missing observations and ownership conflicts
- **WHEN** 仓位、挂单、归属或来源观察发生缺失、数量冲突或读取失败
- **THEN** 精简后的文本仍保留具体对象、最后已知信息、缺失事实和影响范围，不表述为已确认空仓、无挂单或无异常

#### Scenario: Scheduled agent and bound chat share rendering
- **WHEN** 定时决策和绑定任务聊天读取相同状态
- **THEN** 二者得到相同精简事实与已发布策略内容；增加无关内部元数据或已完成历史记录不会增长默认 Prompt

### Requirement: Memory review has one effective output contract
系统 SHALL 按当前记忆整理模式选择唯一有效的输出契约，并使用对应契约验证响应。REAL/STRATEGY 独立退出模式 SHALL 要求完整原文 `summary`、新记忆策略内容 `memory` 和待转换来源集合 `projections`；其他模式保留单 `summary` 契约。自定义 Prompt 和历史素材 SHALL NOT 改变程序输出约束，已保存模板及完整来源正文保持不变。

#### Scenario: Independent memory request contains no competing built-in format
- **WHEN** 独立退出任务构造记忆整理请求
- **THEN** 程序有效指令只要求完整投影输出，不同时要求仅输出单 `summary`；响应按相同模式校验

#### Scenario: Legacy custom prompt uses the summary-only example
- **WHEN** 已保存自定义模板含旧的单 `summary` 示例，且任务使用 system 或 user 指令角色
- **THEN** 自定义内容仍作为整理素材完整保留，程序明确当前完整投影契约，旧示例不获得覆盖格式的效力

#### Scenario: No historical sources need conversion
- **WHEN** 独立模式生成新记忆且没有待转换历史来源
- **THEN** 仍要求本次记忆的完整策略对象，`projections` 显式为空数组；不能省略字段或由程序伪造空策略内容

#### Scenario: Non-independent memory remains compatible
- **WHEN** 非独立退出任务返回有效单 `summary` 响应，包含既有可忽略的旧规则说明字段
- **THEN** 按原兼容行为取得完整记忆正文，不要求投影字段、不启用投影格式纠正或规则工具

### Requirement: Projection format correction is bounded and auditable
对独立模式中已完整结束且无工具调用的响应，系统 SHALL 允许 JSON 或字段结构错误触发至多一次格式纠正阶段。首次请求、传输重试及纠正阶段的实际模型请求 SHALL 共享每次整理最多三次的上限，正常有效响应只调用一次。系统 SHALL 分别保留各次请求、原始响应、错误类别、所属整理操作及已返回用量，只有通过完整校验的结果可作为成功发布依据。

#### Scenario: Summary-only response is corrected
- **WHEN** 第一次模型响应只有有效 `summary`，缺少 `memory` 和 `projections`，且仍有请求预算
- **THEN** 系统使用原来源快照、唯一输出契约和缺失字段说明启动一次纠正；只有完整响应通过结构及语义校验后，整理才能成功

#### Scenario: Transport retries share the correction budget
- **WHEN** 一次整理交替发生传输失败、格式错误和纠正请求
- **THEN** 所有实际请求合计不超过三次，纠正阶段至多启动一次；预算耗尽时保留具体失败状态，不重置计数再次调用

#### Scenario: Correction fails again
- **WHEN** 纠正响应仍缺字段或无法解析为约定 JSON
- **THEN** 本次整理失败，错误可追溯，系统不补空字段、不复制混合原文充当策略、不缓存为成功，也不覆盖已发布内容

#### Scenario: Incomplete and semantic failures remain rejected
- **WHEN** 服务商报告截断或拒绝、响应包含工具调用，或者完整结构中的来源身份、哈希、覆盖、执行引用或权限语义校验失败
- **THEN** 系统拒绝本次结果，不把这类错误转换为格式纠正许可，不执行工具，不放宽发布校验

#### Scenario: Failed attempt and successful correction retain separate costs
- **WHEN** 初次结构校验失败而后续纠正成功，两次响应均提供用量
- **THEN** 首次调用保留失败状态和原始输出，后续调用独立记录成功；两次真实用量各记录一次，并能关联同一整理操作与不同尝试阶段

### Requirement: Legacy projection format failures recover once per contract revision
输出契约修复后，系统 SHALL 对能够确认为旧契约缺少 `memory`/`projections`、来源与当前范围仍有效的终止投影和记忆生成任务，按契约修订只进行一次后台重新排队，不依赖新交易产生。恢复 SHALL 原子保留原失败诊断与修订标记，沿用租约、退避、每项最多三次工作尝试及成功结果复用。已发布内容、原始证据和交易状态 SHALL 保持完整。

#### Scenario: Failed existing memory and summaries publish for the first time
- **WHEN** 当前范围内已有有效短期记忆原文和三条有效摘要，相关转换因旧缺字段错误终止且没有已发布策略
- **THEN** 后台定向重新排队并在获得合法结果后发布，下一次默认决策及绑定聊天读取到真实记忆和三条策略摘要及来源截至时间，不再仅显示两个“尚无已发布”提示

#### Scenario: Failed memory generation has no original memory yet
- **WHEN** 当前任务没有短期记忆原文，但有因旧缺字段错误终止、证据窗口和范围仍可确认的记忆生成任务
- **THEN** 后台恢复该任务，仅重新整理已保存证据，成功后原子保存记忆和策略视图并推进有效摘要发布，不重跑交易或伪造历史来源

#### Scenario: Restart cannot reset the retry budget repeatedly
- **WHEN** 定向恢复后的来源再次耗尽工作重试次数，随后后台再次扫描或服务重启
- **THEN** 同一契约修订不会再次自动重置它，保留失败原因供现有管理重试入口处理；其他有效已发布内容继续可用

#### Scenario: Unrelated or invalid sources are excluded from recovery
- **WHEN** 候选行已经成功、属于旧 partial 写入、源正文已修改或删除、属于其他范围、无法核实当前范围，或错误属于鉴权、截断、语义校验等无关故障
- **THEN** 本次自动恢复不重新排队该行，不改写它的原始失败证据，不使其他有效内容失效

#### Scenario: Existing ready views survive the contract repair
- **WHEN** 任务已有有效已发布策略，升级输出契约并恢复部分历史失败行
- **THEN** 原已发布视图立即保持可读，旧任务晚完成不会覆盖来源时间较新的有效内容，历史 unknown 禁令不重新进入默认 Prompt

#### Scenario: No recoverable source exists
- **WHEN** 当前范围没有有效短期记忆原文，也没有可恢复的记忆生成任务
- **THEN** 系统如实保留无已发布短期记忆状态；存在有效摘要时可独立发布摘要，不凭空生成旧记忆，也不把无错误等同于恢复完成
