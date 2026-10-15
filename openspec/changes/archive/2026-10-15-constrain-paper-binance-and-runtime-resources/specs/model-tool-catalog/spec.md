# Spec Delta

## MODIFIED Requirements

### Requirement: Internal trading roles bind one execution entry
REAL、STRATEGY、SPOT_DCA 的可写交易 Agent 及绑定任务聊天 SHALL 仅绑定 execute_trade_actions 一个交易工具。单项 SHALL 使用一项动作数组；模型 SHALL 不绑定操作查询、单项交易、旧分发器或 adopt 工具。执行 SHALL 沿用角色授权和聊天审批，一次调用返回实际执行事实，后续核验由系统负责。

#### Scenario: Trading agent runs in any supported mode
- **WHEN** 为 REAL、STRATEGY 或 SPOT_DCA 的可写交易角色构建模型工具目录
- **THEN** 交易绑定恰好只有 execute_trade_actions，schema 符合当前任务能力，不出现 get_trade_operation、单项 open/close/edit/cancel 或同义适配器

#### Scenario: Bound task chat requests one cancellation
- **WHEN** 绑定任务聊天在既有审批流程下请求撤销一笔订单
- **THEN** 通过 execute_trade_actions 的一项 cancel 动作执行，沿用任务管理身份、授权及审批；调用返回后无需再发起操作查询工具

### Requirement: MCP publishes a bounded canonical core
MCP 默认核心目录 SHALL 使用且仅使用 get_trading_context、search_symbols、get_market、get_news、get_account_state、get_trade_records、execute_trade_actions 七个名称。执行 SHALL 公布 contract_version=3；get_operation SHALL 不注册为可调用工具，默认目录 SHALL 不重复公布单项动作、旧 execute_trade 或 adopt。

#### Scenario: Fully authorized live client discovers tools
- **WHEN** 启用的 live key 有 read、trade、cancel 权限且有可用授权 profile
- **THEN** 默认目录恰好为七个核心名称，不随现货、附带保护或独立退出增加同义工具，亦无 get_operation

#### Scenario: Client inspects the execution schema
- **WHEN** 新客户端读取 execute_trade_actions 描述和 schema
- **THEN** 获得 v3 有类型顺序动作契约，不要求先发现另一套旧交易 schema 或在执行后调用操作查询工具

### Requirement: MCP catalog is filtered by authenticated identity and scopes
MCP 默认目录 SHALL 按可信 key 类型、scope 和 profile 能力过滤。live SHALL 不公布 Paper 专属工具，Paper SHALL 不公布 live profile；无 trade/cancel SHALL 隐藏执行，有 cancel 无 trade 的 live key SHALL 仅可撤单。get_operation SHALL 在所有身份下移除，目录展示 SHALL 不替代调用时鉴权。

#### Scenario: Read-only live key discovers tools
- **WHEN** live key 只有 read 权限
- **THEN** 目录为六个核心只读名称，不包含 execute_trade_actions、get_operation 或写入兼容名称

#### Scenario: Cancellation-only live client submits an entry
- **WHEN** live key 有 read、cancel 而无 trade，并通过执行入口提交 open
- **THEN** 首次金融写入前因缺少 trade 失败，纯 cancel 仍按获授权 profile 能力处理

#### Scenario: Paper key discovers its default tools
- **WHEN** Paper key 请求 tools/list
- **THEN** 返回其核心和有权限附加工具，无 get_operation，不泄露 live profile、live 专属参数或旧五十项目录

### Requirement: Multi-profile capabilities do not duplicate public tools
同 key 多 profile 时 MCP SHALL 每个公共名称仅公布一次，在上下文逐 profile 返回市场、退出模式、标的、数量单位、管理范围及动作。客户端 SHALL 可区分能力；合并目录 SHALL 不扩大任一 profile 权限，也不增加操作查询工具。

#### Scenario: One key has spot and independent swap profiles
- **WHEN** 同 live key 授权现货及独立退出合约 profile
- **THEN** 仍为同一套七个核心名称，分别说明能力，现货请求不能借合约能力通过验证

#### Scenario: Profile or scope changes after discovery
- **WHEN** 发现后 profile 停用、标的缩小或权限变化
- **THEN** 后续调用按当前授权复查，旧目录缓存不授予已撤销能力，重新发现反映当前集合

### Requirement: Trading context replaces schema and account bootstrap discovery
get_trading_context SHALL 为 live 返回授权 profile，为 Paper 返回配置、namespace 身份和稳定 bot user_id；包含契约版本、市场、数量方式及系统自动更新执行状态的说明。上下文 SHALL 不给出操作查询工具或执行后二次核验指令，不暴露密钥、其他 namespace，亦不要求旧 get_trading_tools、get_paper_config 或 get_paper_bot_account。

#### Scenario: New paper conversation needs the bot identity
- **WHEN** Paper key 调用 get_trading_context
- **THEN** 读取费用、杠杆、市场规则及自身 bot user_id，无需另一身份工具，不获得其他账户私人持仓

#### Scenario: Live key has no authorized profile
- **WHEN** live key 的授权 profile 为空
- **THEN** 返回空集合和无可执行 profile 事实，不展示未授权 profile 或暗示可下单

### Requirement: Symbol and market queries preserve venue-specific capabilities
search_symbols 与 get_market SHALL 根据可信执行上下文使用相应标的发现和行情能力。Paper SHALL 仅使用 Binance 已核验且符合既有市场能力的公开行情，不跨场所搜索或 fallback；别名 SHALL 不代替上市核验。live SHALL 保留 profile 标的授权。发现失败或部分结果 SHALL 不被表达为标的必然不存在，返回的交易所与规范标的 SHALL 可用于后续同范围动作。

#### Scenario: Paper searches an unfamiliar stock token
- **WHEN** Paper 客户端搜索股票代币或股票永续简称
- **THEN** 仅返回 Binance 目录核验且受现有能力支持的 symbol、exchange 和资产类型；未核验名称不触发其他场所搜索，目录失败明确标识，报价不被描述为底层股票交易所现货价格

#### Scenario: Live client searches outside its selected symbols
- **WHEN** live profile 使用 selected 标的范围并执行搜索
- **THEN** 搜索结果限制在获授权规范标的内，不通过公共发现结果扩大该 profile 的下单范围

#### Scenario: Paper explicitly selects another exchange
- **WHEN** Paper 客户端为 search_symbols 或 get_market 指定非 BN 场所
- **THEN** 返回明确场所范围错误，不连接该交易所，不把搜索空结果或 BN 失败作为跨场所回退依据

### Requirement: Paper retains distinct financial and competition capabilities
Paper SHALL 在七个核心名称上仅增加 paper_bank、configure_paper_account、get_paper_leaderboard；read+trade 共十项，仅 read 共七项。账户配置 SHALL 区分开户、后续杠杆和显示/参赛/通知偏好，已有账户不重置；移除 get_operation SHALL 不影响银行、偏好或榜单权限。

#### Scenario: Writable paper key discovers all capabilities
- **WHEN** Paper key 有 read、trade 且既有 Paper 功能可用
- **THEN** 默认目录共十项，银行、配置、榜单保持可达，不出现操作查询及重复交易适配器

#### Scenario: Read-only paper key discovers tools
- **WHEN** Paper key 只有 read
- **THEN** 返回六项核心读取及 get_paper_leaderboard，共七项，无执行、银行、配置或 get_operation

#### Scenario: Account setup is repeated
- **WHEN** configure_paper_account 初始化已存在账户
- **THEN** 保留现金、债务、仓位和历史，返回原身份，不补资金或重置

### Requirement: Hidden compatibility tools preserve authorization and request identity
旧金融单项适配器、分发器及 v2/无版本调用 SHALL 保留兼容权限、模式、参数及原持久身份，退出默认目录；操作查询工具 SHALL 完全停用。目录或回执精简 SHALL 不改变旧指纹、不将原请求变成另一笔写入，兼容金融路径 SHALL 不绕过管理竞争或当前授权。

#### Scenario: Existing client directly calls a hidden adapter
- **WHEN** 已授权旧客户端调用隐藏的金融适配器名称
- **THEN** 按原输入契约及当前权限处理，无 trade/cancel 仍拒绝；执行一次返回精简事实，不要求再调用查询工具

#### Scenario: Old unknown operation is replayed after upgrade
- **WHEN** 客户端用原 ID 和相同 v2/无版本意图重试
- **THEN** 返回或由执行层核验原事实，不转 v3 重提交；更改意图复用原 ID 拒绝，不要求独立 get_operation

### Requirement: Default receipts are compact and evidence remains queryable
新旧金融入口的默认模型结果 SHALL 仅含真实操作/逐项状态、必要订单身份、数量、成交及失败事实；账户摘要最多一次。完整回执 SHALL 持久化，用户可经授权 HTTP/UI 详情按需读取。结果 SHALL 不嵌入旧评估树、重复 blocker 或操作查询指令，不能省略管理竞争、目标变化及未知事实，也不把 submitted 当成交。

#### Scenario: Paper batch has several successful steps
- **WHEN** Paper 多项交易完成并请求账户摘要
- **THEN** 返回逐项精简事实及一次末尾摘要，不重复账户和历史，无 get_operation 后续要求

#### Scenario: Confirmed close has unresolved cleanup
- **WHEN** 平仓已确认而残单待核验
- **THEN** 保留确认成交和必要残单身份/未决状态，后台按原身份继续核验，用户可查看完整详情，不要求模型再调操作工具

#### Scenario: Legacy conditional close returns accepted but unfilled
- **WHEN** 旧 v2 close 创建 SHORT take_profit_market 退出，数量 0.03，权威 ACK 已收到而成交量为 0
- **THEN** 一次调用返回 submitted、待触发订单状态、filled_amount=0、已知订单/客户端身份和数量，不能报告已平仓，也不附二次查询任务

## ADDED Requirements

### Requirement: Retired operation tools cannot be invoked through cached names
get_trade_operation 和 get_operation SHALL 从 Agent/聊天绑定、MCP 注册及兼容可调用集合移除。缓存名称调用 SHALL 明确返回已移除/不可用且不执行原查询逻辑；回执、提示、示例和默认上下文 SHALL 不引导调用它们，也不新增同义工具、query 动作或强制记录查询来替代。

#### Scenario: Old request includes an account scope as config id
- **WHEN** 旧客户端仍调用 get_trade_operation，携带原 ID、symbol 及从 account_scope 复制的 config_id
- **THEN** 该工具明确不可用，不执行原查询、不选择该账户、不要求改参数再调用；原操作由可信执行上下文及后台核验关联，原账本不变

#### Scenario: Default trade flow is inspected after removal
- **WHEN** 检查各模式执行后的工具调用和默认提示
- **THEN** 只有本次交易执行调用，无操作查询名称、二次核验任务或伪装的 operation 查询工具；正常自主读取账户/订单/成交能力仍可用

### Requirement: Default receipt size is bounded without corrupting facts
默认单项金融回执 SHALL 不超过 4096 字节，最多二十项的整批 SHALL 不超过 32768 字节，按序列化 UTF-8 计量。结果 SHALL 使用结构化去重及有限诊断，保留每项索引、原身份、真实状态和必要数量；省略详情明确标记并可授权查看。不得截断 JSON、订单 ID、伪造成功或丢弃未执行项来达标。

#### Scenario: Large old assessment accompanies one close
- **WHEN** close 原证据含重复 scope、数量别名及十一条复用同一 blocker 的 previous_assessment 规则
- **THEN** 默认回执在 4096 字节内，无旧评估树，blocker 只表达一次并注明省略详情；必要身份/数量/真实状态保留，原完整证据不变

#### Scenario: Long batches retain every action state
- **WHEN** 二十项结果含成功、submitted、unknown、失败和未执行尾项，历史诊断继续增长
- **THEN** 整批在 32768 字节内，全部索引、身份和状态保留，最多一次请求的账户摘要，历史增长不使默认返回无界增长
