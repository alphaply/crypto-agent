# runtime-resource-capacity Specification

## Purpose

使 Web 与 scheduler 的多类并发工作在经测量确定的资源容量内运行，控制缓存、客户端及刷新峰值，保护主机和关键交易恢复的可用性，并保留能够区分资源压力、容器 OOM 与主机 global OOM 的持续证据及可复现隔离验收结果。

## Requirements

### Requirement: Capacity profiles are declared before verification
系统 SHALL 在负载验收前声明容量 profile，包括目标环境、镜像及配置版本、空闲和典型负载、并发/刷新峰值、运行时长、资源预算及上下界、主机保留余量和服务 SLO。容量报告 SHALL 保存实测峰值、稳定区间、计量口径和结论。容器 memory/swap/CPU、应用并发及缓存额度 SHALL 由该报告确定，不能以单次 RSS 或未测量假设拍定。

#### Scenario: A profile establishes measurable acceptance
- **WHEN** 准备对 Web 与 scheduler 执行容量验收
- **THEN** profile 在执行前固定负载、时长、预算、上下界和 SLO，报告将实测结果关联到同一镜像和配置，不能在失败后改变标准冒充通过

#### Scenario: Capacity evidence is incomplete
- **WHEN** 只有空闲 RSS、单容器采样或缺少刷新峰值和主机余量的记录
- **THEN** 报告标识覆盖不足，不将其作为已验证的运行容量或线上 OOM 修复证明

### Requirement: Aggregate admission covers all nested runtime work
Web 与 scheduler SHALL 各自提供进程总容量准入，将 Agent、MCP、保护维护、成交同步、记忆、新闻、流连接、历史分页和初始化/刷新等工作纳入统一预算，同时保留工作类别隔离。嵌套并发 SHALL 被父预算或明确的子额度核算，不能借独立线程池绕过总限制。CPU 可用性 SHALL 结合实际运行限制判断，不能仅用宿主 CPU 数决定工作额度。

#### Scenario: Individually bounded pools saturate together
- **WHEN** 多个工作池各自未满但其聚合资源承诺已达到进程额度
- **THEN** 新工作有界等待或返回资源不足，运行和预留额度不突破声明总预算，不继续创建未计入的并发工作

#### Scenario: One job creates parallel children
- **WHEN** 单个新闻、Paper 扫描或市场初始化任务派生多个并行子任务
- **THEN** 子任务使用已核算额度并受并发约束，不能因父任务只占一个 worker 而允许无界子工作，也不形成互等额度的永久阻塞

### Requirement: Queues and retained work sets remain bounded
系统 SHALL 限制实际运行数并保持公平进度。已受理的策略、新闻和定投触发 SHALL 持久保存，同一触发身份去重，不合并不同触发，不按等待时长或忙碌丢弃；其他类别继续使用其原有排队及等待限制。队列、待处理快照、历史扫描及保护轮询的内存工作集 SHALL 随声明容量受限，不能一次装载全部历史或以配置数量增长绕过界限。不可接受的容量配置 SHALL 明确拒绝，不能默默恢复成无界默认。

#### Scenario: More configurations arrive than workers can serve
- **WHEN** 配置数或触发数超过当前可服务容量
- **THEN** 已受理策略、新闻和定投触发保留在持久队列并显示等待，按最早可运行顺序领取；不在内存积累无界 futures 或全量任务副本，其他类别继续遵守其原有队列与等待限制

#### Scenario: Long history is processed under pressure
- **WHEN** 原计划、成交或 Paper 历史超过单次内存工作集
- **THEN** 按有界分页/批次推进并保存进度，不把读取失败或未扫描尾部表示为无订单、无持仓或对账完整

#### Scenario: A single financial record exceeds the work budget
- **WHEN** 单个账户或回执超过声明的读取/解析预算，完整资金承诺无法在预算内核验
- **THEN** 保留原记录并返回资源不足、未处理和可核对身份，不通过缩小页数后无界解析，不把部分预留作为完整准入依据或执行该账户新的金融变化

### Requirement: Critical recovery retains reserved capacity
系统 SHALL 为原操作结果核验及必要的已授权保护/周期维护保留受限容量。压力时 SHALL 优先限制新决策、重扫描和非必要刷新，不能通过关闭全部维护释放已有管理责任。保留额度 SHALL 计入进程总预算。关键工作仍 SHALL 遵守自身预算、退避和真实权限，不授权新策略写入或无界重试。

#### Scenario: New analysis exhausts ordinary capacity
- **WHEN** 普通容量被 Agent 或公开数据工作占满且存在已提交未知操作
- **THEN** 原身份核验可使用保留额度有界推进，新分析可延后；容量不足不被表示为原交易已失败或已完成

#### Scenario: No model operation tool remains after an accepted trade
- **WHEN** 已受理或未知交易需要后续证据，而模型操作查询工具已移除
- **THEN** 系统使用保留额度和有限退避自动核验原已尝试身份，更新原回执、默认状态及授权界面，不把后台拥塞改成需要模型第二个调用的任务或新金融执行

#### Scenario: A paused manager retains live responsibilities
- **WHEN** 决策暂停或资源压力限制新任务，但持久管理范围仍有活跃/未决资源
- **THEN** 保留管理权和必要维护责任，按有限容量继续核验，不因暂停或拥塞允许另一管理者抢占

### Requirement: Capacity returns only after actual workers finish
运行容量 SHALL 绑定真实 worker 和资源生命周期，已开始的同步调用、客户端初始化、刷新、长驻连接及子任务未结束前不能因调用者断开、Future 取消或总预算到期提前归还。未开始的排队工作 SHALL 可安全取消并归还其排队预留。进程退出后的持久交易协调 SHALL 继续按原执行证据恢复，不能以 worker 消失推断未提交。

#### Scenario: A timed-out caller leaves a running dependency
- **WHEN** 请求预算到期或调用者断开，而底层同步 worker 仍在执行
- **THEN** 请求返回明确超时阶段，worker 继续占用实际额度至结束，后续请求不能使用已被虚假释放的容量叠加执行

#### Scenario: Queued work is cancelled before starting
- **WHEN** 有证据确认某工作仍未开始且从队列取消成功
- **THEN** 释放其排队预留而不提交金融写入，保持其未执行状态，其他工作可按既定预算继续

### Requirement: Shared catalogue capacity covers MCP and non-MCP clients
市场目录加载与缓存 SHALL 覆盖 MCP、scheduler、Agent、聊天和后台维护的相关初始化路径，在同一进程内复用元数据并合并同 key 刷新。不同进程的独立加载及副本 SHALL 纳入双容器峰值和主机预算。key SHALL 区分交易所、市场类型、公共端点及公共选项/版本，不以 secret 或凭据轮换作为身份；观察时间真实可辨。可变账户客户端、凭据及执行状态 SHALL 隔离，公开缓存不能代替当前账户证据。

#### Scenario: Cold initialization occurs across runtime paths
- **WHEN** 同一进程中的 MCP 和非 MCP worker 同时需要同一有效市场目录
- **THEN** 同 key 只进行一次实际刷新，其他调用在预算内复用结果或明确失败，不分别反复加载和深复制完整目录

#### Scenario: Two containers initialize the same catalogue
- **WHEN** Web 与 scheduler 分别冷加载同一公开市场目录
- **THEN** 每个进程内各自合并刷新，报告把双方实际加载、消费者副本和同时刷新峰值计入预算，不将进程内合并声称为部署全局只一次加载

#### Scenario: Cached metadata is reused by another account
- **WHEN** 两个账户复用兼容的公开市场元数据
- **THEN** 元数据复用不共享可变账户客户端、凭据或执行状态，写入仍核验对应账户和动作依赖的当前事实

#### Scenario: A refresh cannot complete
- **WHEN** 目录刷新失败、过期或预算耗尽
- **THEN** 返回其来源、原观察时间和缺项；允许使用旧公开数据时明确陈旧状态，不宣称刷新成功或借缓存放行交易

### Requirement: Cache and client budgets include peaks and retirement
目录、公开行情、客户端、协调器和相关运行缓存 SHALL 有容量及生命周期预算，计量包含保留对象、活跃消费者副本、刷新时新旧目录、解析缓冲及初始化峰值。字节计量口径 SHALL 明确并与实测进程内存分开。超额对象 SHALL 不进入缓存，刷新须先获得峰值额度。配置删除/轮换、到期及停止使用后 SHALL 回收无活跃引用对象，不能破坏活跃互斥或未决恢复证据。

#### Scenario: Entry count fits but catalogue bytes do not
- **WHEN** 市场目录条目数未超限但计量字节或刷新峰值超出可用预算
- **THEN** 有界拒绝加载/缓存并指出限制，不因条目数合格忽略内存峰值，也不先保留超额目录再清理

#### Scenario: Repeated configuration changes retire cache keys
- **WHEN** 反复增删配置、轮换凭据或替换目录版本且旧对象已无活跃引用
- **THEN** 在声明生命周期内回收旧客户端、缓存和协调器，稳定负载下占用不随历史 key 无限增长，保留必要持久交易证据

#### Scenario: An active refresh or coordinator is retained
- **WHEN** 需要回收缓存但旧对象仍被刷新、账户互斥或真实 worker 使用
- **THEN** 不撤销其活跃协调，不把被保留字节算作已释放；额度不足时限制新工作而非形成重复并发写入

### Requirement: Deployment budgets preserve host headroom
部署容量配置 SHALL 提供 Web 与 scheduler 的 memory/swap/CPU 约束及应用预算，并核验两者峰值承诺、其他必要服务和主机保留余量共同满足容量 profile。值 SHALL 来自测量并可追溯，不默认无上限或以 swap 代替内存容量治理。实际生产更新 SHALL 属于后续明确请求的 rollout；实现、隔离验收或发布镜像本身不能表示生产限额已经生效。

#### Scenario: Two containers independently fit but exceed the host
- **WHEN** Web 与 scheduler 各自预算可满足却与主机必要服务及余量合计超出声明容量
- **THEN** 容量配置校验失败并指出缺口，不能只凭单容器验证通过声称主机安全

#### Scenario: A verified image is published without rollout
- **WHEN** 资源治理代码和镜像在隔离环境通过验证并发布
- **THEN** 报告本地/隔离验证和待执行的生产配置及现场复验，不修改生产容器或宣称线上 OOM 已修复

### Requirement: Effective resource limits are reported truthfully
运行时 SHALL 读取并报告实际可获得的进程、容器及 CPU 有效限制，包括 memory 当前/峰值/上限、swap、OOM 事件和 CPU 配额/节流，并区分配置意图与实际生效值。不可读取或环境不支持时 SHALL 明确缺项，不能填零或假设无限安全。容量准入 SHALL 根据已验证的有效限制和声明预算判断，限制不一致时明确诊断。

#### Scenario: Declared settings are not applied to a container
- **WHEN** 应用设置的容量 profile 与实际容器限制不一致
- **THEN** 诊断展示两者及影响，不把环境变量、文档示例或宿主 CPU 数作为已生效限制的证据

#### Scenario: A platform lacks required counters
- **WHEN** 当前平台无法读取某类 cgroup、主机压力或 OOM 计数
- **THEN** 返回不可用及范围说明，仍保留其他真实观测，不据缺失计数断言未发生压力或 OOM

### Requirement: Resource evidence survives the affected worker
系统 SHALL 持续保存有界、低基数的主机/进程/容器资源及各类运行、排队、缓存、刷新计量，覆盖故障前后并关联时间、镜像、配置和启动身份。证据保留 SHALL 不仅依赖可能被杀死的 worker。归因 SHALL 区分有明确证据的 global OOM、容器 OOM、资源压力与证据不足；后续采样不能证明过去故障原因。诊断 SHALL 脱敏，不用密钥或操作身份生成无界指标标签。

#### Scenario: Kernel evidence confirms a global OOM
- **WHEN** 故障时段的内核、进程与容器身份证据可对应到 global OOM
- **THEN** 保留关联证据及其确定范围，区分被杀对象与未经证明的内存增长来源，不仅凭受害进程推断其为根因

#### Scenario: Later pressure lacks the incident record
- **WHEN** 后续采样显示内存或 swap 压力但缺少故障瞬间的 OOM 事件
- **THEN** 报告压力事实和关联推断，明确归因证据缺失，不将其写成已确认同容器再次 OOM

#### Scenario: A worker dies before it emits a final diagnostic
- **WHEN** worker 或容器被终止并失去自身最后日志
- **THEN** 可读取此前已持久的资源序列和独立事件证据，保留缺口及重启身份，不能将采集器失联解释为零占用

### Requirement: Resource failures preserve original financial continuity
资源拒绝、超时、进程死亡和重启 SHALL 保留原父子操作、请求指纹、客户端/订单身份、已确认回执、未知状态及持久管理权。未提交工作 SHALL 明确未执行；可能已提交的工作 SHALL 只按原身份核验，不换 ID 重发、不自动执行未尝试尾项、不因资源清理释放未知金融协调。容量恢复不能扩展原调用者权限或交接范围。

#### Scenario: A process dies after submission
- **WHEN** 金融请求可能已发送且 worker 因资源故障终止
- **THEN** 重启后恢复原操作身份和未知证据，只读核验实际结果，同 ID 回放不会新增订单

#### Scenario: Capacity recovers after a partial batch
- **WHEN** 前项已确认完成、后项未开始，批次在资源不足时终止
- **THEN** 查询保留前项事实和未执行尾项，不自动续跑尾项或把容量恢复当作其重新授权

#### Scenario: A repeated invocation cannot acquire capacity
- **WHEN** 同一 operation_id 的新调用未准入，但原操作可能已经提交
- **THEN** 仅将本次调用标记 busy/未开始，原状态及恢复身份保留；原账本不可读时明确不可验证，不将历史未知或已完成事实覆盖为从未执行

### Requirement: Isolated capacity verification has explicit pass criteria
系统 SHALL 提供使用临时数据、假账户/依赖及受限隔离进程/容器的验收入口，按预先声明的 profile 覆盖冷启动、稳定负载、并发/刷新峰值、慢依赖、配置轮换、资源耗尽和重启。验收 SHALL 检查各额度及队列不超界、缓存生命周期、Web SLO、主机余量和原金融身份连续性，保存关联镜像/配置的报告。不得用生产交易写入或主机 global OOM 制造验收。

#### Scenario: Concurrent slow work reaches declared limits
- **WHEN** 隔离负载同时占用多池并制造慢依赖和目录刷新
- **THEN** 测得运行/排队/计量峰值保持在 profile 上下界及预算内，超额请求有界返回，Web 满足声明 SLO，未准入工作产生零交易所写入

#### Scenario: Isolated termination exercises recovery
- **WHEN** 在受限隔离环境终止已保留未知身份的 worker 并重启
- **THEN** 报告保存故障前证据和有效限制，原操作可核验且不重发，主机无新增 global OOM，测试不访问生产账户

#### Scenario: A measured requirement is not satisfied
- **WHEN** 占用持续随退休 key 增长、峰值越界、Web 未达 SLO 或关键恢复未按预算推进
- **THEN** 验收失败并报告具体 profile、阶段及缺项，不能只凭容器能启动、单次 health 成功或请求结束判定通过
