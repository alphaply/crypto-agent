## ADDED Requirements

### Requirement: Memory consolidation consumes the confirmed rolling sources
短期记忆 SHALL 在 Agent 决策与工具执行结束、本轮策略压缩成功保存后更新。自动及手动整理 SHALL 使用最近 4 小时内的压缩摘要（自动整理包含本轮且仅一次）、旧版最近 7 天成交/平仓操作记录及上一轮短期记忆文本，不单独获取或注入新闻。记录范围沿用原任务、账户、标的与模式，缺失事实不补零、不换数据源。

#### Scenario: More than four summaries in four hours
- **WHEN** 截至本轮的 4 小时内有 5 条已保存压缩摘要
- **THEN** 使用窗口内 5 条且本轮仅出现一次，不误选最近 4 条；消息面仅来自已有摘要和记忆

#### Scenario: Historical operations have a different window
- **WHEN** 操作发生在 4 小时之前但仍在旧版 7 天记录范围内
- **THEN** 按原成交/平仓口径提供该记录，不把操作记录缩为 4 小时，不扩展为全量工具请求日志

#### Scenario: Summary generation fails
- **WHEN** 本轮策略压缩失败或尚未保存
- **THEN** 不以未完成压缩触发该轮记忆更新，保留原有效记忆

## MODIFIED Requirements

### Requirement: Memory resolves execution concerns by identity and evidence
执行层 SHALL 取得被引用操作的最新可用证据，按账户、任务、标的、方向、周期和订单/操作身份对应；记忆模型只整理提供的历史素材，不负责操作引用解析或恢复。旧疑虑被较新证据解决后 SHALL 从决策可见的当前异常中退出，即使原事件已在摘要窗口外。无匹配、证据不完整或查询失败不得推定解决；原始记忆和回执保留可追溯。

#### Scenario: A restriction resolves outside the four-hour window
- **WHEN** 旧记忆引用四小时前的 unknown 操作，当前证据已确认其成交、相关清理及残留影响均解决
- **THEN** 下一次决策不再获得该操作导致的“不新开仓”限制，执行层使用原身份的解决证据，历史记忆不构成当前限制

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
决策可见的短期记忆和近期已发布策略摘要 SHALL 作为历史背景提供，执行引用由程序独立保存。市场假设、价位、持仓理由、策略等待条件及历史教训可以保留；持仓/订单现状和执行限制由当前状态视图统一提供。已解决的执行限制 SHALL NOT 经旧摘要、记忆失败回退或自定义占位符重新进入默认输入；源正文与摘要完整保留，不要求模型额外生成策略投影，不按字符硬截断。近期摘要 SHALL 选取最近三条有效已发布策略内容，保留真实来源时间，不把较旧的已发布内容表述为最新三轮决策。

#### Scenario: Keep a strategy condition while removing a resolved execution ban
- **WHEN** 原文同时包含“等四小时收盘确认”和“旧订单 unknown，因此禁止加仓”，后者已核验解决
- **THEN** 策略等待条件保留，旧执行禁令消失，当前异常按最新证据生成

#### Scenario: Recent summaries cannot resurrect a resolved issue
- **WHEN** 短期记忆已更新，但近期摘要仍记载相同旧禁令
- **THEN** 默认近期策略背景中的历史描述不成为当前执行禁令，原始摘要仍可在历史记录中查看

#### Scenario: Background resolution does not wait for a language model
- **WHEN** 执行证据已解决事项，但记忆整理模型失败或队列尚未运行
- **THEN** 下一次决策读取时相应执行限制已经退出，仍有效的已发布策略内容继续使用，不回退注入旧自由文本

### Requirement: Legacy narrative transition is explicit and non-destructive
旧记忆及近期摘要 SHALL 保留原文，并保留原有有效已发布内容及来源关联；新的纯文本记忆直接保存和读取。未完成投影的旧执行描述 SHALL NOT 自动成为当前事实或许可依据；无法关联的历史疑虑保持未解决记录，其当前影响只依据可核验的范围呈现。升级及整理失败 SHALL 保留已有有效策略内容，不阻止当前状态读取、触发交易重放或删除旧证据。不再为旧 JSON 投影契约启动后台转换或定向恢复，后续正常记忆整理使用已确认的三类素材，管理端区分未生成、排队、失败及可重试状态，Prompt 不逐条注入整理占位符。

#### Scenario: First decision after upgrade
- **WHEN** 任务只有含旧全局禁令的自由文本记忆，尚无可用策略视图
- **THEN** 当前持仓、挂单和可核验异常正常呈现；旧混合正文不直接作为当前状态注入，已有有效背景继续读取；后续正常记忆整理生成纯文本，不另起历史投影转换，管理端可查看及重试失败

#### Scenario: Manual edit invalidates the derived view
- **WHEN** 用户修改或删除作为投影来源的记忆或摘要正文
- **THEN** 原文按现有编辑行为保存或删除，对应派生旧内容失效；不能冒用旧来源版本或覆盖较新的人工修改，其他有效来源仍可使用

### Requirement: Strategy sources and execution evidence fail independently
短期记忆、近期策略摘要及执行层证据读取 SHALL 分别处理失败。一项读取或解析失败 SHALL NOT 隐藏其他已读取的有效策略内容。执行证据不可用仍按原身份报告不确定性，不推定旧事项已解决，也不把历史文字重新升级为当前交易许可或禁令。

#### Scenario: Execution reference lookup fails
- **WHEN** 记忆和摘要可读，但原操作证据查询失败
- **THEN** 策略正文保留，执行层相关异常说明证据不可用及范围

#### Scenario: One strategy source is unreadable
- **WHEN** 记忆或摘要中的一个来源损坏或读取失败
- **THEN** 其余有效来源继续展示，管理端保留故障状态，不将整份背景替换为通用占位符

### Requirement: Memory consolidation prunes execution details and bounds pending sources
短期记忆整理素材中的工具回执与执行证据 SHALL 经过精简，省略内部引擎元数据（如 continuation 准入评估、blocker_order_ids、admission 判定、recovery 调试及重试计数）与未提交至交易所的瞬态参数校验报错。记忆策略素材 SHALL 使用最近 4 小时内含本轮的压缩摘要，不附加历史待转换投影队列；已取消或已完成的终态订单 SHALL 停止累加恢复尝试计数。

#### Scenario: Memory review input omits internal execution engine metadata
- **WHEN** 历史摘要中的工具操作回执包含内部 continuation、admission 与 recovery 调试字段并被注入记忆整理 Prompt
- **THEN** 记忆整理素材仅保留工具名称、动作、执行状态与关键交易事实（方向、数量、成交量、价格与当前保护状态），剥离 blocker_order_ids、continuation 规则列表、previous_assessment 及 recovery 重试元数据

#### Scenario: Transient schema validation errors are excluded from memory evidence
- **WHEN** 历史周期中发生未提交至交易所的工具参数验证错误（如 TradeActionsSchema 依赖或参数校验失败）
- **THEN** 该报错仅在决策审计日志中保留，不作为未决操作证据或阻塞订单注入短期记忆整理输入

#### Scenario: Pending historical projection sources are bounded
- **WHEN** 数据库中存在大量未完成（status='pending'）的待转换历史策略摘要
- **THEN** 新记忆整理不加载该投影队列；只读取 4 小时摘要、旧版 7 天操作记录及上一轮记忆，不用条数上限替代已确认时间窗口

#### Scenario: Terminal orders stop accumulating recovery retries
- **WHEN** 操作关联的订单已确认处于终态（如已取消或完全成交且无未决清理影响）
- **THEN** 系统将其标记为已收敛，停止对其递增 attempt_count 并移出活跃 recovery 轮询集合，回执中的尝试计数不再无限制增长

### Requirement: Memory prose contains only strategy judgments levels and lessons
新生成及默认展示的记忆正文 SHALL 用纯文本压缩整理近期操作、策略判断、关键价位和有效教训，可保留有策略意义的观察条件。operation_id、resolution、周期报错、执行解锁流程、当前仓位/挂单快照和原始账本 JSON SHALL NOT 写入记忆正文。当前交易事实 SHALL 来自独立的最新状态；原始历史证据及关联元数据 SHALL 保留在后台或按需详情，不通过旧记忆回退重新注入。

#### Scenario: Consolidate the supplied ETH memory
- **WHEN** 整理素材含两次 Edit 拒绝、缺少周期的失败批次、null 操作身份、未执行尾项及收益账本，另有四小时收盘或 CPI 观察条件
- **THEN** 新记忆保留有证据的近期操作事实、策略判断、相关价位和有效教训，移除内部字段及历史错误复述；市场观察条件保留，执行解锁清单不进入记忆

#### Scenario: Historical noisy memory remains auditable
- **WHEN** 已存记忆正文包含 operation_id、resolution 或周期诊断
- **THEN** 后续正常整理压缩为纯文本摘要，旧原文可按需审计；不再运行专用投影转换，失败保留已有有效记忆，不删除旧证据

### Requirement: Memory consolidation separates evidence plumbing from narrative input
记忆整理 SHALL 使用最近 4 小时含本轮的策略压缩摘要、旧版最近 7 天操作记录和上一轮短期记忆；不另取新闻，不拼接完整工具回执或内部同步诊断。旧版操作记录限于其既有成交/平仓口径，不扩大为所有挂撤改单及失败请求。执行引用、来源校验和恢复状态 SHALL 由程序维护，与记忆正文及策略判断分开。精简 SHALL 依据事实分类，不按字符截断策略理由或关键价位；明示未提交的失败和未执行项不得作为待恢复交易反复复述。

#### Scenario: Definite rejections and unexecuted tails
- **WHEN** 原批次确认只有提交前失败且后续项未执行，或改单因归属校验被拒
- **THEN** 原回执保留于历史，记忆不复制失败计数或操作 ID，也不要求为不存在的交易所提交执行恢复

#### Scenario: Genuine unknown submission
- **WHEN** 存在可能已发送的真实交易写入而其结果仍未知
- **THEN** 后台保留原身份并核验；其必要影响通过当前相关事项简述，不能因精简记忆而重复下单或报告已解决

### Requirement: Detailed ledgers and diagnostics are available on demand
详细成交、费用、归属缺项、周期、同步覆盖及原操作回执 SHALL 通过用户主动访问且受现有任务、账户、namespace/principal 权限约束的 HTTP/UI 详情提供，保留原来源与真实观察时间。详情 SHALL NOT 注册为模型/MCP 工具或成为交易后的必调用步骤，读取不得恢复金融写入。除记忆整理按本变更恢复的旧版 7 天操作记录外，默认记忆正文、收益摘要与例行决策输入 SHALL 不附完整资料；详情与摘要使用同一证据口径和时间，不重复计算盈亏，不改写历史 raw 记录。

#### Scenario: Expand performance details
- **WHEN** 用户主动打开收益或账本的授权 HTTP/UI 详情
- **THEN** 可查看原成交、费用、归属、周期和同步证据，默认收益区保持三类指标；详情不自动复制到下一次记忆正文，也不构成模型操作查询工具

#### Scenario: Historical snapshot is expanded
- **WHEN** 用户通过授权 HTTP/UI 查看旧记忆关联历史快照
- **THEN** 标明历史时间并保留原证据，不替换为实时账户状态或覆盖当前持仓，不因打开详情提交、撤销、修改订单

#### Scenario: Audit access cannot disclose another principal
- **WHEN** 用户请求不在其任务、账户或原 namespace/principal 授权范围的操作原文
- **THEN** 现有权限边界拒绝访问，精简回执及详情不泄露其他主体身份；移除操作查询工具不扩大 HTTP/UI 审计权限

### Requirement: Default tool results preserve compact actionable facts
新旧默认模型工具结果 SHALL 为按请求顺序排列的简短文本，每项只呈现动作、目标及必要数量价格、真实结果和失败/未执行原因；接口超时显示请求失败及具体原因，并简述提交结果未知、请勿重复提交；不在调用内追加核验，后台核验和必要前置检查保留。已知订单身份、数量模式与成交事实保留；内部父子操作关联和客户端身份保存在结构化证据中，不重复展示。单项 SHALL 不超过4096字节、最多20项整批不超过32768字节，以返回文本 UTF-8 计量；不重复 previous_assessment、规则或 blocker 树。整批账户摘要 SHALL 最多一次且只含请求的当前信息。完整原始 JSON 和后续证据 SHALL 保留于授权 HTTP/UI 详情，不逐项嵌入或删除原事实。

#### Scenario: A twenty action paper batch
- **WHEN** 二十项 paper 动作都有已保存回执，调用者请求账户摘要
- **THEN** 默认整批 UTF-8 不超过32768字节，包含二十项精简真实结果与一份末尾摘要，不出现二十份账户快照或重复评估树；必要订单身份和未知事实不被省略成成功

#### Scenario: Failed and unexecuted steps remain distinguishable
- **WHEN** 第一步撤单待核验，默认停止策略使后续开仓未执行
- **THEN** 模型文本区分 cancel 请求失败（提交结果未知）与 open 未执行，内部保留 pending/unknown、原身份及后台核验，文本简述原因及不可重发，不把整批写成全部失败/已提交，不要求操作查询

#### Scenario: Complete evidence requested explicitly
- **WHEN** 用户在现有权限下通过 HTTP/UI 明确请求原操作完整详情
- **THEN** 返回持久化原始回执及增量证据和真实观察时间，不恢复金融写入；该详情不是另一个模型/MCP 工具，不自动进入默认模型结果

#### Scenario: A noisy legacy receipt is projected within the item budget
- **WHEN** 旧金融回执包含重复 previous_assessment、blocked_actions、规则及相同 blocker
- **THEN** 默认单项 UTF-8 不超过4096字节，只保留当前有影响的去重限制、真实状态和必要身份；原始 JSON 不改写，默认结果不附 get_trade_operation/get_operation 调用步骤

### Requirement: Memory review has one effective output contract
所有记忆整理模式 SHALL 只输出一份纯文本摘要，不要求 summary、memory、projections、来源哈希、执行引用数组或 JSON 外壳。系统 SHALL 保存完整摘要文本，只验证非空、响应完成及无工具调用等基本响应条件，不因缺少旧结构字段或未符合“纯策略”句式而拒绝。失败保留原有效记忆；不执行规则工具。

#### Scenario: Independent memory request contains no competing built-in format
- **WHEN** 独立退出任务构造记忆请求
- **THEN** 只要求整理后的纯文本，不附旧投影 JSON 示例或格式纠正指令

#### Scenario: Legacy custom prompt uses the summary-only example
- **WHEN** 已保存自定义模板要求旧 JSON 输出
- **THEN** 程序明确本次以纯文本输出，不改写用户保存的模板，也不为旧契约要求模型二次纠正

#### Scenario: No historical sources need conversion
- **WHEN** 没有历史来源需要转换
- **THEN** 仍仅输出本次摘要，不要求空 projections 或 memory 对象

#### Scenario: Non-independent memory remains compatible
- **WHEN** 其他已有记忆模式完成整理
- **THEN** 同样保存纯文本，不要求单 summary JSON，也不启用规则维护

#### Scenario: Failed response preserves previous memory
- **WHEN** 模型返回空内容、截断、拒绝或工具调用
- **THEN** 不发布该结果，保留旧有效文本及错误记录，不补默认摘要


## REMOVED Requirements

### Requirement: Projection format correction is bounded and auditable
**Reason**: 用户要求纯文本摘要并删除为旧 JSON 投影服务的复杂校验与恢复。
**Migration**: 停止该契约的新任务和定向重排；保留历史诊断及已发布内容，后续正常整理采用纯文本，不重跑交易或批量删除历史。

### Requirement: Legacy projection format failures recover once per contract revision
**Reason**: 用户要求纯文本摘要并删除为旧 JSON 投影服务的复杂校验与恢复。
**Migration**: 停止该契约的新任务和定向重排；保留历史诊断及已发布内容，后续正常整理采用纯文本，不重跑交易或批量删除历史。
