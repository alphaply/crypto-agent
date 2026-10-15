# Spec Delta

## MODIFIED Requirements

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
- **THEN** 金额表示逐仓保证金且手续费另计，遵守账户最大杠杆及维护保证金限制，不把它解释为不带杠杆的名义金额

#### Scenario: Same instrument is available on several venues
- **WHEN** 用户旧账本在多个场所有同名标的，新动作需要匹配目标
- **THEN** 仅可执行可证明为 BN 且符合动作能力的目标；非 BN 与来源不明资源被过滤，不以 BN 同名资源替代被引用的旧目标，回执与完整原审计保留真实 venue

#### Scenario: Binance public quote is unavailable
- **WHEN** 当前合法 BN 动作的公开行情在预算内不可用
- **THEN** 返回明确依赖失败或未知执行事实，不使用其他场所报价填补，不以缓存缺项伪造市场成交

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
