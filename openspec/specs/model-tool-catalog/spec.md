# model-tool-catalog Specification

## Purpose

为内置 Agent、任务绑定聊天及 MCP 客户端提供按身份、交易模式和权限裁剪的精简工具目录，减少重复 schema 与返回内容，同时保留既有查询、交易、恢复和兼容调用能力。目录描述、可调用权限及真实执行能力必须一致，避免模型从展示或旧名称推断出额外授权。

## Requirements

### Requirement: Internal trading roles bind one execution entry
REAL、STRATEGY、SPOT_DCA 的可写交易 Agent 及绑定任务聊天 SHALL 仅绑定 execute_trade_actions 与 get_trade_operation 两个交易相关工具。单项交易 SHALL 通过一项动作数组表达，默认模型目录 SHALL 不绑定单项交易适配器、旧分发器或 adopt 工具；执行仍须遵循角色授权和既有聊天审批。

#### Scenario: Trading agent runs in any supported mode
- **WHEN** 为 REAL、STRATEGY 或 SPOT_DCA 的可写交易角色构建模型工具目录
- **THEN** 交易相关绑定恰好包含 execute_trade_actions 和 get_trade_operation，执行 schema 与当前任务能力一致，不出现 open、close、edit、cancel 或模式同义适配器

#### Scenario: Bound task chat requests one cancellation
- **WHEN** 绑定任务聊天在既有审批流程下请求撤销一笔订单
- **THEN** 通过 execute_trade_actions 的一项 cancel 动作执行，并沿用该任务管理身份、授权范围和原审批约束

### Requirement: Read-only internal roles do not acquire trading bindings
只读聊天、记忆角色及策略摘要角色 SHALL 绑定零个上述交易相关工具，且 SHALL 不绑定金融写入兼容入口。已有只读上下文和分析能力 SHALL 保留，查询可见性 SHALL 不产生写入或自动认领权限。

#### Scenario: Read-only chat sees a manual position
- **WHEN** 只读聊天上下文包含人工仓位和挂单
- **THEN** 模型仍无 execute_trade_actions、get_trade_operation 或写入适配器，展示仓位不导致认领管理权

#### Scenario: Memory or summary role is initialized
- **WHEN** 初始化记忆或策略摘要角色
- **THEN** 其交易相关工具绑定数量为零，不继承交易 Agent 的写入目录

### Requirement: MCP publishes a bounded canonical core
MCP 默认核心目录 SHALL 使用且仅使用 get_trading_context、search_symbols、get_market、get_news、get_account_state、get_trade_records、execute_trade_actions、get_operation 八个名称。执行入口 SHALL 公布新的 contract_version=3 契约；默认目录 SHALL 不重复公布单项动作、模式同义入口、旧 execute_trade 或 adopt。

#### Scenario: Fully authorized live client discovers tools
- **WHEN** 启用的 live key 具有 read、trade、cancel 权限且有可用授权 profile
- **THEN** 默认工具目录恰好包含八个核心名称，不随该 profile 是现货、附带保护合约或独立退出合约增加同义工具

#### Scenario: Client inspects the execution schema
- **WHEN** 新客户端读取默认 execute_trade_actions 描述和输入 schema
- **THEN** 获得 contract_version=3 的有类型顺序动作契约，不要求先发现或调用另一套旧交易 schema

### Requirement: MCP catalog is filtered by authenticated identity and scopes
MCP 默认目录 SHALL 按可信 key 类型、scope 及可用 profile 能力过滤。live SHALL 不公布 Paper 专属工具；Paper SHALL 不公布 live profile 入口。无 trade 或 cancel 权限 SHALL 隐藏执行入口；有 cancel 而无 trade 的 live key SHALL 仅具有撤单执行能力，目录展示 SHALL 不替代调用时鉴权。

#### Scenario: Read-only live key discovers tools
- **WHEN** live key 只有 read 权限
- **THEN** 目录包含七个核心只读名称，不包含 execute_trade_actions 或任何写入兼容名称

#### Scenario: Cancellation-only live client submits an entry
- **WHEN** live key 有 read、cancel 而无 trade，并使用已公布执行入口提交 open 动作
- **THEN** 请求在首次金融写入前因缺少 trade 权限失败，纯 cancel 动作仍按已授权 profile 能力处理

#### Scenario: Paper key discovers its default tools
- **WHEN** Paper key 请求 tools/list
- **THEN** 返回其 Paper 核心及有权限的附加工具，不泄露 live profile 列表、live 专属参数或原统一五十项目录

### Requirement: Multi-profile capabilities do not duplicate public tools
同一 key 授权多个 profile 时，MCP SHALL 每个公共工具名称仅公布一次，并在交易上下文中逐 profile 返回市场、退出模式、授权标的、数量单位、管理范围及允许动作。客户端 SHALL 能区分各 profile 能力；跨 profile 合并目录 SHALL 不扩大任一 profile 的执行能力。

#### Scenario: One key has spot and independent swap profiles
- **WHEN** 同一 live key 同时授权现货和独立退出合约 profile
- **THEN** 仍只有一套八个核心名称，上下文分别说明两个 profile 的动作和参数能力，现货请求不能借合约能力通过验证

#### Scenario: Profile or scope changes after discovery
- **WHEN** 已发现目录后 profile 被停用、授权标的缩小或 token 权限发生变化
- **THEN** 后续查询和写入按当前授权重新检查，旧目录缓存不继续授予已撤销能力，重新发现反映当前可用能力

### Requirement: Trading context replaces schema and account bootstrap discovery
get_trading_context SHALL 为 live 返回获授权 profile 及能力，为 Paper 返回配置、namespace 内可用身份和稳定 bot user_id。返回 SHALL 包含默认契约版本、恢复入口及支持的市场和数量方式，并 SHALL 不暴露密钥、其他 namespace 信息或要求模型先调用旧 get_trading_tools、get_paper_config、get_paper_bot_account。

#### Scenario: New paper conversation needs the bot identity
- **WHEN** Paper key 调用 get_trading_context
- **THEN** 可读取费用、杠杆和市场规则及自己的 bot user_id，无需另一项 bot 身份工具，也不获得其他账户的私人持仓

#### Scenario: Live key has no authorized profile
- **WHEN** live key 的授权 profile 集合为空
- **THEN** 上下文返回空集合和无可执行 profile 的能力事实，不展示未授权 profile 或暗示可下单

### Requirement: Account and trade records support compact facet queries
get_account_state SHALL 按显式 facet 查询余额、仓位、可管理库存或挂单摘要；get_trade_records SHALL 区分订单列表、指定订单、成交和历史事件，支持范围及分页限制。默认返回 SHALL 仅含所请求的精简字段，并保留数量单位、来源、估值时间、管理者和未知状态；完整详情 SHALL 可按需读取。

#### Scenario: Client asks for positions only
- **WHEN** 客户端仅选择 positions facet
- **THEN** 返回当前授权范围内的仓位及来源、管理状态和观察时间，不附完整余额、全部挂单或历史事件

#### Scenario: Client checks an order after submission
- **WHEN** 客户端以单订单查询类型和原回执 order_id 调用 get_trade_records
- **THEN** 返回该订单的当前身份、状态和实际成交，明确区分接受委托与成交，不把订单 ID 当作操作 ID

#### Scenario: Paper client reads account history
- **WHEN** Paper 客户端选择 history 类型
- **THEN** 在该 namespace/user 账户范围内返回借还款、成交、爆仓和账户调整事件，不混同公开排行榜或其他参与者的私人历史

### Requirement: Symbol and market queries preserve venue-specific capabilities
search_symbols 与 get_market SHALL 根据可信执行上下文使用相应标的发现和行情能力。Paper SHALL 保留多交易所、股票代币别名与真实公开行情来源；live SHALL 保留 profile 标的授权。发现失败或部分结果 SHALL 不被表达为标的必然不存在，返回的交易所与规范标的 SHALL 可用于后续同范围动作。

#### Scenario: Paper searches an unfamiliar stock token
- **WHEN** Paper 客户端搜索只在某个支持场所上市的股票代币或股票永续简称
- **THEN** 返回经场所目录核验的 symbol、exchange 和资产类型，后续报价不被描述为底层股票交易所现货价格

#### Scenario: Live client searches outside its selected symbols
- **WHEN** live profile 使用 selected 标的范围并执行搜索
- **THEN** 搜索结果限制在获授权规范标的内，不通过公共发现结果扩大该 profile 的下单范围

### Requirement: Paper retains distinct financial and competition capabilities
Paper 默认目录 SHALL 在核心八项基础上仅增加 paper_bank、configure_paper_account、get_paper_leaderboard。有 read、trade 的 Paper key SHALL 公布十一项；只有 read 时 SHALL 公布八项只读工具。账户配置 SHALL 区分显式开户、后续默认杠杆和显示/参赛/通知偏好，已有账户 SHALL 不因开户或配置被重置。

#### Scenario: Writable paper key discovers all capabilities
- **WHEN** 启用的 Paper key 有 read、trade 且所有现有 Paper 功能可用
- **THEN** 默认目录共十一项，借还款、账户配置和公开排行榜能力保持可达，交易适配器名称不再重复出现

#### Scenario: Read-only paper key discovers tools
- **WHEN** Paper key 只有 read
- **THEN** 目录包含七项核心只读工具及 get_paper_leaderboard，不包含 execute_trade_actions、paper_bank 或 configure_paper_account

#### Scenario: Account setup is repeated
- **WHEN** configure_paper_account 显式请求初始化一个已存在账户
- **THEN** 返回已存在的账户身份且保留现金、债务、仓位和历史，不补充初始资金或重置账户

### Requirement: Event polling and bot provisioning stay outside conversation tools
scan_paper_events 及 bot 初始化恢复入口 SHALL 不进入默认对话模型目录。机器人调度的事件 HTTP 接口 SHALL 保留 cursor、delivery_key、namespace 和通知偏好语义；首次提供 Paper 身份的维护流程 SHALL 预建或恢复 bot 账户，事件读取 SHALL 不自行发送消息。

#### Scenario: Bot polls automatic fill events
- **WHEN** 机器人通过受 Paper key 鉴权的事件 HTTP 接口读取事件
- **THEN** 可继续按 cursor 和 delivery_key 去重交付，模型目录无需公开扫描工具，也不改变已配置的通知偏好

#### Scenario: Bot account is missing during initialization
- **WHEN** 已授权的 Paper 初始化或维护流程发现 bot 账户缺失
- **THEN** 创建该 namespace 的稳定 bot 账户，已有 bot 账户保持资金和历史，默认模型无需调用 ensure_paper_bot_account

### Requirement: Hidden compatibility tools preserve authorization and request identity
旧单项工具、分发器、Paper 适配器及历史 v2/无版本调用 SHALL 退出默认目录，但 SHALL 在兼容调用路径保留原权限、模式限制、参数含义及持久化请求身份。工具重命名或新默认目录 SHALL 不改变旧回执指纹或重放为另一笔写入；兼容路径 SHALL 不绕过自动管理竞争和当前授权。

#### Scenario: Existing client directly calls a hidden adapter
- **WHEN** 已授权旧客户端调用未列入默认目录的原工具名称
- **THEN** 按原输入契约和当前权限处理，缺少 cancel 或 trade 权限仍拒绝，工具隐藏不等于放宽调用限制

#### Scenario: Old unknown operation is replayed after upgrade
- **WHEN** 客户端重放既有 v2 或无版本操作的原 ID 与原请求
- **THEN** 查询或恢复原操作，不将其转换为 v3 后重新提交；更换意图复用原 ID 被拒绝

### Requirement: Default receipts are compact and evidence remains queryable
默认模型结果 SHALL 只包含操作及逐项状态、已知订单身份、数量方式、已核实成交和必要失败/恢复信息，批次末尾最多附一次按需账户摘要。完整回执和核验证据 SHALL 继续持久化并经操作或记录查询可达，精简 SHALL 不删除会影响下一动作的管理竞争、目标变化或未知执行事实。

#### Scenario: Paper batch has several successful steps
- **WHEN** Paper 批次执行多项交易并请求最终账户摘要
- **THEN** 每项返回精简真实回执，末尾返回一次账户摘要，不在每项重复附全部账户和历史

#### Scenario: Confirmed close has unresolved cleanup
- **WHEN** 本次平仓已确认而相关残单仍待核验
- **THEN** 精简回执保留已确认成交，单独指出残单身份、待核验状态和恢复入口，完整证据仍可查询

### Requirement: Tool catalog contracts are consistently observable
实际模型绑定、MCP tools/list、交易上下文、输入 schema 及操作入口 SHALL 对同一身份和能力保持一致。新目录 SHALL 拒绝未知字段并明确必填、互斥、单位及模式限制；目录验收 SHALL 以各角色、key 类型、scope 与多 profile 组合的名称集合及数量为准。

#### Scenario: Published input contains unsupported financial fields
- **WHEN** 调用请求在当前 profile 的动作中携带不支持的金额、保护或执行引擎字段
- **THEN** 明确拒绝不支持字段，不静默丢弃，也不从其他模式的 schema 推断可执行能力

#### Scenario: Catalog and task binding are verified together
- **WHEN** 检查所有交易角色、只读角色和 live/Paper key 权限组合
- **THEN** 名称集合、数量和公布能力与上述契约一致，不存在可见但恒不可用的同义入口或未授权领域信息
