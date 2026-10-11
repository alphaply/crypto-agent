# Design

## Context

动机与范围见 [proposal.md](proposal.md)。本次已经读取实际实现、相关测试和币安官方接口，以下是设计依据：

- `agent_tools.py` 已支持 `orders` 列表，`agent_graph.py` 同轮执行多个 tool calls；`trade_batch.py` 已提供最多 20 项的顺序批处理和遇到失败/未知停止的机制，不需要另建并行交易引擎。
- `IndependentExits` 和 `MockIndependentTrading` 已按方向聚合持仓，开仓不自动安装保护。当前仅有 market、普通 LIMIT（旧名 take_profit_limit）、条件 STOP_MARKET 三种退出。
- 当前 amount=0 立即折算为当时数量，入场改单 amount 是总量，退出改单 amount 是剩余量；后台减仓后按比例缩小同类退出单。模拟退出表 amount/remaining 为 NOT NULL。
- `PositionProtection` 已具备普通/条件单查询、actualOrderId 跟踪、原生入场改单与可恢复撤换的一部分机制；附带保护的自动冲突清理和失败平仓不适合复用到独立退出。
- REAL/STRATEGY 默认提示词仍要求“保护无法落实就等待”；MCP 有平铺 open/close/entry amend，但独立退出改单需走通用分发器。风险分类使用具体工具字符串，改名不能只改注册目录。
- CCXT 锁定版本 4.5.34 的 Binance 适配器已有 USDⓈ-M 条件单 Algo 路由，先复用并通过真实适配器的拦截请求测试核对，不预设需要升级依赖。

设计涉及契约、执行、数据库迁移、MCP、提示词与展示，满足创建 design.md 的跨模块和迁移复杂度条件。

## Goals / Non-Goals

**Goals:**

- 使自主分批交易建立在一个有版本的动作模型上，内置与外部入口不再各自解释方向、数量与价格。
- 将“交易计划”“交易所订单”“已确认成交”分开，尤其使动态 all、条件母子单和部分成交具备准确的状态表达。
- 让系统负责订单一致性和交易所能力约束；价格、退出档位及跨轮操作时机留给 Agent。
- 在不发真实交易的测试环境中证明升级兼容、权限与故障恢复。

**Non-Goals:**

- 不支持本轮范围外的现货、adopt 扩展、逐入场 lot 平仓、合成 OCO/依赖订单、自动切换持仓模式。
- 不增加普通/条件限价动态 all 的本地轮询模拟能力；交易所不支持就明确返回。
- 不改变附带保护模式及既有周期归零后撤同周期剩余入场单的策略。

## Decisions

### 1. 新规范入口与旧入口适配分离

独立模式默认使用以下工具；内置 Agent 的 config_id/symbol/operation_id 由运行时注入，MCP 的 profile_id/symbol/operation_id 仍为显式范围参数。

| 工具 | 新规范业务参数 |
| --- | --- |
| `open` | side、amount、price、reason；本轮仍为限价开仓/加仓 |
| `close` | side、order_type、amount 或 close_all=true、price?、trigger_price?、reason |
| `edit` | order_id、remaining_amount?、price?、trigger_price?、reason |
| `cancel` | order_id、reason |
| `execute_trade_actions` | contract_version=2、actions，每项为 action 与对应单项业务参数的平铺对象 |

新单项工具名确定 v2，不要求模型反复提供版本。批处理的既有名称保持不变，但新推荐 schema 明示 contract_version=2；无版本旧批次进入旧解析器，旧 `order` 嵌套与 amend_entry/amend_exit 只在旧契约内合法，不混用新旧形状。

在类型校验前选择版本与规范化入口，随后使用严格模型；未知字段、布尔数字、不符合类型的价格、空理由和互斥参数冲突均失败。新调用不使用 amount=0，不要求模型传原始 Binance side/reduceOnly/positionSide。保留旧注册名供兼容调用和历史恢复；模型的模式化推荐目录与示例只推荐当前规范。附带模式仍选择原工具，MCP 明确报告新工具仅适用于独立合约配置。

旧工具保持入参和回执兼容，通过适配层转发统一服务：旧 take_profit_limit -> 新 limit；旧 entry_price -> 新 price；旧 pos_side -> 新 side；旧入场总量 amount 按已成交量转换；旧 amount=0 首次执行时生成固定快照并持久化，重放不重新读取当前量。旧 execute_trade 继续验证原工具白名单，并不能穿透模式、额度、权限或归属限制。

**取舍**：直接覆盖旧枚举最简单，但会把仍挂着的普通单错误送入条件接口；全面删除旧工具会破坏 MCP 客户端和重放，因此采用显式适配。

### 2. 以 fixed/all 数量模式区分计划和实际执行

规范退出记录保存 contract_version=2、order_type、quantity_mode=fixed|all、requested_amount（fixed 时为基础币量）、price、trigger_price、trigger_basis、operation_id、episode_id、side。计划字段与 filled_amount、actual_remaining、resolved_amount、coverage_estimate 分开；all 触发前的 requested_amount/actual_remaining 为 null，不用 0 伪造固定数量。

- 市价 all：发送前重新核验仓位，固化该次 resolved_amount；操作仍记录 all 意图，不保证之后发生的入场也被平掉。
- 条件市价 all：使用交易所原生 closePosition，在触发时由交易所处理该方向剩余仓位；创建时的覆盖估计仅用于展示，加仓不会让其退化为旧快照。
- 其余 all：在写入前拒绝，不切换类型、不偷偷固化数量、不新增后台合成交易策略。

原生 all 是账户整方向语义，前提是当前任务独占管理该方向；它不能隔离本地虚拟份额。创建前和维护时沿用账户归属核验。用户或其他系统之后直接往同方向增仓可能进入原生 all 覆盖范围，接口说明明确这个交易所事实；已知归属不明时拒绝新建，不声称可精确分离外部仓位。

**取舍**：将 all 一律解析成快照容易复用旧代码，但不满足末档触发时全平；同时支持合成动态限价会显著增加后台策略和竞态，留在范围外。

### 3. 固定档位由 Agent 管理，合计数量只观察不自动分配

停止产生新的后台比例缩量和冲突自动撤换。新建/显式修改 fixed 仍要求单笔剩余目标不超过当前仓位；取消当前“同一 exit_type 挂单总量不得超过仓位”的额外本地排他预留规则，允许替代条件重叠并交由交易所真实减仓约束裁决。

观察统计分为普通限价退出、条件 TP、条件 SL 三组，各组汇总固定剩余量并标记 quantity_conflict；all 单独列出，不参与固定预留总和。SL 覆盖为已核验有效订单的覆盖估计，上限为当前仓位；stop_limit 标明是触发限价，并不保证成交。数量冲突不写成 plan.error 或 unknown，否则会错误阻止 Agent 自己 edit/cancel。

这个选择将用户“前几笔指定数量、最后全出、Agent 自由一些”的方向具体化为可评审方案；它是独立模式的行为变更，应在升级说明中显式列出。既有周期的固定单也停止新增自动比例调整，但升级前已经开始的 replacement 继续按其持久化意图完成核验，不能遗弃不明写入。

**取舍**：保留合计排他预留或自动比例缩量都能简化覆盖数字，但会改写/限制 Agent 的条件选择；本方案保留真实订单状态和冲突提示，避免伪造自动保护。

### 4. 集中定义订单类型与交易所能力

集中维护订单类型的通道、必填字段、触发语义、覆盖类别与 all 能力，替代各处 `exit_type == stop_market` 判断。

| 规范类型 | Binance 类型 | 通道 | 字段 | all |
| --- | --- | --- | --- | --- |
| market | MARKET | regular | amount 或解析后的仓位量 | 提交前快照 |
| limit | LIMIT | regular | amount、price | 不支持 |
| stop_market | STOP_MARKET | algo | trigger_price、amount 或原生全平 | 原生 |
| stop_limit | STOP | algo | amount、trigger_price、price | 不支持 |
| take_profit_market | TAKE_PROFIT_MARKET | algo | trigger_price、amount 或原生全平 | 原生 |
| take_profit_limit | TAKE_PROFIT | algo | amount、trigger_price、price | 不支持 |

固定量 One-way 退出使用 reduceOnly 与 BOTH；Hedge 使用目标 positionSide 和反向买卖动作，不发送 reduceOnly。原生 all 不带 quantity/reduceOnly。普通 limit 不再沿用“价格必须在盈利侧”的旧校验；条件价按 TP/SL、LONG/SHORT 和已有 trigger_basis 校验，限价与触发价不互相猜测。

新独立周期沿用当前默认 CONTRACT_PRICE（最新成交价），本轮不新增触发基准工具参数；旧记录若已明确保存其他基准则保留。创建回执和展示注明实际基准，不能用最新价核验一张以 MARK_PRICE 触发的历史单。

普通下单/改单使用 `/fapi/v1/order`，条件下单/查询/撤单使用 `/fapi/v1/algoOrder` 对应能力；在 CCXT 层提供明确类型与 trigger 查询标记。其他已有交易所保留已支持能力，新组合按适配器能力表决定，无法保证相同语义时提前返回 unsupported_combination。

已核对的官方依据（2026-10-11）：[条件单迁移与未触发改单限制](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/change-log#2025-11-06)、[普通/条件下单参数与全平限制](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/trade)、[原生 LIMIT 修改](https://developers.binance.com/docs/derivatives/usds-margined-futures/trade/rest-api/Modify-Order)。实现测试截获真实 CCXT 适配器发出的请求，不需要实际交易验证。

**取舍**：只在创建处分支不足以支持新类型，查询、撤销、回执恢复与账本都需要相同分类，否则条件 TP 会被当成普通订单。

### 5. 订单身份与 edit 以真实执行为准

新回执的 order_id 是带通道的规范引用（例如 regular:123、algo:456；模拟使用对应本地引用），调用方原样回传，不自行拼装。持久化分别保存 exchange_order_id、exchange_channel、client_id、parent_algo_id、execution_order_id 和 replacement 链。旧数字 ID 只在归属范围内唯一时接受，否则返回明确歧义；账本同时登记条件母单与实际执行身份，成交去重按真实执行证据处理。

普通 LIMIT 原生 edit 的剩余目标转换为 `filled_at_verification + desired_remaining` 的总量；成交在途时不反复追量，最终返回核验到的真实剩余量。退出原生改单保留原减仓方向与语义；若无法保证，则在预检阶段报告能力限制，不在结果未知后切换撤建路径。

未触发条件单 edit 使用持久化撤建意图。旧单取消后再核验新成交和当前仓位，原剩余目标扣掉期间新增成交；不再按其他同组单预留量偷偷缩小替代单。若目标仍超过当前仓位，返回明确数量冲突供 Agent 调整，不自动选择新数量。all 只允许改价格，替代单保持 all。

若母单已经触发，查询其实际子单：不得因“母单已不在挂单列表”而重建第二张条件单。对于可继续操作的限价子单返回新的 regular 引用；Agent 以该身份显式 edit/cancel。未完成的核验返回 pending/unknown，不将母单撤销等同于子单也撤销。

对 edit 使用已触发母单身份，返回 order_already_triggered 及子单引用，等待 Agent 显式修改子单，不把原 trigger_price 意图转换成子单 price。对 cancel，已授权意图是取消这笔退出的未成交部分，因此在关联与归属核验后可继续撤其活跃子单；仅在所有目标挂单均终止后报告完成，并保留期间发生的真实成交。普通单/已触发子单不接受 trigger_price；市价执行单不支持 edit。

**取舍**：统一 edit 是调用层体验，不能承诺所有单型原地改单、ID 恒定或无空窗。原生 LIMIT 与条件撤建分别使用可验证路径。

### 6. 幂等、权限与批处理沿用统一执行边界

操作身份在任何交易写入前持久化。新请求保存契约版本、规范动作与原始入参摘要；重放首先查已有版本与原请求，不能升级重算旧摘要再下单。历史同 ID 原请求返回原结果；跨别名同 ID 不能创建第二个写入，若无法证明意图相同则返回 identity_conflict。

把权限/额度分类从工具字符串判断集中到规范动作。open 和入场 edit 仍执行增仓校验及已有杠杆/额度检查；edit 的目标类型必须从归属记录解析，不能依调用方声称是退出而绕过限制。退出 edit 为 trade，cancel 为 cancel；混合批次需要两者。旧入口与新入口经过相同分类。

edit 的 trade 授权包含对同一归属目标必要的取消并替换，不额外索要 cancel scope；它不能取消其他冲突单。显式 cancel 动作仍单独检查 cancel 权限。这个边界保持原退出改单能力，同时避免适配器实现方式改变外部权限需求。

新批次先校验所有结构、字段、模式和已知方向冲突，再顺序执行；每个动作实际提交前再次核验动态仓位与状态。One-way 混合方向批次整体拒绝，分开的 open 也检查已有对向入场挂单。执行继续使用账户范围的串行一致性边界，不将“同轮多次”实现为无约束并发。

操作 status 保持 submitted/completed/pending/unknown/failed/not_executed；order_status 单独表示 waiting_trigger、open、partially_filled、filled、canceled、rejected 等实际状态。回执附数量模式、实际成交、当前身份、必要的旧身份与 edit_method；completed 表示操作完成，不能单独作为成交证明。

**取舍**：单独再做一套万能 executor 会造成权限分叉。保留 execute_trade_actions 组合现有动作，execute_trade 只作旧分发适配。

### 7. 实盘和模拟使用相同的数量、触发与周期模型

真实计划 JSON 记录版本、数量模式及母子身份，读取旧记录时规范化到只读视图，保存时追加版本而不改变历史数量/单型。mock_exit_orders 增加同等字段；使用事务迁移使计划 amount/remaining 可空，并把所有旧记录标为 fixed，保留 ID、episode、历史成交、操作结果与索引。

模拟条件限价需区分 waiting_trigger 和触发后 open：触发不是成交，触发后仍按限价规则撮合；触发时间防止用早于触发的行情回填。保留现有同根行情的确定性排序，并在测试和文档中记录其 SL 优先等约定，不声称掌握 K 线内真实时间顺序。

模拟 fixed 实际成交量上限为执行时仓位，多余未成交部分使用独立字段和终态表示；all 触发时读取当时余仓。PnL 仅计算真实模拟成交，不使用原计划量。将 `_resize()` 中的归零清理与比例缩量拆开，删除后者的自动行为但保留终止周期清理。

实盘等待触发的 all 母单可没有量和 filled，此时依据可信 algoStatus 表示“尚未执行”，不把缺字段写成 0 成交，也不让整个周期无故 pending。触发后的成交跟踪子单；仓位短暂空读、退出与入场并发、撤单中的迟到成交继续沿用已有多次核验规则。停止自动比例调整不改变周期确认归零后的同周期入场/退出清理。

**取舍**：仅删除实盘缩量循环会导致模拟与实盘不一致，还会遗失模拟周期结束清理；需要以同一验收场景贯穿两种执行。

### 8. 提示词、展示与说明按退出模式派生

独立模式使用专门的决策规则：允许跨轮设置退出，不要求当前轮次必须补齐 TP/SL；仍基于已成交事实行动。附带模式保留原保护要求。更新默认模板和运行时模式上下文，不批量覆盖用户自定义提示词；自定义模板仍有强制保护句时在文档给出迁移说明，模式事实由系统上下文提供。

工具说明统一为“用途、参数条件、方向/数量单位、短例子、回执”。示例覆盖同方向两次 open、Hedge 两向 open、两档定量加 all 的 TP 和 SL、edit、cancel。Agent 上下文、MCP、stats_service、前端卡片/图表统一展示 all 标签、限价与触发价、实际状态、覆盖估计和数量冲突，不再用 stop_market 之外全部当 TP 的二分显示。

## Risks / Trade-offs

- [固定挂单合计超过余仓，交易所可能拒绝/取消某笔] → 展示真实状态与数量冲突，由 Agent 决定，禁止反向开仓；不保证所有替代条件同时有效。
- [条件 all 覆盖账户整方向及触发前新增仓位] → 仅在归属可核验时创建，明确语义，不承诺隔离外部后来混入的份额。
- [条件 edit 存在撤旧到建新的空窗] → 保存撤建意图与身份，准确返回阶段，未知不重发；不声称原子更新。
- [同时触发、跳空或入场迟到成交] → 以实际成交核算，保留残仓和清理状态，不假定“末档必定最后成交”。
- [新状态导致旧代码误读] → 先完成版本读取和迁移，再启用写入；旧工具/记录契约测试覆盖历史含义。
- [模拟 K 线无法还原盘中路径] → 采用确定性规则并明确限制，测试不把模拟排序当成交易所保证。

## Migration Plan

1. 先实现新旧契约读取、规范化、订单身份、数据库可空量与版本迁移；以隔离数据库副本验证旧挂单、历史回执及 pending replacement 恢复，迁移本身不发交易请求。
2. 接入统一权限和幂等处理，再启用新执行与模拟路径；保留旧工具适配和旧请求摘要，禁止对旧回执重新提交。
3. 启用独立模式新目录与提示词，发布工具和行为变化说明：停止自动比例重分配、合计数量仅作观察、all 能力范围、周期清理仍保持原逻辑。
4. 验证通过后才考虑环境升级；本次提案阶段不修改配置、不切换任务退出模式、不发订单。实现阶段默认测试同样使用隔离 DB 和拦截式交易所。
5. 回滚不能直接让旧版本解释活跃 v2 条件单/all。先停止新 v2 写入并保留兼容版本的查单、撤单和对账；待相关周期与不明操作处理完毕，再降级。不得用旧数据库备份覆盖升级后的真实成交记录。
