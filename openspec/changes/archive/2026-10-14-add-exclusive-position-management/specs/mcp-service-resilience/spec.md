# Spec Delta

## Purpose

使 MCP 故障能按网络、TLS、入口代理、应用认证及交易所依赖分层定位并核验恢复，确保外部查询积压或依赖超时不会持续拖住网页和其他工具，并使返回的数据完整性、执行确定性及新鲜度可辨识。

## ADDED Requirements

### Requirement: Outage repair is based on layer-specific evidence
当前 MCP 宕机修复 SHALL 记录同一诊断时段的服务器本机健康、公网 DNS/TLS/HTTP、OAuth 发现、认证 MCP、反向代理和容器状态，按证据定位并修复故障。TLS 未完成 SHALL NOT 被诊断成交易所接口超时；本地测试通过 SHALL NOT 等同公网恢复。生产变更 SHALL 保留实际调整、验证结果及回退依据，不展示密钥或令牌。

#### Scenario: Public TLS fails before HTTP
- **WHEN** 服务器本机健康成功，但公网 TLS 握手超时
- **THEN** 排查和修复入口网络、证书/TLS及代理层，并保留证据，不通过扩大交易所超时声称修复

#### Scenario: Application fails locally
- **WHEN** 公网代理可用但容器本机健康或认证处理失败
- **THEN** 结合容器、应用及数据库状态定位对应故障，保留各层诊断结果

### Requirement: Recovery is verified through the actual public MCP path
故障恢复验收 SHALL 包含公网网页、健康检查、正确 issuer/resource 的 OAuth 发现及一次已授权只读 MCP 调用。账户、订单和行情关键读取 SHALL 分别验证成功或返回有界、可解释的依赖失败。仅端口可连、首页可开或 OAuth 元数据可读 SHALL NOT 被报告为 MCP 已恢复。无生产访问或故障仍未解决时 SHALL 明确报告待完成阶段。

#### Scenario: Full public recovery
- **WHEN** 入口及应用故障修复后完成公网和已授权只读工具检查
- **THEN** 报告各项恢复结果与时间，不执行新交易或通过重放未知交易验证连接

#### Scenario: Missing production access
- **WHEN** 本地加固验证完成但无法取得生产日志或执行公网认证验证
- **THEN** 保留本地验证结果并将生产故障修复/验收标为待完成，不宣称整体修复成功

### Requirement: Slow MCP work does not exhaust Web availability
MCP 的交换数据读取和金融执行 SHALL 使用有界并发、有限排队和总调用预算，不持续耗尽网页服务额度。认证及配置存储的慢访问 SHALL NOT 阻塞其他异步请求。限流、排队超时或依赖超时 SHALL 返回可解释状态，资源继续可恢复，不自动重试金融写入。

#### Scenario: Exchange queries occupy MCP capacity
- **WHEN** 多个余额、订单及行情请求持续等待交易所
- **THEN** 超额请求有限等待或快速返回繁忙，网页静态资源与不依赖该交易所的健康/轻量接口仍可响应

#### Scenario: Database contention during authentication
- **WHEN** 认证读取遭遇数据库锁等待
- **THEN** 等待受限且不阻塞应用事件循环，其他可独立处理的请求继续响应

### Requirement: Query budgets preserve completeness and observation times
读取 SHALL 在包括排队、市场目录加载和依赖请求的总预算内完成或返回失败/部分成功。可复用市场元数据与公开行情缓存，但 SHALL 标识来源和真实观察时间；交易执行 SHALL 重新核验动作依赖的事实。部分订单列表、旧仓位缓存和读取失败 SHALL NOT 被表示为当前无订单、空仓或已获准入。

#### Scenario: Ticker succeeds and candles time out
- **WHEN** 行情读取取得 ticker，但 K 线在总预算内失败
- **THEN** 可返回明确的部分成功、ticker 观察时间和 candles 缺项，不能把缺项伪装成正常空数组

#### Scenario: One order channel fails
- **WHEN** 普通订单读取成功而条件订单通道超时
- **THEN** 查询标识不完整及失败通道，不能据此认定没有保护单或允许依赖完整列表的写入

### Requirement: Financial timeouts retain a single recoverable operation
金融写入 SHALL 区分未开始、已明确拒绝、已接受和结果未知。客户端断开或总预算到期后可能仍在执行的请求 SHALL 保留原操作及订单身份，不能释放保护并允许另一请求重复提交。只读查询和后台核验 SHALL 能恢复原结果；增加读取超时治理 SHALL NOT 降低既有幂等和执行确定性。

#### Scenario: Client disconnects after exchange submission
- **WHEN** 交易请求可能已发送但调用者断开或响应超时
- **THEN** 保留原 operation_id/client_id 和阶段，后续查询原操作，不换 ID 重发、不以请求结束证明未执行

### Requirement: Diagnostics locate latency without exposing credentials
系统 SHALL 为 MCP 请求记录可关联的请求身份、工具、脱敏范围、排队/认证/元数据/交易所/持久化耗时、结果及超时阶段，并提供当前并发、积压和依赖失败的诊断信息。日志 SHALL NOT 包含交易所密钥、Bearer token、OAuth code 或管理员密码。无关失败 SHALL 与整个服务不可用区分。

#### Scenario: Balance request times out
- **WHEN** 余额工具失败
- **THEN** 运维可辨别失败发生在入口、认证、排队、初始化或余额请求阶段，不能只得到无法定位的总超时文本
