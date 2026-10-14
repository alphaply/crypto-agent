# decision-state-context Specification

## Purpose

为交易决策和绑定任务聊天提供一致、可追溯的当前交易状态，使模型围绕当前持仓、挂单及实际相关异常作出策略判断。将执行核验交给执行层，并使策略记忆与历史执行记录分离，避免旧回执和已失效限制持续影响新决策。

## Requirements

### Requirement: Historical memory snapshots can be cleared independently
Agent 短期记忆及历史列表中的成交事实快照 SHALL 明确为整理时保存的历史证据副本，完整原文按需在限定高度区域中查看。已登录用户 SHALL 能按任务和单条记忆身份清除该快照，保留策略正文、已发布策略视图和原始交易记录。清除 SHALL 不改变执行状态、触发交易或关闭后续新记忆的快照生成。

#### Scenario: Clear a large old snapshot
- **WHEN** 用户在指定记忆展开成交事实快照并确认清除
- **THEN** 服务仅清空该记忆的 position_summary；其他任务、记忆及审计证据不变，页面隐藏已清快照，重复清除同一现存记录成功，找不到该记忆时返回未找到

#### Scenario: Editing strategy after clearing preserves the clear
- **WHEN** 用户清除快照后保存一个之前打开的策略正文编辑框
- **THEN** 正文编辑不重新提交旧快照，省略快照字段的更新仅改变策略正文，兼容显式更新快照的既有请求

#### Scenario: Public users only view snapshots
- **WHEN** 未登录用户查看记忆或直接请求清除快照
- **THEN** 页面不显示清除入口，写入接口拒绝未认证请求

### Requirement: Default trading context describes current state
REAL、STRATEGY、SPOT_DCA 的默认决策和绑定任务聊天 SHALL 使用同一当前交易状态语义，展示配置可见范围内的当前持仓、活跃挂单、保护覆盖、数量冲突、管理者及相关异常。完整历史操作回执、回执修订列表和旧准入建议 SHALL NOT 被逐轮默认注入。市场、消息及有效策略背景继续可用；本轮工具结果和用户明确请求的历史查询不属于该默认注入限制。外部来源 SHALL 不单独构成不能管理的结论；实际管理竞争、权限及目标能力分别表达。

#### Scenario: Completed history does not grow the default prompt
- **WHEN** 当前账户和相关异常完全相同，仅新增或刷新 100 条已完成且无残留影响的历史操作
- **THEN** 默认交易状态文本不变，不出现这些操作的回执 JSON、恢复计数或历史限制

#### Scenario: An ordinary independent position
- **WHEN** 当前有 LONG 0.15、一笔未成交入场和三笔已核验等待触发的退出单
- **THEN** 默认上下文逐单展示当前有效身份、类型、方向、价格及数量语义，正常订单只展示一次，历史开仓和平仓回执不再额外展开

#### Scenario: Bound chat uses the same state
- **WHEN** 定时决策和绑定任务聊天使用相同账户证据
- **THEN** 两者呈现相同持仓、挂单、管理者和异常事实，聊天不会再追加一份历史恢复记录块，既有审批保持有效

#### Scenario: An external position has no local cycle
- **WHEN** 当前账户存在可见人工仓位及挂单，本地周期尚未建立且该范围未被其他管理者认领
- **THEN** 如实展示数量、订单和可自动认领状态，说明首次管理请求会内部纳管，不显示空仓、要求先 adopt 或将外部来源作为全局交易禁令

#### Scenario: A visible resource belongs to another manager
- **WHEN** 查询可见某资源，但其交易范围已经由另一个有效任务认领
- **THEN** 保留资源事实并明确管理者及本调用者只读状态；其他未冲突范围不被整体禁用

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
当前持仓和挂单 SHALL 使用确定性文本或表格渲染，保留原订单身份、方向、用途、状态、精确数量以及分别标识的限价和触发价。正常项目只展示一次，省略空值和冗余内部元数据；未知、零、fixed 和触发时 all 必须可区分。真实观察时间按数据来源集中展示，差异和陈旧性显式保留；保护覆盖、数量冲突及当前执行异常简洁单列。管理范围、当前管理者和外部来源 SHALL 清楚展示，来源与管理权分开表达。格式整理 SHALL NOT 发起模型调用、认领范围或修改执行状态。

#### Scenario: Normal position and three orders
- **WHEN** 持仓为多仓 0.07、均价 2415、浮盈 7.93163651，挂有止盈市价 0.07 @2545、止损市价 0.07 @2357 和限价入场 0.1 @2385
- **THEN** 持仓用一行可读文字、订单用逐单表格展示，完整订单 ID 原样保留，不展开 client_id、重复 snapshot 字段、null 或浮点时间戳；浮盈可明确约数展示，交易数量和价格不作破坏精度的截断

#### Scenario: Trigger limit, dynamic all and child identity
- **WHEN** 条件限价单包含触发价和成交限价，all 单数量在触发时确定，或条件单已有活动子单
- **THEN** 两种价格分别保留，all 表述为触发时该方向全部仓位，子单及必要父单身份保持准确，不将未知数量写成零

#### Scenario: Missing observations and ownership conflicts
- **WHEN** 仓位、挂单、管理权或来源观察发生缺失、数量冲突或读取失败
- **THEN** 精简后的文本仍保留具体对象、最后已知信息、缺失事实和影响范围，不表述为已确认空仓、无挂单或无异常；已知外部来源本身不被描述为归属冲突

#### Scenario: Scheduled agent and bound chat share rendering
- **WHEN** 定时决策和绑定任务聊天读取相同状态
- **THEN** 二者得到相同精简事实、管理状态与已发布策略内容；增加无关内部元数据或已完成历史记录不会增长默认 Prompt

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
对独立模式中已完整结束且无工具调用的响应，系统 SHALL 允许 JSON 或字段结构错误触发格式纠正；结构、来源、覆盖及引用均有效但策略文字混入执行限制或解锁结论时 SHALL 允许内容分离纠正。两类错误 SHALL 共享至多一次纠正阶段，使用原来源快照及可信错误路径要求重新返回完整对象。首次请求、传输重试及纠正阶段的实际模型请求 SHALL 共享每次整理最多三次的上限，正常有效响应只调用一次。系统 SHALL 分别保留各次请求、原始响应、错误类别、所属整理操作及已返回用量，只有通过完整校验的结果可作为成功发布依据。

#### Scenario: Summary-only response is corrected
- **WHEN** 第一次模型响应只有有效 `summary`，缺少 `memory` 和 `projections`，且仍有请求预算
- **THEN** 系统使用原来源快照、唯一输出契约和缺失字段说明启动一次纠正；只有完整响应通过结构及语义校验后，整理才能成功

#### Scenario: Transport retries share the correction budget
- **WHEN** 一次整理交替发生传输失败、格式错误和纠正请求
- **THEN** 所有实际请求合计不超过三次，纠正阶段至多启动一次；预算耗尽时保留具体失败状态，不重置计数再次调用

#### Scenario: Correction fails again
- **WHEN** 纠正响应仍缺字段、无法解析为约定 JSON 或仍混入执行限制，即使前后错误类别不同
- **THEN** 本次整理失败，错误可追溯，系统不补空字段、不复制混合原文充当策略、不缓存为成功，也不覆盖已发布内容

#### Scenario: Incomplete and semantic failures remain rejected
- **WHEN** 服务商报告截断或拒绝、响应包含工具调用，或者来源身份、哈希、覆盖、执行引用或额外结构化权限字段校验失败，即使同时混入错误执行叙述
- **THEN** 系统拒绝本次结果，不启动格式或内容纠正，不执行工具，不放宽发布校验

#### Scenario: Misplaced execution narrative is separated
- **WHEN** 完整响应的来源和引用均有效，但 strategy_text 含旧 unknown 禁令或 allowed、blocked_actions、已解锁等执行结论
- **THEN** 在剩余预算内启动一次内容纠正，要求保留完整 summary，将执行叙述放入 unlinked_execution_notes、可信身份放入 execution_refs；重新验证完整输出，不由程序删句或把纠正当成交易许可

#### Scenario: Independent strategy conditions do not become execution bans
- **WHEN** 文字说明历史回执不能证明成交，或旧 unknown 仅供审计，另一个句子要求等四小时收盘确认前不加仓
- **THEN** 校验不把无关跨句内容拼成执行禁令；直接以未核验为由禁止加仓的叙述仍触发内容分离校验

#### Scenario: Failed attempt and successful correction retain separate costs
- **WHEN** 初次结构校验失败而后续纠正成功，两次响应均提供用量
- **THEN** 首次调用保留失败状态和原始输出，后续调用独立记录成功；两次真实用量各记录一次，并能关联同一整理操作与不同尝试阶段

### Requirement: Legacy projection format failures recover once per contract revision
输出契约修复后，系统 SHALL 对能够确认为旧契约缺少 `memory`/`projections`、来源与当前范围仍有效的终止投影和记忆生成任务，按契约修订只进行一次后台重新排队，不依赖新交易产生。内容分离修订 SHALL 另定向恢复此前精确执行叙述校验错误，不重置已使用缺字段修订预算的任务。恢复 SHALL 原子保留原失败诊断、之前修订的恢复诊断与修订标记，沿用租约、退避、每项最多三次工作尝试及成功结果复用。已发布内容、原始证据和交易状态 SHALL 保持完整。

#### Scenario: Content repair restores earlier failed memory work
- **WHEN** 旧修订的任务因精确错误 Strategy projection contains an execution permission/restriction 终止，来源与当前范围仍有效
- **THEN** 后台只恢复一次，使其经过严格校验及有界内容纠正后发布记忆与摘要；新发布记忆记录当前修订，后续扫描不会重复恢复

#### Scenario: New repair does not reset earlier format failures
- **WHEN** 缺字段任务已在格式修订中耗尽尝试，随后升级内容分离修订
- **THEN** 该任务保留失败状态和预算；反复跳过无法核实范围的任务也不反复嵌套同一修订的诊断

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
- **WHEN** 候选行已经成功、属于旧 partial 写入、源正文已修改或删除、属于其他范围、无法核实当前范围，或错误属于鉴权、截断、伪造来源/引用等无关故障
- **THEN** 本次自动恢复不重新排队该行，不改写它的原始失败证据，不使其他有效内容失效

#### Scenario: Existing ready views survive the contract repair
- **WHEN** 任务已有有效已发布策略，升级输出契约并恢复部分历史失败行
- **THEN** 原已发布视图立即保持可读，旧任务晚完成不会覆盖来源时间较新的有效内容，历史 unknown 禁令不重新进入默认 Prompt

#### Scenario: No recoverable source exists
- **WHEN** 当前范围没有有效短期记忆原文，也没有可恢复的记忆生成任务
- **THEN** 系统如实保留无已发布短期记忆状态；存在有效摘要时可独立发布摘要，不凭空生成旧记忆，也不把无错误等同于恢复完成

### Requirement: Memory consolidation prunes execution details and bounds pending sources
短期记忆整理素材中的工具回执与执行证据 SHALL 经过精简，省略内部引擎元数据（如 continuation 准入评估、blocker_order_ids、admission 判定、recovery 调试及重试计数）与未提交至交易所的瞬态参数校验报错。待转换的历史策略投影来源 SHALL 受到有界滑动窗口限制，避免历史积压全量涌入单次输入；已取消或已完成的终态订单 SHALL 停止累加恢复尝试计数。

#### Scenario: Memory review input omits internal execution engine metadata
- **WHEN** 历史摘要中的工具操作回执包含内部 continuation、admission 与 recovery 调试字段并被注入记忆整理 Prompt
- **THEN** 记忆整理素材仅保留工具名称、动作、执行状态与关键交易事实（方向、数量、成交量、价格与当前保护状态），剥离 blocker_order_ids、continuation 规则列表、previous_assessment 及 recovery 重试元数据

#### Scenario: Transient schema validation errors are excluded from memory evidence
- **WHEN** 历史周期中发生未提交至交易所的工具参数验证错误（如 TradeActionsSchema 依赖或参数校验失败）
- **THEN** 该报错仅在决策审计日志中保留，不作为未决操作证据或阻塞订单注入短期记忆整理输入

#### Scenario: Pending historical projection sources are bounded
- **WHEN** 数据库中存在大量未完成（status='pending'）的待转换历史策略摘要
- **THEN** 记忆整理与投影准备单次仅加载最近有界数量的待转换来源（如最近 5 条），超出界限的过期来源不全量注入单次 Prompt，候选操作引用集仅取当前有界来源

#### Scenario: Terminal orders stop accumulating recovery retries
- **WHEN** 操作关联的订单已确认处于终态（如已取消或完全成交且无未决清理影响）
- **THEN** 系统将其标记为已收敛，停止对其递增 attempt_count 并移出活跃 recovery 轮询集合，回执中的尝试计数不再无限制增长

### Requirement: Current context describes management scope and external changes
内置 Agent、绑定任务聊天、MCP 与前端 SHALL 以一致事实展示独占范围、管理者、当前仓位、已纳管外部订单、变化来源、真实观察时间和对账完整性。外部来源不确定时 SHALL 标识为外部/未跟踪活动，不能推断一定由人类操作。默认上下文 SHALL 使用当前相关事实及变化摘要，不逐轮注入全部外部成交历史。

#### Scenario: External reduction is reconciled
- **WHEN** 独占范围内发生可核验外部减仓
- **THEN** 当前数量、退出覆盖和数量冲突同时更新，默认状态说明外部变化，不再仅因其来源提示归属失败

#### Scenario: External order is enrolled
- **WHEN** 外部订单纳管完成且可在既有权限下管理
- **THEN** 展示真实 order_id、入场/退出作用、原生参数、来源及可用动作，不能仍仅标为 external_or_untracked/modifiable=false

#### Scenario: Only one source is incomplete
- **WHEN** 普通订单可读而条件订单或成交证据不完整
- **THEN** 标明具体缺失来源及其观察时间，不显示“已确认没有异常”或空仓，也不把所有动作统一标为禁止

### Requirement: External closure preserves original and current meanings
外部全平、原周期结束及新周期建立 SHALL 在当前状态和历史中区分。当前视图 SHALL 使用新周期事实；旧 Agent 操作和外部活动保留历史身份。外部动作 SHALL NOT 被写成 Agent 原操作的成交结果；已解决的外部变化 SHALL NOT 持续作为当前阻塞项。

#### Scenario: Human closes and opens a new position
- **WHEN** 人工全平及新开仓已核验，旧周期清理完成且新周期已登记
- **THEN** 默认上下文展示当前新仓及其保护，历史可查旧周期与外部平仓，不沿用旧周期数量、旧 all 覆盖或旧准入建议

### Requirement: Memory prose contains only strategy judgments levels and lessons
新生成及默认展示的记忆正文 SHALL 只包含策略判断、关键价位和有效教训，可保留有策略意义的观察条件。operation_id、resolution、周期报错、执行解锁流程、当前仓位/挂单快照、收益统计和账本 JSON SHALL NOT 写入记忆正文。当前交易事实 SHALL 来自独立的最新状态；原始历史证据及关联元数据 SHALL 保留在后台或按需详情，不通过旧记忆回退重新注入。

#### Scenario: Consolidate the supplied ETH memory
- **WHEN** 整理素材含两次 Edit 拒绝、缺少周期的失败批次、null 操作身份、未执行尾项及收益账本，另有四小时收盘或 CPI 观察条件
- **THEN** 新记忆保留策略判断、相关价位和有效教训，移除内部字段及历史错误复述；市场观察条件保留，执行解锁清单不进入记忆

#### Scenario: Historical noisy memory remains auditable
- **WHEN** 已存记忆正文包含 operation_id、resolution 或周期诊断
- **THEN** 经版本化后台转换发布纯策略视图，旧原文可按需审计；转换失败不回退将旧噪声注入默认策略正文，也不删除旧证据

### Requirement: Memory consolidation separates evidence plumbing from narrative input
记忆整理 SHALL 使用按语义筛选的策略素材，不默认拼接完整工具回执、周期账本、同步区间及收益统计。执行引用、来源校验和恢复状态 SHALL 由程序维护，与记忆正文及策略判断分开。精简 SHALL 依据事实分类，不按字符截断策略理由或关键价位；明示未提交的失败和未执行项不得作为待恢复交易反复复述。

#### Scenario: Definite rejections and unexecuted tails
- **WHEN** 原批次确认只有提交前失败且后续项未执行，或改单因归属校验被拒
- **THEN** 原回执保留于历史，记忆不复制失败计数或操作 ID，也不要求为不存在的交易所提交执行恢复

#### Scenario: Genuine unknown submission
- **WHEN** 存在可能已发送的真实交易写入而其结果仍未知
- **THEN** 后台保留原身份并核验；其必要影响通过当前相关事项简述，不能因精简记忆而重复下单或报告已解决

### Requirement: Current facts and restrictions are independent of historical cycle diagnostics
当前持仓 SHALL 以实际交易所观察为准；无本地周期、无已归属成交或历史账本开放周期为空 SHALL NOT 被表述为交易所空仓。历史引用读取失败、null 身份、旧归属拒绝及 unavailable 状态 SHALL NOT 单独形成当前交易禁令。对账与恢复 SHALL 由后台及执行层处理，只有当前动作真实依赖的未核实事实才进入简明待处理事项。

#### Scenario: Exchange position exists without an owned cycle
- **WHEN** 当前快照有 ETH 多仓和外部挂单，本地没有自有周期或已归属成交
- **THEN** 如实展示交易所仓位与挂单及实际可管理状态，不写“账户持仓为无”；独占授权下后台纳管，工具正常核验后管理

#### Scenario: Unrelated historical operation cannot be read
- **WHEN** 某旧操作 unavailable 且无法证明其对本次动作有实际影响，当前所需证据已满足
- **THEN** 不要求 Agent 查历史 ID 解锁，不增加“不新开仓”执行禁令；历史诊断可在详情中查看

### Requirement: Default performance presents equity change profit and drawdown only
默认收益区域 SHALL 只呈现权益变化、盈亏和回撤三类指标，标明简短区间、截至时间及必要口径。权益快照变化 SHALL 与可归属交易盈亏区分，不能相加或互相替代；收益指标 SHALL NOT 等待完整周期闭合才展示已可靠核验的金额。缺失、归属或覆盖不足 SHALL 显示暂不可统计，不以零值暗示完整结果；胜率、周期数和同步记录不默认展示。

#### Scenario: The supplied incomplete seven-day ledger
- **WHEN** 权益快照可计算，但近七天有未知归属成交、完整周期为零且覆盖不足
- **THEN** 展示已知权益变化及快照回撤，盈亏显示暂不可完整统计；不展示零盈亏、零交易结论或整份账本 JSON

#### Scenario: Verified profit before cycle closure
- **WHEN** 某未闭合管理周期已有可可靠归属并核验的已实现盈亏，相关金额口径完整
- **THEN** 默认盈亏可显示该金额及统计区间，不因完整周期数为零隐藏真实已确认金额，也不推断整笔交易已结束

#### Scenario: Mixed metrics have different scopes
- **WHEN** 权益变化按账户快照计算，而盈亏按本任务归属成交计算
- **THEN** 各指标简短标明口径和截至时间，不统一冠以 Agent 独立收益或将快照回撤宣称为逐笔盘中最大回撤

### Requirement: Detailed ledgers and diagnostics are available on demand
详细成交、费用、归属缺项、完整周期、同步覆盖和原操作回执 SHALL 在明确的详情入口按需查看，保留来源与真实观察时间。默认记忆、收益摘要和例行决策输入 SHALL NOT 自动附加上述完整资料；详情与摘要 SHALL 使用相同证据口径和时间，不重复计算盈亏。

#### Scenario: Expand performance details
- **WHEN** 用户主动打开收益或账本详情
- **THEN** 可查看已保留的成交、费用、归属、周期及同步证据；默认收益区保持三类指标，详情不自动复制到下一次记忆正文

#### Scenario: Historical snapshot is expanded
- **WHEN** 用户查看旧记忆关联的历史快照
- **THEN** 标明其历史时间，保留原证据，不把它替换为实时账户状态或用其覆盖当前持仓

### Requirement: Availability separates capability authority and execution evidence
Agent、MCP 和前端 SHALL 分别表达资源是否在可见范围内、管理权是否可认领或已持有、调用者权限、目标原生能力和本次动作依赖的证据状态。可自动认领 SHALL 不要求模型先调用接管工具；只读调用者 SHALL 不因可见而获得写入权限。支持动作是能力说明，实际提交前仍重新核验。

#### Scenario: The owner manages a human amended order
- **WHEN** 人工修改本管理者范围内挂单的价格和数量，最新状态已经核验
- **THEN** 展示最新参数、外部变化和支持动作，不能因原参数变化或不是 Agent 创建而将其显示为永不可修改

#### Scenario: An unrelated incomplete observation exists
- **WHEN** 订单 A 的最终成交待核验，但订单 B 的指定撤单依赖已完整核实
- **THEN** 状态指出 A 的具体限制，B 仍展示按目标执行核验的能力，不用全局 cancel/edit 禁令替代实际依赖

### Requirement: Default tool results preserve compact actionable facts
默认模型工具结果 SHALL 返回精简的逐项动作事实，保留动作索引、操作/订单身份、操作状态与订单状态、数量模式和已知成交、具体失败或依赖阻塞及按需查询指引。整批账户摘要 SHALL 最多出现一次且只含请求的当前信息；完整原始证据 SHALL 继续可按授权查询，不逐项重复嵌入。

#### Scenario: A twenty action paper batch
- **WHEN** 二十项 paper 动作都有已保存回执，调用者请求账户摘要
- **THEN** 默认结果包含二十项精简真实结果和一份末尾摘要，不出现二十份账户全量快照；未知字段和必要核验信息不被省略成成功

#### Scenario: Failed and unexecuted steps remain distinguishable
- **WHEN** 第一步撤单待核验，默认停止策略使后续开仓未执行
- **THEN** 模型结果准确区分撤单 pending 与开仓 not_executed，保留原操作查询身份，不把整批写成全部失败或全部已提交

#### Scenario: Complete evidence requested explicitly
- **WHEN** 有权限的调用者明确请求原操作完整详情
- **THEN** 返回持久化完整回执及实际证据时间，默认投影的精简不删除原始事实，也不因详情查询恢复任何交易写入
