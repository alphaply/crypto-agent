# Tasks

本清单记录 apply 完成情况，验证证据见 docs/BN_SINGLE_CALL_VALIDATION.md 与 docs/CAPACITY_VERIFICATION.md。资源数值按 design 的校准/最终验收流程产出；真实生产 rollout 不计入本 change 的完成条件。用户最新要求已纳入：移除操作查询工具，一次金融调用及时返回，后续核验由后台承担；原 get_trade_operation 失败改为消除该工具工作流，不放宽可信上下文校验。

## 1. Paper BN 策略与入口

- [x] 1.1 建立隔离历史 fixture，包含纯 BN、非 BN、缺失/冲突来源、曾回填 BN、同名同方向、混合预留、旧默认配置和 v2/v3 回执；保存原行/资金/指纹基线，验证 fixture 无真实凭据且可重复加载。
- [x] 1.2 实现统一 Paper venue policy 与新配置校验，分开旧存储兼容加载和有效 BN 配置；测试旧非 BN 默认仍能鉴权/审计/借还款，新保存/导入明确非 BN 被拒绝，历史来源不随默认值变化。
- [x] 1.3 将 paper_market 搜索/报价/resolve_quote 改为仅核验 BN 现有 USDT spot/linear swap，移除四场所扩展、外场所 hint 及 fallback；以网络调用 spy 验证 BN 失败和陌生别名均产生零其他场所请求。
- [x] 1.4 将共享策略接入 canonical、隐藏旧工具、HTTP、paper_batch 及后台入口，整批预查显式场所和原目标来源；测试后项越界时首项零金融变化，直接旧名称调用也不能绕过。
- [x] 1.5 更新交易上下文/schema、PaperTradingPanel 的行情来源和配置导入导出提示；验证有效 BN、内置交易 1 项/只读 0 项、live 7/6 项、Paper 10/7 项及原权限，无 get_operation/get_trade_operation，实盘非 BN profile 仍可用。
- [x] 1.6 同步 README 与 docs/QQBOT_PAPER_MCP.md 的 BN 范围、旧客户端 breaking 行为和默认目录说明；逐项核对文档中的场所、工具名称、鉴权及请求示例与本组测试。

## 2. 历史投影、资金及原操作

- [x] 2.1 增量保存有效策略版本、来源分类和证据关联，取消 upgrade_account 按默认值/同 ticker 回填；测试两次升级不变原行金融事实，曾回填 BN 但无原周期证据的资源仍 unresolved。
- [x] 2.2 为新 BN 资源提供可独立寻址的持久身份、存储 key 和周期，保留被过滤旧资源 key/ID；测试现货同 symbol、合约同 symbol/side 不合并旧数量/成本/margin/周期，无法独立寻址路径明确拒绝，新成交不能追认旧来源。
- [x] 2.3 分离 raw ledger 与默认查询/活跃维护投影，覆盖账户 facets、订单、成交/历史和后台 instrument 集合；测试默认只见可信 BN，原 ID HTTP/UI 全审计受 namespace/user 权限保护且不联网，不恢复已移除的操作工具。
- [x] 2.4 保持 cash/debt/margin 及全部预留参与准入、还款和 reservation refresh，阻止过滤释放资金或使用旧库存；以混合资金 fixture 检验可用现金、新 BN 入场/退出、银行和重复开户的账本不变量。
- [x] 2.5 增加过滤范围、原因汇总和估值完整性，修正混合账户 equity/净收益/收益率及榜单资格；测试缺项返回 null/明确历史观察值、不按假零值排名，借还款/调整不成为交易利润。
- [x] 2.6 原操作查找/指纹比对先于新默认场所解释，保留父子 ID、索引及完成/失败/unknown/not_executed；旧 fixture 验证非 BN 重试零网络/零重复金融变化、尾项不续跑，已原子提交子回执能修复丢失父响应，无证据项继续未解决，BN 原幂等仍通过。
- [x] 2.7 更新 PaperTradingPanel/PaperDataPanel 对缺项、过滤承诺和未排名的展示，避免 Number(null) 变零；完成中文/英文混合账户及旧审计人工检查，记录截图，并在 Paper 文档说明逻辑过滤、资金保留和同名资源边界。

## 3. 目录、缓存和客户端容量

- [x] 3.1 提供 MCP 与非 MCP 共用的进程内公共目录模块，公共选项/端点/市场分 key 并合并冷加载/刷新；测试同进程多路径同 key 一次加载，跨进程副本分别计量，凭据轮换不增公开 key。
- [x] 3.2 对必要目录字段建立精简快照与客户端适配，替换完整深复制；测试两个可变 CCXT 客户端的 markets/currencies/timeDifference/凭据隔离，保留已锁定依赖的合法 symbol/precision/合约单位能力，并比较冷/刷新分配峰值。
- [x] 3.3 为目录、quotes、初始化消费者及刷新新旧/解析空间增加 entry/bytes/peak 准入；测试超额目录有界拒绝、不先无界解析/入缓存，刷新失败保留真实观察时间，TTL 到期不绕过容量约束。
- [x] 3.4 接入 MarketTool、scheduler 保护客户端及其他发现的 load_markets 路径；用路径覆盖测试验证 MCP 与非 MCP 都使用受限目录服务，Paper 仅读 BN，而实盘各 profile 保留各自公共端点能力。
- [x] 3.5 对客户端、account coordinator、退休 key 和流连接实现受限引用/生命周期回收；测试反复删除/停用/轮换后无活跃引用对象按期回收，活跃锁/真实 worker 未提前关闭，未决操作持久证据不丢失。
- [x] 3.6 文档化缓存计量口径、进程边界、峰值和配置项，在 .env.template 提供有限容量说明；以测试配置验证非法值明确报错、文档示例能加载，不承诺跨容器只刷新一次。

## 4. 总准入与后台并发

- [x] 4.1 实现 Web/scheduler 进程总额度、类别保留额度、有界队列和等待预算，以及父子额度/公平调度规则；测试多池同时满载、nested 并行和顺序子项不会越限或互等死锁，保留额度包含在总限中。
- [x] 4.2 将 MCP resilience、目录初始化和金融入口接入总准入，保留轻量 Web 通路；扩展 test_mcp_resilience 验证有界拒绝零写入、同 ID 旧状态不被 busy 降级、ACK 后不等待额外读取许可、认证锁不阻塞事件循环。
- [x] 4.3 将 scheduler Agent、maintenance、成交同步与保护池接入总预算，继续按配置去重和保留管理责任；扩展 scheduler/management 测试验证普通负载满时关键核验仍可推进、暂停不释放 owner。
- [x] 4.4 将 memory_updates、新闻并行子任务与 execution_stream 的线程/订阅/重连纳入额度和生命周期；测试并发子工作、长驻连接、重连退避和关闭释放都受限，独立 pool 不能绕过总预算。
- [x] 4.5 将容量释放绑定真实 worker 完成，区分未开始取消与已运行超时，读取有效 CPU quota/affinity 辅助定额；测试调用者断开后不叠加新 worker，未知提交与原操作/管理权在退出重启后保持。
- [x] 4.6 在运行时文档与 .env.template 说明总额度、角色保留、busy/超时和平台缺项，给出进程内计量示例；核对配置校验及测试结果，不以 os.cpu_count 或取消 Future 作为实际容量证据。

## 5. 有界存储工作集与维护

- [x] 5.1 改造 Paper instruments/maintenance 为稳定进度的 BN 有限批次，限制每轮标的/账户/时间；测试大规模混合账户连续推进且原非 BN 状态/资金不变，网络不进入金融事务锁。
- [x] 5.2 将 history/事件读取改为存储级分页，兼容旧读取范围并按已检查记录推进 cursor；测试连续纯过滤空页后仍可见后续 BN 事件、has_more/delivery_key/namespace/通知偏好不变且无重复交付。
- [x] 5.3 将 leaderboard 和账户投影改为有界扫描/完整投影发布，保护 tick 只扫描活跃/未决计划；测试扫描尾部未完成不冒充完整排名/对账，DONE 计划不会持续占用解析工作集。
- [x] 5.4 对单个超大账户/回执先检查读取/解析预算，提供有界投影/审计续读；测试不能完整核验资金时明确未处理且零新金融变化，不能只读部分预留继续准入，也不改写原记录。
- [x] 5.5 文档化各页/每轮预算、过滤空页和未完成语义；验证手动连续读页、后台分批重启续读和榜单观察时间与本组集成测试一致。

## 6. 有效限制与故障证据

- [x] 6.1 增加只读进程/cgroup/CPU/主机资源采集，区分配置值、实际值和 unavailable；用平台 fixture 验证 memory/swap/peak/OOM/CPU 节流及缺项，Windows 无相应计数时不填假零值。
- [x] 6.2 将启动/容器/镜像/配置身份、资源序列和请求/worker 阶段写入有界持久诊断，控制 buffer、采样、轮转及标签基数；测试采集故障不阻塞金融事务、长期占用有上限且无凭据泄漏。
- [x] 6.3 提供只读宿主事件关联与诊断报告入口，保留旧容器映射和统一时间口径；以 global OOM、cgroup OOM、仅压力、重启后 OOMKilled=false fixture 验证归因及证据不足不会混淆，不启动真实故障注入。
- [x] 6.4 在部署/故障排查文档写明持续采集方式、权限、容量及不可读指标处理；通过 fixture 报告核对原操作关联、脱敏和故障前证据在 worker 死亡后仍可查。

## 7. 双容器预算与容量验收

- [x] 7.1 定义版本化 capacity profile 与报告格式、校准和最终验收模式，预先声明环境/业务规模/负载/时长/预算/阈值/Web SLO/主机余量；测试缺项或事后改标准不可通过，报告保存原失败结果及镜像/配置身份。
- [x] 7.2 提供受限的双容器启动与隔离负载入口，使用临时数据、假金融账户/依赖和受核验的目录样本；验证不读生产 .env/数据库/凭据，禁止制造 host global OOM，能采到两进程独立目录的实际峰值。
- [x] 7.3 在检查过主机余量的隔离环境执行 idle/典型/并发/同时刷新/配置轮换校准，形成报告并据此确定 memory、memory+swap、CPU、池/缓存/扫描额度；交付可复现命令与数值依据，标注目标生产主机尚未验证。
- [x] 7.4 冻结最终 profile 后执行完整持续验收及受限 worker 终止/重启，验证池/队列/bytes 不越界、退休占用稳定、Web 达 SLO、关键核验推进、原 unknown 身份无重发/尾项续跑，报告实际限制及无新增 host global OOM；失败必须重新校准并生成新版本报告。
- [x] 7.5 将验证过的 resource profile 接入 Docker 启动说明和 scripts/release-archived-change.ps1 的隔离烟测，文档写明 swap 总量语义、双容器合计与主机余量校验及安全回退；以临时容器实际 inspect/cgroup 值验证限额生效，烟测失败不继续依赖发布步骤，不修改生产容器。

## 8. 单次金融调用、后台状态与紧凑回执

- [x] 8.1 从用户案例建立脱敏回归 fixture：v2 SHORT 条件 close 已受理、waiting_trigger、请求0.03/成交0、仓位旧观察0.06，含11重复 blocker 规则和旧 scope/config 冲突调用；验证无真实凭据、原始回执/指纹基线稳定且三种事实可区分。
- [x] 8.2 删除 Agent/聊天绑定、registry 可调用映射及新旧 MCP 注册/适配器中的 get_trade_operation/get_operation，保留纯后台核验函数；测试所有角色/权限目录数量、缓存名称不可用且不执行原查询，无同义工具、operation facet 或 query 动作替代。
- [x] 8.3 将 v3 及旧金融入口改为前置准入、持久意图、执行、保存权威响应并及时返回；测试 ACK 后 fetch_order/fetch_positions 故障不会拖住或改坏响应，已知成交/撤单直接报告，缺成交证据不填假零，后项真实 completed 依赖仍在同调用内核验。
- [x] 8.4 接通 REAL 各退出模式、STRATEGY、SPOT_DCA、live/Paper MCP 及历史版本的自动后台核验和消费者更新；测试无需模型第二调用也能恢复原订单成交/丢失 ACK，旧父子/owner/principal/原 symbol 关联正确、无新金融写入/未尝试尾项续跑、保留额度和退避生效。
- [x] 8.5 在持久原回执后为全部金融返回、ToolMessage 和默认记录投影接入统一摘要；测试原 request/hash/raw 不变，submitted/waiting_trigger/filled=0、当前/旧/客户端身份、失败阶段、edit 部分成功、cleanup 未决及观察时间保留，旧 query 失败不变成交易失败。
- [x] 8.6 实现单项4096/整批32768 UTF-8 字节上限和结构化去重/省略，去掉 previous_assessment、重复数量/position 别名及同 blocker 规则，诊断有数量和原记录引用；测试实际返回 JSON 长度、20项全部索引/状态/身份、长历史及重复刷新不使默认输出增长，不能截断 JSON/ID或伪造成功。
- [x] 8.7 通过授权 HTTP/UI 保留版本固定、有界分页的原始/最新回执详情，DecisionMessages 默认仅显示摘要、用户展开才加载完整 JSON；测试权限/分页/证据时间和待触发显示，默认不载入双份完整回执，详情无金融写入且不注册为模型工具，记录中文/英文案例截图。
- [x] 8.8 删除默认提示、工具描述、历史模型投影和示例中的调用 get_operation/二次核验指导，更新 README、docs/MCP.md、TRADING_WORKFLOW.md、TRADE_RECEIPTS_AND_RECOVERY.md、PROMPT_DESIGN.md、WORKBENCH.md 及 QQBot 文档/集成 skill；以 schema/内容搜索及模型流程 fixture 验证一次金融调用后无框架追加查询步骤，正常记录读取、HTTP/UI 审计和原金融权限仍可用。

## 9. 跨模块最终回归

- [x] 9.1 运行 Paper、ordered batch、catalog、MCP resilience、scheduler/management、回执/恢复/上下文及权限相关回归，保存结果；验证数量/顺序/周期/先到先得、资金、旧指纹、实盘非 BN、零二次模型核验和后台自动更新共同满足。
- [x] 9.2 运行 npm run build --prefix frontend，在临时受限镜像检查 BN-only、过滤、原 ID HTTP/UI 审计、单次 ACK 返回与紧凑回执及轻量网页；保存构建/只读烟测结果，不用真实交易验收。
- [x] 9.3 对照七份 delta、capacity 报告和每项任务证据完成最终审查，运行 openspec validate constrain-paper-binance-and-runtime-resources --strict；交付实际完成/未验证清单和原数据校验，不将 isolated/release 状态写成生产修复。

## Workflow follow-up

- 用户已要求完成开发并自行提交、构建和推送；本 change 的任务全部验收后，按 archive-release-workflow 同步主规范、归档并发布提交镜像。
- 用户后续请求 rollout 时，再读取真实主机基线、核验最终 profile 与实际镜像，应用两个容器的限额并完成公网只读及资源复验；镜像发布不更新运行容器。
- rollout 回退保留策略/来源证据及已验证限额；旧版本若不能识别 BN-only，只允许金融写入与 scheduler 停用的诊断模式，禁止重新撮合被过滤资源或重放未知操作。
