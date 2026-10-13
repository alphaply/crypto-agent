# Spec Delta

## MODIFIED Requirements

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

## ADDED Requirements

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
