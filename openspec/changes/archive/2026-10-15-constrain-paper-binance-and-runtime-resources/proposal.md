# Proposal

## Why

用户提供的 2026-10-13 内核日志已证明主机发生 global OOM、swap 耗尽并杀死容器内 Python；10-14 的断联与资源压力相关，但没有故障瞬间的 OOM 证据，不能归因到同一个容器。当前 Paper 默认搜索四家交易所并在报价失败时跨场所回退，完整市场目录反复加载和复制；MCP 与 scheduler 的各自线程限制也没有覆盖整体内存峰值及主机余量。

用户补充的 close 回执还暴露了执行后的重复核验与返回膨胀：旧 v2 条件退出已受理、尚未成交，回执仍给出 get_trade_operation 指引，随后该调用因把 account_scope 当作 config_id 返回 `Conflicting trusted context: config_id`；附件原始 JSON 的 UTF-8 长度约 19.9 KB，重复嵌入 11 条旧评估规则。按用户最新要求，移除操作查询工具，执行、简短回执和后台状态更新由系统承担。

## What Changes

- **BREAKING**：Paper 仅支持 Binance 已核验、受现有市场能力支持的 USDT 现货和线性永续标的。搜索、报价、新旧金融入口、显式 exchange 参数、配置导入及后台估值/撮合统一限制为 BN，取消隐式跨场所搜索和 fallback；实盘其他交易所 profile 不受该 Paper 策略影响。
- **BREAKING**：历史非 BN 及来源无法证明为 BN 的 Paper 资源从默认持仓、订单、交易事件、标的扫描和活跃维护集合过滤。保留原账本与受权限保护的原操作审计；过滤不转换场所、不清算、不释放已承诺资金。混合账户的完整权益及竞赛收益无法可靠计算时明确标记不完整并不参与排名。
- **BREAKING**：从 Agent、聊天和 MCP 移除 get_trade_operation、get_operation，亦不保留可直接调用的旧操作查询适配器。Agent 可写角色仅绑定 execute_trade_actions；live MCP 可写/只读为 7/6 项，Paper 为 10/7 项。保留正常账户、订单、成交读取及受权限保护的 HTTP/UI 历史详情，不把它们改装成另一个必调用的操作查询工具。
- 所有 open/close/edit/cancel/protect 及兼容金融入口在一次调用内完成实际所需的前置准入和执行，得到权威受理 ACK 就返回 submitted，不为再次 GET 订单/仓位或等待未来成交延迟返回。已确认的成交直接报告，后续事实/未知结果由后台按原身份核验并更新上下文和界面；不要求模型第二次取证，也不以去掉查询工具为由重复金融写入或补跑尾项。
- 新旧交易默认结果统一精简：单项回执不超过 4 KiB、最多 20 项的整批不超过 32 KiB（序列化 UTF-8），保留真实状态、订单身份、数量及必要异常，去除重复别名、旧评估树和规则中重复 blocker。完整原始回执继续保存并可由用户按需在授权详情中查阅；默认输出不再给出 get_operation 恢复指令。
- 市场目录、公开行情、客户端和协调器建立容量及生命周期预算，覆盖刷新期间新旧副本、解析和客户端初始化峰值；减少重复加载及完整深复制，同时保持可变客户端、凭据和执行状态隔离。
- 为 Web 与 scheduler 建立覆盖多类工作及嵌套并发的进程总准入和有界排队；压力时限制新重任务，为原操作核验及必要维护保留容量。Paper 扫描和历史分页的工作集也须有界。
- 提供经测量确定的容器 memory/swap/CPU 与应用容量配置，校验两个容器的总预算及主机保留余量；采集实际生效限制和故障前资源证据，区分 global OOM、容器 OOM、压力与证据不足。
- 保持 v3 顺序执行、权限、原父子操作 ID、未知结果、持久管理权和幂等恢复；资源治理不能造成重复金融写入或自动续跑尾项。

## Capabilities

### New Capabilities

- `paper-binance-scope`: Paper 的 BN-only 网络与交易范围、历史过滤、来源判定以及资金和审计保留规则。
- `runtime-resource-capacity`: Web/scheduler 的总容量、内存及峰值预算、缓存生命周期、部署隔离、资源观测和故障连续性。

### Modified Capabilities

- `model-tool-catalog`: Paper 改为 Binance 范围，移除操作查询工具并同步目录数量、描述及有界紧凑结果，保留原金融权限。
- `paper-ordered-trading`: 将场所限制纳入可信执行边界及整批静态校验，保留数量、交易周期和原操作恢复含义。
- `mcp-service-resilience`: 将有界工作和读取预算衔接进程资源容量，补充 ACK 返回、资源拒绝/死亡及后台恢复语义。
- `trading-tool-contracts`: 移除模型操作查询契约，明确单次执行及时返回、后台原身份核验及证据消费者的一致性。
- `decision-state-context`: 执行层负责状态更新，默认上下文和工具结果不引导第二次查询，原始详情由用户按需读取。

## Impact

- Paper：`backend/mcp/paper_market.py`、`paper.py`、`paper_orders.py`、`paper_batch.py`、`paper_competition.py`、新旧工具/HTTP 入口、Paper 配置界面及 QQBot 文档。
- 运行时：`backend/mcp/exchange_runtime.py`、`resilience.py`、`backend/utils/market_data.py`、scheduler、保护/成交同步/记忆/新闻/流连接工作及诊断接口；可能新增共享容量和缓存服务模块。
- 交易返回与模型绑定：`backend/agent/tool_registry.py`、`independent_tools.py`、`unified_trading.py`、Agent/聊天图及提示、MCP 注册和兼容路由、trade_receipts/trade_recovery 与决策记录投影、`DecisionMessages.jsx`；后台核验服务保留，模型操作查询名称和调用流程删除。
- 数据：增量保存 Paper 策略版本、可信来源与过滤/估值完整性信息，保留原账户、现金、债务、保证金、预留、请求指纹、回执与事件；不重建历史资金、不清空数据库。
- 运维与验收：资源配置/只读采集及隔离负载验证入口、`.env.template`、部署指南和发布烟测；预算数值由容量报告确定，不凭旧单进程 RSS 拍定。
- 文档与回归：同步 MCP、交易工作流、回执、提示、工作台及 QQBot 集成说明；以脱敏 close fixture 验收零二次模型查询、ACK 后不等待额外取证、默认返回字节上限、原回执/指纹不变和后台事实自动更新。
- 本次只生成规划产物，不修改实现或生产容器。后续 apply 可实现并在隔离环境验收；真实容器更新、主机限额应用及现场复验须在用户请求 rollout 后执行，镜像发布本身不证明线上修复。
