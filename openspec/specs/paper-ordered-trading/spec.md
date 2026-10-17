# paper-ordered-trading Specification

## Purpose

让 Paper 客户端通过一次有类型的交易动作调用完成按序的开户后交易意图，沿用真实公开行情、模拟账户和既有保护规则。以持久化父子操作身份、逐项账户事务和确定的中断恢复约束，减少模型往返并保证批次重放、并发和超时不会重复成交或扩大账户授权。

## Requirements

### Requirement: Paper uses the unified versioned execution envelope
Paper SHALL 通过默认 execute_trade_actions 的 contract_version=3 契约接受一至二十项有类型的交易动作，并使用与其他模式相同的顺序、策略、依赖及回执外层契约。动作能力 SHALL 由 Paper 账户、市场及公开配置确定，单笔 SHALL 使用一项数组；不得接受任意代码、前项动态结果表达式或等待未来成交的交易计划。

#### Scenario: One call cancels and replaces a known paper order
- **WHEN** 请求先 cancel 已知订单 ID，再提交使用已知参数的新订单动作
- **THEN** 同一次调用按请求顺序执行并一次返回逐项结果，客户端无需在两项之间发起另一轮模型调用

#### Scenario: Client sends unsupported orchestration syntax
- **WHEN** v3 请求携带任意代码、前项结果引用、后台等待成交指令或超过二十项的数组
- **THEN** 在首次金融写入前拒绝，说明不支持的输入，不将其解释为可执行程序

### Requirement: Execution domain and account scope come from trusted authorization
Paper 执行 SHALL 由可信 key 类型决定，namespace SHALL 来自该 key，目标 user_id SHALL 是获授权的可信账户身份，场所 SHALL 受服务端 BN-only 策略约束。请求 SHALL 不得通过 engine、namespace、config_id、principal 或 exchange 切换引擎、范围或身份。所有交易、订单、回执、后台核验及授权 HTTP/UI 审计 SHALL 验证同一 namespace/user 范围，Paper SHALL 不调用交易所私有交易 API 或其他场所公开 API。

#### Scenario: Paper request attempts to select live execution
- **WHEN** Paper key 的动作请求携带 live 引擎、真实 profile 或冲突 namespace 字段
- **THEN** 请求在任何金融写入前拒绝，不访问真实交易所账户或提交真实订单

#### Scenario: Order belongs to another paper account
- **WHEN** 当前 namespace/user 的动作引用其他 namespace 或其他 user 的订单 ID
- **THEN** 返回目标范围错误或不可见事实，不撤改单、不泄露其他账户私人回执，也不因订单可搜索而获得管理权

#### Scenario: Authorized account references an excluded venue
- **WHEN** 同账户的新动作显式选择非 BN 或引用可证明非 BN/无法证明为 BN 的历史目标
- **THEN** 返回场所范围错误或来源不明事实，不连接其他场所、不替换为同名 BN 资源，也不改变原目标管理权、状态或资金

### Requirement: Batch preflight precedes all financial effects
Paper SHALL 在首次金融写入前完成整批结构、权限、版本、策略、依赖、单位、已知模式能力及 BN 场所/已知目标来源验证。当前余额、预留、订单状态和持仓数量 SHALL 在每项实际执行时重新准入，不能因尚未发生的资金释放而提前通过，也不能把可由前项释放的资源按初始快照永久拒绝。金融动作 SHALL 要求 read 与 trade 权限，过滤不释放原账本承诺。

#### Scenario: Later action has an invalid sizing combination
- **WHEN** 批次第一项有效，后项同时指定基础币数量和保证金金额
- **THEN** 整批在首次金融写入前失败，第一项未执行，错误指出互斥数量字段

#### Scenario: Cancellation releases resources for a later entry
- **WHEN** BN 原挂单预留导致当前可用现金不足，但首项撤单已确认并释放预留，第二项重新开仓
- **THEN** 第二项按撤单提交后的真实可用现金和全部其他承诺重新检查，可以在额度足够时执行，不沿用批次开始前的不足判断

#### Scenario: Read-only key submits an otherwise valid batch
- **WHEN** Paper key 只有 read 权限
- **THEN** 整批在首次金融写入前因缺少 trade 被拒绝，撤单也不会借用 live cancel 权限执行

#### Scenario: Later action targets a legacy non Binance order
- **WHEN** 首项是合法 BN 动作，后项 edit/cancel 引用同账户的非 BN 历史订单
- **THEN** 整批在首次金融写入前拒绝，不先执行首项或先释放旧订单预留，原订单仍按过滤策略保留

### Requirement: Parent intent and bounded stable child identities precede mutation
Paper SHALL 在首次金融写入前持久化父操作完整意图、契约版本、顺序、策略、依赖及全部子项身份。子操作 ID SHALL 在允许长度内由账户域和父操作/索引稳定产生并持久化映射，不能简单向最长父 ID 追加索引。恢复 SHALL 沿用逻辑父 ID；同一账户复用父 ID 且意图变化 SHALL 拒绝，持久化失败 SHALL 阻止金融写入。

#### Scenario: Parent operation ID has the maximum allowed length
- **WHEN** 有效父 operation_id 已达到长度上限并包含多项动作
- **THEN** 所有稳定子 ID 仍符合长度和字符限制，映射可恢复，各子项不会因索引拼接超长而在部分写入后失败

#### Scenario: Identical batch is retried after key rotation
- **WHEN** 仍获授权于同一 Paper namespace/user 的客户端用原父 ID 和完全相同意图重试
- **THEN** 读取原父子映射及回执，不重新生成身份或再次执行已尝试项

#### Scenario: Retry changes action order or dependency policy
- **WHEN** 客户端复用原父 ID 但改变动作参数、顺序、execution_policy 或 depends_on
- **THEN** 返回意图冲突，不以新默认值覆盖原意图，也不通过另一适配器重下单

### Requirement: Each paper action has an atomic account commit
每项 Paper 动作的账户变化、订单状态、预留变化、事件及子操作回执 SHALL 原子提交或原子回滚。整批 SHALL 非原子，后项失败不得回滚已提交前项。账户事务 SHALL 不等待外部行情或其他网络调用；提交时 SHALL 重新验证相关账户和目标事实，避免行情获取期间的并发变化覆盖已提交状态。

#### Scenario: Edit fails after its old reservation was tentatively removed
- **WHEN** 一项改单重新校验后发现余额或订单状态不满足条件
- **THEN** 该项账户变化、订单变化和预留调整全部回滚，不留下部分编辑状态；父批次按策略记录该项失败

#### Scenario: Later market trade fails after a successful cancellation
- **WHEN** 前项撤单及预留释放已提交，后项交易因报价失效失败
- **THEN** 保留前项撤单事实和释放后的账户状态，后项没有成交，批次不被描述为全部回滚或全部成功

#### Scenario: Public quote is slow while another account is active
- **WHEN** 某项等待公开网络报价，其他账户或同账户既有订单发生合法状态变化
- **THEN** 网络等待不占用账户写事务；当前项提交时重新读取相关状态，目标变化导致明确拒绝或重新准入，不覆盖并发结果

### Requirement: Failures before mutation produce durable factual receipts
对已授权且操作身份有效的请求，首次金融写入前的验证、报价或账户准入失败 SHALL 持久化为 failed 回执，明确未产生该项金融写入。若无法持久化意图或失败事实，系统 SHALL 停止金融写入并报告持久化问题；不得把失败请求换 ID 自动重试，或将公开行情读取失败误报为可能已真实下单。

#### Scenario: Public market lookup fails before account mutation
- **WHEN** 子项已登记但行情源不可用或报价过期
- **THEN** 保存该项 failed、未成交及失败阶段，查询父操作可看到失败事实，后续按策略处理

#### Scenario: Preflight rejects a validly identified parent request
- **WHEN** 已授权且 operation_id 有效的整批请求在金融写入前因不支持能力被拒绝
- **THEN** 持久化父失败回执和未执行事实，相同 ID 的相同请求返回该结果，不在恢复后悄然执行

#### Scenario: Intent storage is unavailable
- **WHEN** 无法持久化父子意图
- **THEN** 不修改账户、订单或预留，返回保留逻辑操作 ID 的持久化错误，不开始任何金融动作

### Requirement: Ordered policies retain explicit dependencies and dynamic admission
Paper SHALL 按原请求顺序执行。省略 execution_policy 或指定 stop_on_error 时，failed、pending、unknown 或被阻塞项 SHALL 停止尾部并标记 not_executed。显式 independent SHALL 只继续依赖满足且实际准入有效的其他项；depends_on SHALL 仅引用不重复的此前整数索引且要求 completed，不能以 submitted 或未经核验状态满足依赖。

#### Scenario: Cancel fails before its dependent replacement
- **WHEN** independent 批次中新建订单显式依赖前项 cancel，而该 cancel 失败或尚未 completed
- **THEN** 新建项标记 not_executed 并指出依赖；无此依赖且实际账户证据满足准入的其他项仍按序评估

#### Scenario: Dependencies are invalid
- **WHEN** depends_on 包含 null、布尔值、重复项、自身、后向或越界索引，或 stop_on_error 中出现依赖
- **THEN** 整批在首次金融写入前拒绝，不自行修正索引或推断执行策略

#### Scenario: Independent action is affected by uncertain account state
- **WHEN** 前项账户变化仍未核实，后项需要该项可能影响的现金、预留或持仓
- **THEN** 后项不得因未写 depends_on 而绕过实际准入，返回阻塞事实而不假定前项成功或失败

### Requirement: Replay and crash recovery never resume an unexecuted tail
父操作重放或恢复 SHALL 仅查询或核验已尝试项并重建真实结果，未尝试及原 not_executed 尾项 SHALL 保持未执行。子项账户提交与回执已存在时 SHALL 不再运行该项；父摘要缺失 SHALL 不改变此规则。需要执行剩余意图 SHALL 使用新的显式请求，原批次 SHALL 不因查询、重启或 HTTP 重试自动续跑。

#### Scenario: Worker crashes after a child commit before returning the parent summary
- **WHEN** 子项账户变化、事件和回执已原子提交，父响应或摘要更新尚未完成时进程中断
- **THEN** 恢复从子回执重建该项已提交事实，不再次成交；尚未尝试的尾项标记 not_executed，不随重试自动执行

#### Scenario: Worker crashes after parent reservation but before the first action
- **WHEN** 父子意图已保存但没有任何子项被尝试，原执行进程已确认终止
- **THEN** 恢复显示未产生金融写入并保留全部未执行项，不将查询或相同请求重放当作启动整批的命令

#### Scenario: Retried terminal parent contains skipped tail items
- **WHEN** 重放一个前项失败且尾项 not_executed 的已完成父回执
- **THEN** 返回原结果及必要的已尝试项核验，不因账户后来获得资金或标的后来可用而补发尾项

### Requirement: Concurrent retries and matching preserve one financial effect
同一父操作的并发调用 SHALL 共享一个持久化执行身份，且每一子项最多产生一次账户金融效果。不同操作或后台撮合对同账户资源 SHALL 在提交时重新核验；改单 SHALL 识别目标变化，撤单 SHALL 返回真实终态，不能将已经成交的订单重新变为未成交或取消其历史成交。

#### Scenario: Two clients submit the same parent simultaneously
- **WHEN** 相同 namespace/user 下两个客户端同时提交相同父 ID 和意图
- **THEN** 只有一个逻辑执行产生子项金融效果，另一调用读取进行中或已保存的同一结果，不重复事件、成交或预留

#### Scenario: Matcher fills an order before a queued edit commits
- **WHEN** 行情获取期间后台撮合使待改单成交或改变目标版本
- **THEN** 当前 edit 依据最新状态拒绝或返回真实不可编辑事实，不复活已成交订单或覆盖撮合结果

#### Scenario: Cancel targets an already terminal order
- **WHEN** 目标订单已成交或已取消且归属当前账户
- **THEN** 回执保留该订单真实终态和已知成交，明确是否发生变化，不把先前成交报告为本次取消成功

### Requirement: Budget and disconnect outcomes are decided by commit evidence
整批 SHALL 使用一次有界预算，不为每项重置；账户/订单提交后及时返回真实回执，不要求模型再调用操作查询或等待限价未来成交。断线不证明回滚，pending/unknown SHALL 按权威提交证据区分。预算耗尽 SHALL 保留已提交结果、原父子身份及未尝试尾项，后台只核验原事实，不自动续跑或重放金融变化。

#### Scenario: Budget expires after an earlier committed action
- **WHEN** 前项已提交，下一项执行前预算耗尽
- **THEN** 前项真实结果保留，尾项 not_executed，报告预算阶段，不抹去已成交或换 ID 重跑，不要求第二个工具获取许可

#### Scenario: HTTP connection ends while the worker is still active
- **WHEN** 客户端断开而原子项仍在处理中
- **THEN** 后台沿原身份记录进行中事实，不启动第二次金融执行，也不只凭断线宣布失败；后续状态自动进入上下文及授权界面

#### Scenario: Commit acknowledgement is unavailable
- **WHEN** 子项可能提交但当前无法读取权威账户回执
- **THEN** 返回 unknown 及原身份，后台核验原提交，不把不可读解释为无成交重发，亦不把核验工作交给模型查询工具

### Requirement: Paper market execution and sizing retain existing meanings
Paper SHALL 仅读取 Binance 已核验且受现有能力支持的生产公开行情模拟成交，并保留 USDT 现货和线性永续、费用、杠杆及逐仓约束。基础币 amount、USDT quote_amount、开仓 margin_amount 和退出 close_all SHALL 按动作能力互斥；现货 SHALL 不接受杠杆，预算数量 SHALL 不用于退出，回执 SHALL 保留单位、venue 与真实行情来源。来源失败 SHALL 不跨场所回退。

#### Scenario: Spot buy uses a quote budget
- **WHEN** 现货买入选择 quote_amount
- **THEN** 按包含现货买入手续费的 USDT 预算计算基础币数量，不将金额解释为基础币或合约张数

#### Scenario: Perpetual entry uses margin sizing
- **WHEN** 永续开仓选择 margin_amount 并指定有效杠杆
- **THEN** 金额作为包含开仓手续费的总成本预算，系统自动根据杠杆与手续费率倒推持仓保证金与开仓手续费，两者扣款之和严格等于该金额，遵守账户最大杠杆及维持保证金限制，避免额外扣费导致现金透支

#### Scenario: Same instrument is available on several venues
- **WHEN** 用户旧账本在多个场所有同名标的，新动作需要匹配目标
- **THEN** 仅可执行可证明为 BN 且符合动作能力的目标；非 BN 与来源不明资源被过滤，不以 BN 同名资源替代被引用的旧目标，回执与完整原审计保留真实 venue

#### Scenario: Binance public quote is unavailable
- **WHEN** 当前合法 BN 动作的公开行情在预算内不可用
- **THEN** 返回明确依赖失败或未知执行事实，不使用其他场所报价填补，不以缓存缺项伪造市场成交

### Requirement: Accepted orders remain distinct from fills and protection preserves paper cycles
限价和条件单接受 SHALL 不等于成交，后项 SHALL 不把未成交入场当作已持仓。Paper SHALL 保留部分退出、当前周期 close_all、可选入场 TP/SL、成交后激活附带保护、单腿及成对保护、省略已有腿保持原值和 OCO 原子替换/互斥语义；Paper 周期跟随退出 SHALL 不被解释为 live 的未来同方向全平能力，已结束周期的旧退出 SHALL 不影响后来重开仓位。

#### Scenario: Limit entry is followed by immediate position protection
- **WHEN** 前项限价开仓仅被接受且未成交，后项请求针对持仓设置保护
- **THEN** 不凭 accepted 创建不存在仓位的保护；入场附带的已支持保护仅在实际成交后激活，后项按真实已成交敞口准入

#### Scenario: One OCO protection leg fills
- **WHEN** Paper TP/SL 保护一腿成交
- **THEN** 同一账户事务取消另一腿并按实际成交调整持仓和事件，不在第二腿触发时再次平仓

#### Scenario: A single protection leg is set or updated
- **WHEN** protect 只请求一腿，或已有 TP/SL 时只修改 stop_loss
- **THEN** 原有单腿能力可用，省略的已有 take_profit 保留其值；固定部分量、当前周期 all 和费用规则按原语义处理

#### Scenario: Close-all ends a cycle before the side is reopened
- **WHEN** 前项对当前周期全平并清理其旧退出，后项重新开同方向仓位
- **THEN** 新仓位使用新周期，旧 close_all 或旧 OCO 不能平掉该新周期，部分和全量退出含义保持 Paper 原规则

### Requirement: Compact paper receipts retain full recovery evidence
Paper 默认结果 SHALL 返回原父子身份、索引、真实操作/订单状态、成交量和必要预留变化，遵循单项 4096 字节、整批 32768 字节的 UTF-8 上限；账户摘要最多一次。完整回执、事件及订单 SHALL 持久化并可由授权 HTTP/UI 详情读取。后台核验 SHALL 自动更新状态，不泄露其他账户身份，不引导 get_operation 或伪装的二次查询工具。

#### Scenario: Several paper steps complete in one call
- **WHEN** 多项 Paper 动作完成并请求账户摘要
- **THEN** 一次返回精简逐项事实和一次摘要，接受订单与成交区分，不重复快照或附二次核验任务

#### Scenario: Client queries a parent after losing its response
- **WHEN** 同账户用户通过授权 HTTP/UI 查看丢失响应的原父操作记录
- **THEN** 可核对已由后台恢复的父子顺序、已尝试结果、未执行尾项和订单身份，不依赖 get_operation、哈希子 ID 或另一金融写入

### Requirement: Legacy paper operations preserve original financial intent
既有 Paper 单项适配器、操作 ID、回执及订单 SHALL 保留原含义与已保存重放事实。v3 SHALL 不重新解释旧 amount、close_all、保护或开户请求，不因入口合并或场所过滤改变原指纹、完成/失败状态。新兼容金融请求 SHALL 遵循 BN-only；范围外原操作 SHALL 只读核验已存事实，未知状态不强行终结、不联网或续跑。bank、账户配置和竞赛 SHALL 继续可达并保留完整资金承诺。

#### Scenario: Old paper operation is retried through its original adapter
- **WHEN** 客户端重试升级前已持久化的 Paper 操作及相同参数
- **THEN** 返回或核验原回执，不重新请求市场成交、不转换成新批次写入，也不改变原账户资金；非 BN 原记录保留原身份并说明过滤事实

#### Scenario: User changes a preference or borrows funds
- **WHEN** 用户通过 configure_paper_account 或 paper_bank 提交获授权请求
- **THEN** 偏好和借还款保持各自已有账户、债务及幂等含义，不被开平仓批次入口删除或隐式执行，还款及可用现金继续扣除被过滤预留

#### Scenario: Legacy excluded operation remains unknown
- **WHEN** 原非 BN 子项结果未知且批次尾项未执行
- **THEN** 使用原父子 ID、索引和指纹返回持久证据及未解决原因，不到旧场所取价、不重下单，不把 unknown 改成取消或把尾项变成新执行

### Requirement: Bankruptcy admission is checked at financial commit
Paper 交易入口、兼容入口和后台订单成交 SHALL 在提交金融变化时检查账户破产状态。破产后 SHALL 拒绝新的用户金融操作，并停止已有订单成交；读取及原事实核验 SHALL 不被误当作新交易。并发破产状态更新 SHALL 不被较早的准入结果覆盖。持有有效持仓但净资产小于等于零的未破产账户，准入检查 SHALL 允许平仓与退出订单撮合，仅拒绝新增开仓。

#### Scenario: Bankruptcy races with an order fill
- **WHEN** 报价已读取，订单成交提交前账户已进入破产状态
- **THEN** 提交前重新核验状态，不产生成交或覆盖破产状态，旧订单保持未成交事实

#### Scenario: Legacy endpoint tries to bypass the freeze
- **WHEN** 破产账户通过隐藏旧交易入口请求开平仓、撤改单或保护操作
- **THEN** 与新入口一样拒绝，不因 schema、旧工具名或管理员停用字段不同而绕过破产限制

#### Scenario: Negative net equity account executes exit operations
- **WHEN** 账户仍持有有效持仓但账面净资产小于等于零，请求平仓、追加/撤销止盈止损或其退出订单满足触发条件
- **THEN** 金融提交准入允许该操作执行并完成撮合，不被误作为破产冻结拦截

### Requirement: Reset isolates new funds from old trading identities
下海重置 SHALL 建立新的账户周期并终止旧周期待执行资源。已持久化的父子身份、原回执和历史 SHALL 保留。旧周期操作的重试、后台恢复、撮合或异步结果 SHALL 不得再次成交、释放新周期预留、覆盖新周期状态或支配重置补款。

#### Scenario: Old batch is replayed after reset
- **WHEN** 用户下海后重放旧周期已保存的批次或旧订单事件晚到
- **THEN** 返回或核验原持久事实，不重新执行旧动作，不改变新现金、收益率、挂单、持仓或下海次数

#### Scenario: Reset races with matching
- **WHEN** 同一账户的下海重置与后台撮合并发
- **THEN** 账户周期与状态按同一金融提交边界串行核验，重置成功后不出现旧订单对新周期的成交
