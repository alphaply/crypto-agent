# Spec Delta

## MODIFIED Requirements

### Requirement: Slow MCP work does not exhaust Web availability
MCP 的交换数据读取和金融执行 SHALL 使用有界并发、有限排队和总调用预算，并纳入进程总资源准入，不持续耗尽网页服务额度。额度 SHALL 在底层 worker 真正结束后归还；认证及配置存储的慢访问 SHALL NOT 阻塞其他异步请求。限流、排队超时、容量不足或依赖超时 SHALL 返回可解释状态，保留恢复与轻量响应余量，不自动重试金融写入。

#### Scenario: Exchange queries occupy MCP capacity
- **WHEN** 多个余额、订单及行情请求持续等待交易所
- **THEN** 超额请求有限等待或快速返回繁忙，运行及队列额度不突破声明容量，网页静态资源与不依赖该交易所的健康/轻量接口仍在声明 SLO 内响应，调用者超时不虚假释放仍在执行的额度

#### Scenario: Database contention during authentication
- **WHEN** 认证读取遭遇数据库锁等待
- **THEN** 等待受限且不阻塞应用事件循环，其他可独立处理的请求继续响应

#### Scenario: Capacity rejects a financial call before execution
- **WHEN** 本次调用尚未进入金融执行阶段就因进程容量不足被拒绝
- **THEN** 明确返回本次 busy/未开始事实，不产生金融写入、不编造未知提交，不通过隐藏兼容入口或独立池绕过准入；这不证明同 ID 历史操作从未执行

#### Scenario: An existing unknown operation is retried while capacity is full
- **WHEN** 同 ID 原操作已经提交或 unknown，但本次重试因容量不足未开始
- **THEN** 保留原父子身份、指纹和历史状态，仅说明本次 busy；未能读取权威账本时标明原状态不可验证，不把旧操作降级为 not_started/not_executed，也不推出历史零成交

### Requirement: Query budgets preserve completeness and observation times
读取 SHALL 在包括排队、市场目录加载、容量等待和依赖请求的总预算内完成或返回失败/部分成功。可复用受容量及峰值预算约束的市场元数据与公开行情缓存，但 SHALL 标识来源和真实观察时间；交易执行 SHALL 重新核验动作依赖的事实。部分订单列表、过滤历史、旧仓位缓存和读取失败 SHALL NOT 被表示为当前无订单、空仓、完整权益或已获准入。

#### Scenario: Ticker succeeds and candles time out
- **WHEN** 行情读取取得 ticker，但 K 线在总预算内失败
- **THEN** 可返回明确的部分成功、ticker 观察时间和 candles 缺项，不能把缺项伪装成正常空数组

#### Scenario: One order channel fails
- **WHEN** 普通订单读取成功而条件订单通道超时
- **THEN** 查询标识不完整及失败通道，不能据此认定没有保护单或允许依赖完整列表的写入

#### Scenario: Metadata refresh cannot fit its memory budget
- **WHEN** 公开目录刷新或所需客户端初始化无法取得峰值容量
- **THEN** 在总预算内返回资源不足或明确陈旧的允许查询结果，不无界加载、不伪造新的观察时间，也不据此放行要求当前事实的交易

### Requirement: Financial timeouts retain a single recoverable operation
金融写入 SHALL 区分未开始、拒绝、已接受和未知；权威 ACK 足以返回 submitted，不为额外订单/仓位读取延迟响应。断开、到期、worker 死亡或重启 SHALL 保留原父子身份、订单、指纹及管理权，不能释放未决保护以重复提交。后台 SHALL 按原身份有界核验并更新状态，不要求模型操作查询；容量恢复不重发、不续跑尾项，可靠成交不被核验失败覆盖。

#### Scenario: Client disconnects after exchange submission
- **WHEN** 交易请求可能已发送但调用者断开或响应超时
- **THEN** 保留原 operation_id/client_id 和阶段，后台核验原事实，不换 ID 重发、不以请求结束证明未执行，也不引导 get_operation/get_trade_operation

#### Scenario: A resource failure terminates a submitted batch
- **WHEN** 原批次部分项可能已提交，worker 被杀死且随后重启
- **THEN** 查询保留原父子映射、已确认结果、unknown 与 not_executed 事实，仅核验原已尝试项，不自动续跑或因资源清理交接管理权

#### Scenario: Accepted order would be followed by a slow position read
- **WHEN** open/close 的权威受理 ACK 已有，而后续仓位或订单读取会超时
- **THEN** 本次响应及时保留 submitted、订单身份和已知数量，不等待该读取才宣布受理，不因读取失败改为提交失败，也不安排模型第二次查询；后台更新真实成交事实

### Requirement: Diagnostics locate latency without exposing credentials
系统 SHALL 为 MCP 请求记录可关联的请求身份、工具、脱敏范围、排队/认证/元数据/交易所/持久化耗时、结果及超时阶段，并提供实际并发、积压、容量拒绝及依赖失败。诊断 SHALL 关联有界持久资源序列、启动/容器身份和实际限制，区分 OOM 证据、压力与缺项。日志 SHALL NOT 包含交易所密钥、Bearer token、OAuth code 或管理员密码；无关失败 SHALL 与整个服务不可用区分。

#### Scenario: Balance request times out
- **WHEN** 余额工具失败
- **THEN** 运维可辨别失败发生在入口、认证、排队、容量、初始化或余额请求阶段，关联故障时段资源证据和缺项，不能只得到无法定位的总超时文本

#### Scenario: Restarted container reports no current OOM kill
- **WHEN** 重启后当前 OOMKilled=false，但此前请求没有完成记录
- **THEN** 诊断保留旧启动/容器映射及已保存的 kernel/cgroup/资源证据，缺少事发证据时报告归因不足，不以当前状态否定过去 OOM 或断言重复故障原因
