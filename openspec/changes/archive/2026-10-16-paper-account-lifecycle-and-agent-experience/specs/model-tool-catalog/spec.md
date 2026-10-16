# Spec Delta

## MODIFIED Requirements

### Requirement: Paper retains distinct financial and competition capabilities
Paper SHALL 在七个核心名称上增加 paper_bank、configure_paper_account、get_paper_leaderboard、paper_transfer；read+trade 共十一项，仅 read 共七项。账户配置 SHALL 区分开户、后续杠杆和显示/参赛/通知偏好，以及用户主动下海的显式 reset；重复开户不重置。银行 SHALL 反映统一配置的日利率；榜单 SHALL 支持 return_pct 与 reset_count。移除 get_operation SHALL 不影响银行、偏好或榜单权限，live SHALL 不公布 Paper 金融工具。

#### Scenario: Writable paper key discovers all capabilities
- **WHEN** Paper key 有 read、trade 且既有 Paper 功能可用
- **THEN** 默认目录共十一项，银行、配置、榜单和转账可达，不出现操作查询及重复交易适配器

#### Scenario: Read-only paper key discovers tools
- **WHEN** Paper key 只有 read
- **THEN** 返回六项核心读取及 get_paper_leaderboard，共七项，无执行、银行、配置、转账或 get_operation

#### Scenario: Account setup is repeated
- **WHEN** configure_paper_account 初始化已存在账户
- **THEN** 保留现金、债务、仓位和历史，返回原身份，不补资金或重置

#### Scenario: User explicitly resets a bankrupt account
- **WHEN** 可信本人主动下海意图通过 configure_paper_account 的显式 reset 提交
- **THEN** 按破产重置契约执行，不复用 create 或 update 隐式重置，不向只读 key 或 live key 开放

#### Scenario: Transfer schema and context agree
- **WHEN** 已授权 Paper 客户端发现转账工具及上下文
- **THEN** schema 明确付款 user_id、收款 recipient_user_id、USDT 正金额和稳定 operation_id；上下文说明允许向破产账户收款、禁止从破产账户转出

#### Scenario: Client requests a competition board
- **WHEN** 客户端通过榜单工具请求 return_pct 或 reset_count
- **THEN** 分别返回本轮收益率榜或累计下海次数榜；旧 pnl 排名请求明确说明改用支持的指标，不静默映射成另一种排名

### Requirement: Hidden compatibility tools preserve authorization and request identity
旧金融单项适配器、分发器及 v2/无版本调用 SHALL 保留兼容权限、模式、参数及原持久身份，退出默认目录；操作查询工具 SHALL 完全停用。目录或回执精简 SHALL 不改变旧指纹、不将原请求变成另一笔写入，兼容金融路径 SHALL 遵守共享资源并发协调及当前授权，不因已有其他 manager 而拒绝。

#### Scenario: Existing client directly calls a hidden adapter
- **WHEN** 已授权旧客户端调用隐藏的金融适配器名称
- **THEN** 按原输入契约及当前权限处理，无 trade/cancel 仍拒绝；执行一次返回精简事实，不要求再调用查询工具

#### Scenario: Old unknown operation is replayed after upgrade
- **WHEN** 客户端用原 ID 和相同 v2/无版本意图重试
- **THEN** 返回或由执行层核验原事实，不转 v3 重提交；更改意图复用原 ID 拒绝，不要求独立 get_operation

### Requirement: Default receipts are compact and evidence remains queryable
新旧金融入口的默认模型结果 SHALL 仅含真实操作/逐项状态、必要订单身份、数量、成交及失败事实；账户摘要最多一次。完整回执 SHALL 持久化，用户可经授权 HTTP/UI 详情按需读取。结果 SHALL 不嵌入旧评估树、重复 blocker 或操作查询指令，不能省略真实权限不足、并发冲突、目标变化及未知事实，也不得将另一连接的历史 owner 报为准入阻断，也不把 submitted 当成交。

#### Scenario: Paper batch has several successful steps
- **WHEN** Paper 多项交易完成并请求账户摘要
- **THEN** 返回逐项精简事实及一次末尾摘要，不重复账户和历史，无 get_operation 后续要求

#### Scenario: Confirmed close has unresolved cleanup
- **WHEN** 平仓已确认而残单待核验
- **THEN** 保留确认成交和必要残单身份/未决状态，后台按原身份继续核验，用户可查看完整详情，不要求模型再调操作工具

#### Scenario: Legacy conditional close returns accepted but unfilled
- **WHEN** 旧 v2 close 创建 SHORT take_profit_market 退出，数量 0.03，权威 ACK 已收到而成交量为 0
- **THEN** 一次调用返回 submitted、待触发订单状态、filled_amount=0、已知订单/客户端身份和数量，不能报告已平仓，也不附二次查询任务
