# paper-binance-scope Specification

## Purpose

将 Paper 的公开行情、标的发现、模拟交易和后台维护限制在 Binance 现有支持范围，避免隐式跨场所读取造成额外资源消耗。历史其他场所及来源不明数据仅退出默认展示和活跃处理，资金承诺、原操作身份和完整审计继续保留，不通过过滤制造可用资金、收益或另一笔成交。

## Requirements

### Requirement: Paper venue policy is trusted and independent from live profiles
Paper SHALL 仅使用 Binance 已核验且符合既有能力的 USDT 现货及线性永续市场。策略 SHALL 来自服务端可信配置，适用于公开行情和模拟账本，不得由客户端、旧配置或别名覆盖。该策略 SHALL 不改变原入口鉴权或 live profile 的交易所、授权及真实订单管理能力，也不新增 Binance 市场类型。

#### Scenario: Paper requests a supported Binance instrument
- **WHEN** Paper 请求 Binance 已核验且既有能力支持的 USDT 现货或线性永续标的
- **THEN** 按原市场、数量、费用及权限规则处理，不因 BN-only 扩张为其他报价币、反向合约或底层股票现货

#### Scenario: Existing live profile uses another exchange
- **WHEN** 已授权 live profile 使用其他受支持交易所执行查询或交易
- **THEN** Paper 策略不改写或停用该 profile，其调用继续遵循自己的能力和管理权

### Requirement: All paper entry points enforce the Binance boundary before effects
新旧 MCP 名称、Paper HTTP 入口、显式 exchange、批次动作及配置保存或导入 SHALL 遵循同一 BN-only 边界。非 BN 请求 SHALL 在创建其他场所客户端、联网或首次金融影响前明确拒绝；批次 SHALL 整批完成场所静态校验。已有其他场所配置 SHALL 不作为隐式授权，也不得把其历史资源重标为 Binance。

#### Scenario: Hidden compatibility adapter explicitly selects another venue
- **WHEN** 已授权客户端通过旧隐藏入口提交新的非 BN 行情或交易请求
- **THEN** 返回明确的不支持场所原因，不创建该场所客户端、不联网、不修改账户或释放预留

#### Scenario: Later action violates the venue policy
- **WHEN** v3 批次前项有效而后项显式指定其他场所
- **THEN** 整批在首次金融影响前失败，前项未执行，保留原操作身份和静态失败事实

#### Scenario: Imported paper settings select another exchange
- **WHEN** 配置保存或导入尝试启用其他场所作为 Paper 来源
- **THEN** 明确拒绝该场所配置，不将其转换成可执行的其他场所策略；已有历史配置不能绕过当前有效 BN-only 策略

#### Scenario: Persisted legacy default selects another exchange
- **WHEN** 升级前合法保存的 PaperSettings 仍选择非 BN 交易所
- **THEN** 兼容加载保留原配置证据并公布有效 BN 策略，不因旧枚举值使身份校验、银行、偏好或原 ID 审计不可达，不用旧默认或新 BN 默认回填历史资源来源

### Requirement: Discovery and quote failure never fan out to another venue
Paper 搜索和报价 SHALL 仅读取 Binance，省略场所时也不得遍历其他交易所。Binance 报价、目录或可用性失败 SHALL 不触发跨场所 fallback，且 SHALL 区分来源不可用、范围不支持和已核验未找到。股票代币与别名提示 SHALL 不被当作上市证明，不扩张当前 Binance 支持能力。

#### Scenario: Binance quote fails with no explicit venue
- **WHEN** 未指定 exchange 的行情请求无法从 Binance 取得报价
- **THEN** 返回该来源的失败或不完整事实，不搜索、报价或建议已执行其他场所交易，也不声称标的全球不存在

#### Scenario: Alias exists only outside the supported Binance catalogue
- **WHEN** 用户搜索一个存在其他场所提示但未获当前 Binance 目录核验的名称
- **THEN** 不把提示列为可交易 Binance 市场，也不为验证提示而连接其他交易所

### Requirement: Historical filtering changes projections rather than the raw ledger
历史非 BN 及无法证明为 BN 的 Paper 仓位、订单和交易事件 SHALL 从默认账户、订单、历史、通知和后台业务视图过滤，并退出活跃扫描集合。过滤 SHALL 不删除原始账户、订单、事件或回执，不变更原场所、金额、状态、来源和金融事实；受原权限保护的审计读取 SHALL 保留完整原始数据。

#### Scenario: Mixed account is displayed after policy activation
- **WHEN** 同一账户有 BN 与非 BN 历史资源
- **THEN** 默认业务列表仅展示可证明为 BN 的资源，原非 BN 数据仍存在于完整审计，不能被显示成已平仓或已取消

#### Scenario: An administrator inspects excluded history
- **WHEN** 具备原审计权限的管理者明确读取被过滤资源的原始记录
- **THEN** 可核对其原场所、状态及资金事实，该只读审计不触发交易、维护或其他场所联网

### Requirement: Historical venue attribution requires durable evidence
历史 Paper 场所 SHALL 依据原资源/周期相关且可核对的持久订单、成交、回执及来源版本证明；可能由旧升级逻辑回填的 exchange 字段不能单独证明来源。无法证明为 BN 时 SHALL 保持来源不明并隔离，不使用当前全局默认值、相同 ticker、别名或另一周期成交猜测场所。来源恢复 SHALL 不重建资金或覆盖原始证据。

#### Scenario: Legacy position has no exchange field and no matching evidence
- **WHEN** 旧仓位未保存 exchange，且原周期证据不能证明其场所
- **THEN** 该资源被过滤并标记来源不明，不补写 Binance，不获取 Binance 行情替它估值、撮合或爆仓

#### Scenario: Original fill proves the legacy venue
- **WHEN** 与原资源和周期匹配的持久成交证据明确记录 Binance 或其他场所
- **THEN** 使用该已核实来源决定展示与维护资格，保留原证据；其他场所不会因当前默认值是 Binance 而变成 BN

#### Scenario: Legacy Binance field may have been filled by an upgrade
- **WHEN** 历史资源已存 exchange=binance，但无法证明该字段来自原金融写入，旧升级曾按默认值或同 ticker 事件回填
- **THEN** 核对该原资源/周期的可信原订单、成交或回执，无法证实时保持来源不明；不得仅凭现存字段进行 BN 估值、撮合、爆仓或交易准入

### Requirement: Filtered resources retain all financial commitments
资金准入、保证金及预留计算 SHALL 基于完整原账本，包括被过滤资源的已承诺资金和数量。过滤 SHALL 不增加现金、可用现金或可卖库存，不退回保证金、释放订单预留、生成取消或清算，也不使新 BN 动作使用被排除资源。正常 BN 交易 SHALL 仍按真实提交后的完整承诺重新准入。

#### Scenario: Hidden legacy entry reserves cash
- **WHEN** 非 BN 待成交入场被过滤但原账本仍保存现金预留
- **THEN** 该预留继续扣减可用现金，不能通过列表隐藏、重算预留或重启使下一笔 BN 入场取得这笔资金

#### Scenario: Hidden exit reserves another venue's asset
- **WHEN** 非 BN 历史退出或保护占用该场所库存或保证金
- **THEN** 该数量不转为可用于 BN 卖出或开仓的资产，原资金承诺不被自动清理

#### Scenario: A new Binance entry shares a legacy symbol and side
- **WHEN** 新 BN 入场与非 BN/来源不明的旧现货或合约资源同名且方向相同
- **THEN** 仅在能够独立寻址新 BN 资源/周期且完整资金准入满足时执行，否则目标级拒绝；不能合并数量、均价、保证金、周期或重标旧资源，新成交不能成为旧来源的追认依据

### Requirement: Bank and account mutations preserve balances and debt
开户、借还款、偏好和后台账户调整 SHALL 保留已有账户身份、现金、债务、资金来源及各自幂等规则。过滤 SHALL 不重置本金、注销债务或重复发放初始资金；银行还款 SHALL 使用扣除完整预留后的实际可用现金。非交易资金事件 SHALL 不因没有交易所字段被误判为非 BN 交易。

#### Scenario: Mixed account repays a loan after filtering
- **WHEN** 含被过滤预留的账户请求还款
- **THEN** 依据原债务和完整可用现金准入，不因非 BN 订单不再展示而允许动用其预留资金

#### Scenario: Existing account is initialized again
- **WHEN** 策略启用后重复开户或恢复 bot 账户
- **THEN** 保留原现金、债务、保证金及资金承诺，不将过滤解释为新账户或追加初始资金

### Requirement: Filtered valuation has explicit completeness semantics
Paper 返回 SHALL 明确有效场所范围及过滤或来源不明是否影响估值。完整权益、净权益和收益无法可靠计算时 SHALL 标记不完整，并返回 null 或明确历史观察值及观察时间，不以零值或删去资产后的数值冒充完整当前总额。现金、债务和已知预留 SHALL 保留其账本含义，可按完整承诺计算真实可用现金。

#### Scenario: Other venue exposure is omitted from a positions facet
- **WHEN** positions 仅显示 BN，但账户仍有被过滤或来源不明的资产
- **THEN** 响应明确其过滤范围及估值不完整；不能把现金加可见 BN 资产当作完整权益，也不返回伪造的零收益

#### Scenario: Caller requests balances without positions
- **WHEN** 混合账户仅请求余额 facet
- **THEN** 现金、债务和可用现金保持完整账本口径，估值完整性仍可辨识，不要求附上被过滤仓位明细

### Requirement: Competition excludes unverifiable mixed valuations
排行榜 SHALL 不把被过滤资产缺失、旧行情或来源不明造成的估值差额算作 BN 收益或排名优势。不能可靠计算收益及收益率的混合账户 SHALL 不参与对应排名，并提供可辨识的未排名或不完整事实；贷款、充值、债务调整及过滤 SHALL 不成为交易收益，不重算或改写历史比赛审计。

#### Scenario: Mixed account cannot produce a complete competition score
- **WHEN** 账户仍含被过滤资产或收益来源无法可靠区分
- **THEN** 不以删除资产后的权益、旧完整分数或假零值参与排名，保留其未排名原因与原比赛审计

#### Scenario: Borrowing or filtering changes the visible account projection
- **WHEN** 账户借款、后台调整或隐藏历史资源使展示发生变化
- **THEN** 变化不被计为新的 BN 盈亏，资金调整口径和债务事实保持可核对

### Requirement: Paper maintenance never processes excluded venues
Paper 估值、撮合、保护、爆仓和后台标的扫描 SHALL 只处理可证明为 BN 的资源，不对非 BN 或来源不明资源创建客户端、联网或执行资金变化。混合账户处理合法 BN 资源时 SHALL 保留被排除资源及其承诺，不能因其不在工作集合而推断周期结束、取消旧单或允许隐式迁移。

#### Scenario: Maintenance encounters an old non Binance protection order
- **WHEN** 持久订单仍记录其他场所或无法确定来源
- **THEN** 不报价、不撮合、不爆仓、不取消该订单，保留原状态和预留，后台不持续尝试其他场所网络

#### Scenario: Binance instrument is reconciled in a mixed account
- **WHEN** 后台依据 BN 真实行情更新该账户的 BN 资源
- **THEN** 更新仅影响匹配的 BN 资源，被排除资产不会被顺带估值、清理或释放资金

### Requirement: Filtered event pagination preserves cursor and delivery identity
Paper 默认交易事件与通知 SHALL 过滤非 BN 及来源不明事件，同时保持原事件 ID、namespace、接收者和 delivery_key。扫描游标 SHALL 依据已检查的原事件位置推进；空的过滤结果 SHALL 不被表达为流已结束，has_more SHALL 反映是否仍有未检查记录。分页和过滤 SHALL 不重复通知、不漏掉后续 BN 事件，也不重置旧游标。

#### Scenario: A bounded scanned page contains only excluded events
- **WHEN** 当前批次已检查事件全部属于被过滤场所，但后面仍有原事件
- **THEN** 返回空 events 和可继续的已检查游标，并正确说明尚有后续记录，不反复扫描该页或跳过后来 BN 事件

#### Scenario: Binance event follows excluded history
- **WHEN** 游标续读最终遇到 BN 成交或爆仓事件
- **THEN** 使用原事件 ID 和 delivery_key 按既有通知及去重规则返回，不因过滤重新编号或自动发送消息

### Requirement: Original operation audit remains stable under filtering
父子意图、指纹、原操作/订单 ID、索引、真实状态及完整回执 SHALL 持久保留。后台状态投影过滤非 BN 时 SHALL 保留身份、事实和排除原因，不重排索引或把完成改为失败。后台核验、原请求重放及授权 HTTP/UI 审计 SHALL 仅核对被排除资源的原账本，不到旧场所联网、不重复金融影响或续跑尾项；不依赖操作查询工具。

#### Scenario: Old completed non Binance request is retried
- **WHEN** 同范围客户端以原 ID 和相同意图重试已保存的非 BN 操作
- **THEN** 读取原操作事实及过滤状态，不按当前 BN 默认重新成交，也不改变原哈希、现金或债务

#### Scenario: Historical mixed batch is queried after filtering
- **WHEN** 原批次包含 BN、非 BN 及 not_executed 项
- **THEN** 仍能对应原父子 ID、动作索引和原状态，被过滤内容明确排除，未执行尾项保持未执行，完整原审计受原权限保护

#### Scenario: A committed legacy child resolves a lost parent response
- **WHEN** 非 BN 原子子项账户变化及回执已持久提交，父响应丢失，而原 ID 回执足以证明该子项完成
- **THEN** 可只读核实并重建该已完成事实，无权威证据的其他项继续未解决；不变子回执/指纹、不联网、不新增资金变化或续跑尾项
