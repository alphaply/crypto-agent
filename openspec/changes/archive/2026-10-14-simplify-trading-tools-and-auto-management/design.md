# Design

## Context

动机、用户已确认的行为和范围见 [proposal.md](proposal.md)。本变更只产生规划，代码、主规范及其他变更由后续 apply/sync 处理。

已检查的实现决定了以下边界：

- 内置 REAL 独立退出绑定 7 个工具，STRATEGY 独立退出绑定 6 个，附带模式绑定 6 个，SPOT_DCA 绑定 2 个。主 Agent、small Agent 和绑定任务聊天复用 `get_trade_tools_for_mode`。当前交易图自身也顺序处理 tool_calls。
- `trade_batch.py` 已有最多 20 项、父子操作身份、stop_on_error/independent、前项 completed 依赖和持久化恢复；v2 仅支持独立合约，旧契约支持附带模式，现货直接被拒绝。公开 MCP `execute_trade` 是旧分发器，公开 `execute_trade_actions` 只接受 v2。
- MCP server 无条件注册实盘/共享 27 项及 paper 23 项。查询、单项适配器、兼容入口和 paper 配套功能混在同一目录，实际权限在 service/paper 执行时检查。
- paper 的账户修改、订单/预留、事件与单项回执在同一 SQLite 事务中保存；交易行情在事务外读取。当前没有父批次账本，且每项写入返回完整账户快照。
- REAL Agent 对任务标的读取全部交易所仓位及普通/条件订单；MCP 可见范围由 profile 的 selected/all 标的规则限制。strict_owned 的写入范围小于查询范围。
- 当前工作区的 `add-exclusive-position-management` 已实现自动纳管、外部变化对账、原子范围记录、跨进程操作 reservation 和清理恢复，但要求手填有限范围、默认为 strict_owned。该变更尚未同步到主规范；本变更复用其基础设施，并明确调整默认规则。
- `execution_order_links.config_id` 同时承载原订单归属及成交统计，不能为了新的管理者直接改写历史成交归属。现货不同报价交易对会共享同一基础币余额。当前真实账户 scope 基于 API Key 哈希，多 Key 的物理账户关系需要统一保存账户引用。

## Goals / Non-Goals

**Goals:**

- 一个模型交易写入口覆盖现有能力，执行处理器继续按真实市场和退出模式校验。
- 可见交易授权范围内自动取得唯一管理权，并将人工活动纳入可管理事实；认领不要求模型新增步骤。
- 目录、查询投影、执行准入和界面使用共同的范围及能力定义。
- 一次调用在网络超时、进程中断、事务失败或人工并发下仍保留逐项真实结果，不重复金融写入。
- 明确目录数量、能力矩阵、迁移顺序和可以离线验证的验收条件。

**Non-Goals:**

- 不运行任意 Python/JavaScript PTC，不增加前项返回变量引用、条件脚本或等待成交的长期计划。
- 不增加新的交易所、订单类型或自动策略，不把纸上撮合能力视为实盘能力。
- 不允许多个独立管理者同时分配同一真实净仓，不把只读用户变为交易用户。
- 不改写历史收益归属，不删除周期、原请求、回执或已有有效管理者。本变更不包含部署和发版。

## Decisions

### 1. 模型目录与执行注册表分开

保留内部动作处理器和兼容工具的执行注册表；建立按运行身份生成的默认模型目录。权限校验使用独立能力解析器，不再用“模型目录里是否有该名称”代替金融权限判断。

内部交易 Agent、small Agent 和绑定任务聊天各绑定两个名称：`execute_trade_actions`、`get_trade_operation`。只读临时聊天、Memory Agent 和策略摘要绑定零个交易工具。绑定聊天继续沿用任务 owner 与审批。

默认移除 `open/close/edit/cancel`、模式特定的 open_position/update/cancel 等同义工具、`adopt_position_real/adopt_perpetual_position` 及 MCP `execute_trade`。直接调用旧名称的兼容客户端继续经过原身份、模式、权限和幂等校验；隐藏不是废除旧执行能力。不会用新格式重新构造旧操作 fingerprint。

考虑过仅缩短描述或仅把旧名称标为 legacy：这些做法仍将重复 schema 和选择项送入模型，因此采用独立目录。

### 2. MCP 核心目录及权限矩阵

MCP `tools/list` 在认证请求上下文中生成目录，`tools/call` 再次认证和校验。缓存只能按 key 类型、权限、授权 profile 集合及配置能力版本隔离；不得按全局 server 实例共享某个用户的目录。

| 名称 | 作用 |
| --- | --- |
| get_trading_context | live 的授权 profile、标的范围、模式、管理资格和动作能力；paper 的规则、namespace 下 bot 身份及账户能力 |
| search_symbols | live 按 profile 搜索允许的市场；paper 保留跨交易所真实目录和股票代币发现 |
| get_market | 真实行情及 K 线，保留来源/时间/缺项 |
| get_news | 共享新闻摘要 |
| get_account_state | 按 include/fields 获取余额、仓位、现货库存、预留及挂单摘要 |
| get_trade_records | 区分 orders/order/fills/history 的分页或指定身份查询 |
| execute_trade_actions | 唯一交易写入口 |
| get_operation | 根据可信 live/paper 身份查询原操作 |

paper 额外暴露 `paper_bank`、`configure_paper_account`、`get_paper_leaderboard`。configure 使用有类型的 create/update 动作保留开户、默认杠杆、名称及排名/通知偏好；不通过查询隐式开户。bot 预建和修复由已有启动/Key 生命周期维护处理。

| 身份/权限 | 默认目录数量 |
| --- | ---: |
| live read | 7 |
| live read + trade 和/或 cancel，存在可用交易能力 | 8 |
| paper read | 8：7 个核心读取加排行榜 |
| paper read + trade | 11 |

混合多个 live profile 的 Key 使用名称并集，不按 profile 数量重复工具；上下文列出每个 profile 的差异，调用时验证具体 profile。只有 cancel 的 live Key 仍看到执行入口，但仅允许撤单动作；整批混入 trade 动作在首笔前拒绝。paper 不额外借用 live cancel scope。`scan_paper_events` 移出模型目录，现有 `/api/paper/events` 与旧兼容调用继续为机器人事件投递提供能力。

考虑过将银行、排行榜等所有功能都塞进交易 execute：它会混淆账户行政操作、公开排行和交易意图，保留这三类独立 paper 能力。

### 3. 新统一契约采用 contract_version=3

保留 v1/无版本及 v2 的原始解释、请求哈希和重放行为。新增 v3，默认模型 schema 只描述当前允许的 v3 动作；不能按字段缺失猜版本。

v3 使用固定结构：`contract_version=3`、`actions`（1–20 项）、可省略的 `execution_policy=stop_on_error|independent`。MCP 另有稳定 `operation_id` 和互斥的 live `profile_id` / paper `user_id`；内部 Agent 的账户、管理者、标的默认值和操作身份由可信上下文注入。

一次父请求仅属于一个 live profile/账户或一个 paper namespace/user。执行引擎由可信任务配置或认证 Key 推导，参数不能选择另一引擎、namespace、owner、config_id 或绕过身份。paper 动作可指定其现有支持的 market_type、exchange；live 市场类型来自 profile。多标的任务允许动作注明授权 symbol，但保留共同预算及逐标的核验。缺省 symbol 仅在可信默认唯一时补齐，歧义在首笔前拒绝。

使用有类型的 action 判别模型，未知字段拒绝，不提供任意 tool_name/arguments 字典。方向和数量沿用基础币语义，预算式 sizing 仅在原引擎支持的入场动作接受。

| action | REAL/STRATEGY 独立合约 | 附带保护合约 | live spot/SPOT_DCA | paper |
| --- | --- | --- | --- | --- |
| open | 原有限价入场，不附带 TP/SL | 限价入场，沿用 required/optional TP/SL | 限价买入，side=LONG | 现货买入或 LONG/SHORT 入场，原有市价/限价、sizing 及可选入场 TP/SL |
| close | 原有市价/限价/条件退出及 fixed/all | 原有减仓能力，STRATEGY 保持市价退出 | 卖出或现有原生退出能力 | 原有市价/限价/条件退出及支持的 all |
| edit | 入场/退出剩余量及适用价格 | 入场单及原生可改目标 | 可核验目标的现有原生改单能力，缺能力明确拒绝 | 原有可改单目标，v3 使用 remaining_amount |
| cancel | 普通/条件当前有效单 | 原有挂单 | 入场单或管理退出/OCO 组 | 本账户待执行订单 |
| protect | 不提供，使用 close 建独立退出 | 调整附带整仓 TP/SL | 现有原生 TP/SL/OCO | 原有单腿或成对 whole/partial 保护，省略已有腿保持原值 |

protect 不扩展独立合约的整仓保护。edit 省略字段保持最新人工参数；v3 不隐式改变方向、订单类型或 fixed/all。旧 paper edit amount 改为 fixed 的语义仍只由旧入口解释。返回的规范目标身份包括 ordinary/algo/native exit/OCO/paper 所需类型信息，cancel 能解析组身份但不按裸数字猜通道。

paper protect 允许只设置 SL 或 TP，更新一腿时保留省略的已有腿；双腿共存才使用 OCO 关联与互斥成交，不能以统一动作要求同时填写两腿。原有入场保护在真实模拟成交后按原规则激活。

各引擎公开其真实允许的组合，不能将能力矩阵理解为所有订单组合均支持。例如 live 独立条件限价 all 仍拒绝，paper 原有周期约束的限价 all 可以保留。

### 4. 顺序执行与预检边界

统一执行器复用已有批次、作用域、幂等和准入服务。整批先验证格式、数量单位、依赖、可信身份、权限和已知模式/能力；网络读取发生在短存储事务之外。动态余额、仓位、杠杆、订单状态和准入在每项轮到执行时核验。

cancel 放第一项时，后项 open 的动态余额或杠杆失败不得阻止已合法且独立可撤的前项。整批缺权限、非法字段或已知不支持模式仍在首笔前拒绝；不把前置 cancel 当成绕过字段和权限检查的手段。

默认 stop_on_error 在 failed/pending/unknown/被阻塞项后停止，后项记录 not_executed。显式 independent 保留现有规则：depends_on 只引用不重复的此前整数索引，前项 completed 才满足；相关未知执行仍由动态准入限制，不依赖“同方向”或旧错误字符串猜测。

顺序是动作提交和核验，不等待限价成交。已提交 open 后无已成交仓位的 close 失败；既有周期/引擎支持的入场附带保护按原规则执行。多个金融提交不回滚已确认动作。

### 5. 操作账本、重放与精简投影

父请求在首笔影响前持久化规范意图、契约版本、策略、依赖和身份。子身份由父请求与索引稳定生成；paper 等 160 字符外部 ID 用有界域隔离哈希生成子 ID，并持久化父/索引映射，不直接无限拼接后缀。

live 延续当前 MCP 操作账本及内部 run_once；统一层不能因 alias、目录变化或请求超时生成新 ID。旧操作重放继续用原哈希；新 v3 请求在同一身份下改动作、参数、策略或依赖均拒绝。

默认模型投影保留：父/子操作查询身份、index/action、status/order_status、当前及必要旧订单身份、数量模式、已知成交量、错误码/阶段、依赖阻塞、相关待核验事项和恢复查询入口。失败发生在首笔前也有逐项 not_executed 与明确未提交阶段。当前 `compact_receipt_payload` 不能直接复用为完整执行投影，因为它可能遗漏 recovery/error_code/blocked_by。

完整回执和实际观察时间继续持久化。默认账户摘要通过 include 请求获取，整批末尾最多一份；各子项不复制全账户、完整仓位证据或旧回执列表。get_operation 的 detail 模式返回完整数据，读取成功与原交易状态分开。

### 6. paper 父子请求采用逐项事务

paper 批次使用公共编排契约和独立账户 adapter，live API Key 永远不能进入 paper namespace，paper Key 永远不能借参数调用 live profile。

父意图首先短事务保存；行情/目标取证在事务外进行。每项在 `BEGIN IMMEDIATE` 下重新读取账户版本、余额/预留及目标状态，并将该项账户变更、订单/预留、事件、子回执和父进度一起提交。直接调用旧 paper 函数必须复用同一逻辑单元，避免外层事务再嵌套已有 _write 的提交。

前项 cancel 提交后，下一项使用已释放的真实预留。保护替换保留原有单项 OCO 原子性；整个批次仍是逐项提交，不是一笔账户回滚事务。行情预算耗尽或账户版本变化按真实阶段记录 failed/not_executed，不将未成交限价单写成即时成交。

崩溃恢复读取子回执及父进度：子项已经提交时只汇总原结果；可能开始但证据不完整时保留 pending/unknown；未开始尾项记录 not_executed。相同父 ID 的重放不自动续跑尾项。变更交易意图必须使用新父 ID，并由调用者明确提出。

### 7. 可见范围与管理资格由同一解析器产生

新增默认 `visible_first_claim` 策略。管理资格来自可信配置已授权的交易账户与 symbols，而非最近一页订单、缓存里出现的 ID、模型文本或只读市场报价。

- 普通 REAL 任务以所选账户、任务标的的可见方向为资格；SPOT_DCA 使用已有标的集合。selected profile 使用允许标的，all profile 按实际观察到的资源或合法明确请求动态解析，不预先遍历/认领所有市场，也不要求手填最多 30 个 scope。
- 可认领资格覆盖可见 LONG/SHORT；实际认领只在发现对应资源或有效管理意图时进行，不预占空的 Hedge 对向。One-way 净仓协调键使用 BOTH。
- paper 使用 namespace/user 隔离；STRATEGY 使用已有模拟 config 账户隔离。普通用户与 bot 各用可信发送者或已返回的 bot 身份，不能跨用户借可见排行取得私人交易权。
- 查询可以报告 eligible/unclaimed/owned/conflict 和目标能力，但不得认领、开户、撤改单或恢复写入。后台为有效可写配置发现资源，或金融动作完成静态授权后，在原调用中认领。

原 trade/cancel 权限、任务开关及绑定聊天审批继续控制交易提交。管理者是持久任务 `config_id` 或 `mcp:<profile_id>`，不是 token、连接、进程或聊天会话。

### 8. 先到先得使用持久化原子登记

复用管理范围表，增加管理策略和生命周期版本。合约协调键包含 physical_account、market_type、canonical_symbol 和 hedged side/BOTH；现货包含 physical_account、market_type=spot、base_asset，使 BTC/USDT 与 BTC/USDC 共用真实 BTC 库存管理权。paper 及模拟账户另加其隔离身份。

首个成功原子插入/条件更新的 owner 获得管理权；失败者读取原 owner 返回准确冲突，不通过启动时间、查询次序或租期过期判定获胜。Web、scheduler 和多个 MCP worker 共用数据库约束及跨进程金融 reservation。原管理者的绑定聊天与多个 principal 不重复认领。

已登记有效 owner 在迁移中优先保留。旧活跃计划、订单或未知提交可证明原管理者时先导入；多份矛盾历史不能随机选一个，标记具体冲突待处理。认领后的订单创建来源继续保留，不把同一范围内人工单当成另一个 owner。

考虑过删除所有 ownership 检查：会使两个周期同时清理同一净仓。本方案将检查依据改为当前范围管理权，并继续保留唯一管理者。

### 9. 管理权生命周期独立于订单及交易周期

```text
eligible resource --> atomic claim --> owned management scope
                                      |
                 waiting / active --> exiting --> cycle done
                                      |
                    owner remains across flat / pause / restart
                                      |
                explicit retirement or transfer --> released / new owner
```

周期 DONE、人工全平、无挂单、暂停决策、服务重启或 token 过期均不释放 owner；后台继续有限对账。暂停只停止新决策，已授权维护及事实核验继续。

范围缩小、删除任务/profile、换账户凭证或交接走显式管理配置/API 流程。没有活跃仓位/订单/未知写入且核验完整时可确认释放；存在资源时需接受并登记的交接方案。显式交接可以转移未决核验责任，但必须保存原操作/客户端身份、全部已知阶段和证据，并由接收者接受；实际进行中的金融 worker 先停止新提交、保存原阶段并被版本隔离，再原子完成交接。未知执行不能通过释放管理权、交接或更换 owner 重发。交接事务更新当前管理权及资源管理关联，原 actor、请求、历史订单/成交归属不重写。旧 owner worker 的迟到 ACK 只更新原执行事实，不获得继续提交的权限。

交接为接收 owner 保存按资源限定的维护/只读恢复委托及可查询身份映射，允许它在自己的可信任务/profile 上下文核验接受的原未决身份。后端解析受限映射取得原记录，不要求接收者取得旧 principal 全局权限；原操作 ID 不改，未交接的私有回执不开放。任何恢复读取都不得以新 owner 提交原交易或尾项。

### 10. 纳管、外部变化与周期维护复用执行证据

在 scope 管理权已取得后，先恢复可验证的原周期；无原历史时建立真实观察基线。非 Agent 创建、人工修改、本地暂缺周期不构成独立拒绝理由。基线不生成虚假入场单、成交、历史盈亏或 Agent 操作成功。

当前管理关联与创建来源/历史统计关联分开存储，解除现有 register_order 以 config_id 同时控制历史与管理的耦合。外部成交使用稳定身份去重，优先匹配原未知客户端订单身份，避免把已提交 Agent 订单再纳管一次。

复用用户成交流提示及有界 REST 轮询，保留真实观察时间、分页完整性和每订单/方向的相关限制。人工改单后模型省略的字段取最新值；人工撤单不恢复旧挂单。固定退出不自动按比例改变。

人工全平按已核验成交边界进入清理；清理只处理旧周期已关联单。重开建立 successor 周期，旧 all、晚到成交或无法证明的边界只限制相关动作，真实残仓可以管理。自动核验或纯查询不额外撤销人工单；既有已授权周期清理仍按已持久意图执行。

附带保护模式也使用相同管理基线及范围协调，保留其原有整仓 TP/SL 策略和数量规则；不能仅给独立模式隐藏 adopt 而让其他模式继续按创建者拒绝。

### 11. 现货管理库存与历史买入量分开

live spot 取得资产管理权后核验真实基础币余额及既有卖单，建立 managed baseline。后续真实成交、入出金和手续费更新管理数量与差异来源；可管理底仓不再以“未由本 MCP 买入”拒绝。

历史 verified_owned 继续描述已归属买入事实，新增 managed_quantity 与 claim_owner，不能将外部余额改写成 MCP 买入收益。提交卖出/退出受当前可用量、已知预留和目标规则限制；total/free/used 与本地预留重叠时按唯一订单身份只扣一次，OCO 两腿共用一份资产预留。不同报价交易对及多个 profile 不能重复分配同一基础币。

现货入场调用额度及 SPOT_DCA 多标的共同预算继续使用原账本，顺序执行不为每项重新发放整笔 allowance。无法核实相关余额、挂单或资产变动时说明具体缺项，不因无关资产或历史失败阻止目标撤单。

### 12. 配置、提示与界面一起迁移

任务和 MCP 页面以可见标的范围及自动管理为默认，移除默认流程的手填额外管理列表和先 adopt 指引；展示 owner、首次认领时间、状态和真实冲突。新增显式释放/交接入口，常规暂停与删除行为区分，不能在删除时偷偷丢弃管理资源。

策略版本关系明确记录：`visible_first_claim` 是新默认；旧 `exclusive_scope` 的有限清单及“升级不自动启用 exclusive_scope”仅描述旧显式策略，不作为新默认管理的前置条件。旧配置、基线和有效认领按原 owner 增量映射，默认交易流程切换到新的资格解析；旧 strict/adopt 参数及显式独占导入格式只在兼容解释中保留。仅已结束且没有有效认领或未决影响的历史周期不建立新管理权，避免升级扫描代替先到先得。

更新 Agent 工具说明、MCP server instructions、get_trading_context 和文档中的无条件“不能认领人工仓位/先 adopt”文字。管理者范围内来源只影响审计和当前事实；不支持的原生目标、权限不足和证据缺项继续准确说明。

目录 schema 采用共享动作定义，并按能力生成唯一说明。合同测试验证示例、字段和数量语义，避免只减少工具名称却留下一份巨大无类型参数字典。

## Risks / Trade-offs

- [目录数量下降不一定等比例减少 token] → 同时统计 schema/说明与典型回执大小；以重复定义和账户快照移除作为可验证目标，不承诺固定性能百分比。
- [可见 all profile 的范围动态增长] → 仅为实际资源或合法目标认领，分页轮询有界并保留完整性，管理竞争限定具体范围。
- [不同 API Key 属于同一物理账户] → 使用统一保存账户身份及旧 scope 别名映射；无法证明关系时明确配置要求，不能宣称 API Key 哈希能证明物理账户不同。
- [人工操作不受应用锁控制] → 提交前刷新目标、数量和模式，使用原生减仓能力，保存 ACK/部分成交/未知事实；不通过自动重复提交追求成功。
- [交接更换当前 owner 可能污染历史盈亏] → 当前管理关联独立于原创建者及历史成交归属，旧执行 actor 和 operation_id 不改写。
- [paper 批次中断后用户期待自动续跑] → 原请求重放只核验已尝试项，明确未执行尾部；界面和文档说明新意图需要新的稳定请求。
- [旧客户端仍缓存已隐藏工具] → 兼容执行名称继续可调用，重新 tools/list 得到新目录；提供逐项旧哈希、版本与权限回归测试。
- [未同步前置变更与主规范有冲突] → 本变更独立标注覆盖关系；先同步 add-exclusive-position-management 再同步本 delta，执行最终规范语义验证，禁止把两份默认策略同时当作要求。

## Migration Plan

1. 先核对前置独占管理实现与主规范差异，保留其资源/恢复能力；本变更实施不替代前置变更独立验收，主规范同步按依赖顺序进行。
2. 增量迁移配置版本、当前管理权、物理账户别名、管理关联及 v3/paper 父子账本；有效 owner 及当前活跃/未决证据优先导入，已结束纯历史不新建认领。新默认策略与旧显式/兼容策略按版本区分，重复迁移幂等，读权限和 paper/live 身份不扩张。
3. 添加统一 adapter 和 v3 执行，不更改旧请求 fingerprint；完成各引擎及崩溃/并发测试后再切换默认目录。
4. 切换内部 Agent 两工具目录及 MCP 8/11 名称目录，更新查询/提示、任务及 MCP 配置界面、QQBot 集成和文档；保留旧直接调用及事件 HTTP 路径。
5. 验收目录、数量/模式、原始回执、认领生命周期、人工操作、现货预留和 paper 事务；记录 schema/回执基准、离线界面截图与客户端目录刷新结果。
6. 回滚先停止新 v3/新自动认领意图，继续处理已开始请求和当前 owner 的对账。保留全部新账本及 scope，不能回退到无法识别 v3/管理基线的旧二进制直接执行交易；只有提供兼容读取并确认活跃资源已交接/退休后才切回旧目录。历史与未确认提交不得删除或释放来完成回滚。

## Open Questions

没有影响范围或验收标准的待定决策。依赖超时/轮询默认值及界面布局可在实现时沿用当前有界运行参数和既有组件；不会改变此处的目录、权限、认领、顺序执行或恢复契约。
