# exclusive-position-management Specification

## Purpose

使已获得独占范围授权的交易管理者持续接管并管理交易所实际仓位、挂单和外部成交，在启动前已有仓位或运行期间发生人工干预时仍可接续管理，保持来源可追溯、动作权限和跨进程一致性。

## Requirements

本规范保留旧版 `exclusive_scope` 显式配置的兼容契约；其中“升级不自动开启”指该显式策略。当前默认 `visible_first_claim`、已有 owner 的迁移及持续管理权由 [visible-position-management](../visible-position-management/spec.md) 定义。

### Requirement: Exclusive management is an explicit persistent configuration
系统 SHALL 允许为 REAL 合约独立退出任务或 MCP 合约配置显式启用独占管理，指定账户、规范交易对、方向和唯一管理者。授权 SHALL 覆盖范围内已有及后续仓位、普通/条件订单和外部成交。升级 SHALL NOT 自动开启；查询授权 SHALL NOT 等同于管理授权。Hedge 模式可按方向隔离；One-way 模式 SHALL 对共享净仓建立互斥范围。

#### Scenario: Enable management with an existing position
- **WHEN** 管理员为指定任务开启独占管理，交易所已经有同范围仓位和挂单
- **THEN** 系统接受配置并启动核验，不要求先平仓或清空挂单；核验完成后管理者可管理该范围

#### Scenario: Competing owners share a range
- **WHEN** 第二个任务或 MCP 配置申请与已分配范围重叠的独占管理
- **THEN** 配置原子拒绝并说明冲突，不能因进程不同或某任务决策暂停而同时生效

#### Scenario: One-way directions overlap
- **WHEN** 同账户交易对使用 One-way 模式，两个管理者分别请求 LONG 和 SHORT
- **THEN** 系统将其作为共享净仓冲突处理，不以方向标签绕过互斥

### Requirement: Verified startup exposure is enrolled without fabricated trades
已授权管理者 SHALL 在启动或首次管理请求时核验当前仓位、普通及条件订单与接管边界。可恢复的原周期 SHALL 优先恢复；无可恢复原周期的已有仓位 SHALL 记录真实观察基线后纳管。接管 SHALL NOT 伪造入场订单、成交或历史收益，也 SHALL NOT 因存在可核验的外部挂单一概失败。

#### Scenario: Existing manual position and protective orders
- **WHEN** 启动前已有仓位及同范围止盈止损单，独占授权有效且事实可核验
- **THEN** 系统纳管仓位与支持的订单，保留原身份和参数，Agent 可在原请求内继续管理

#### Scenario: Original task evidence explains residual exposure
- **WHEN** 原周期缺失但同任务历史证据完整解释当前残仓
- **THEN** 系统恢复原周期和关联，不额外建立一份重叠接管基线

### Requirement: External fills update managed exposure continuously
系统 SHALL 持续纳入独占范围内可核验的外部加仓、减仓、全平及其他仓位变化，按稳定成交身份去重并保持数量连续。外部成交 SHALL 保留真实来源与时间，不能仅因不由 Agent 提交而拒绝有效管理。当前数量相同 SHALL NOT 证明期间无成交；缺失或不完整证据 SHALL 限制依赖它的具体动作。

#### Scenario: Human increases or decreases position
- **WHEN** 已管理仓位发生外部加仓或减仓且成交及当前仓位可核验
- **THEN** 系统更新管理数量和覆盖事实，允许满足条件的保护、减仓、改单及撤单操作

#### Scenario: Increase and decrease net to zero
- **WHEN** 外部先加仓再减仓使最终数量不变
- **THEN** 两笔真实成交仍被保留和去重，不能误报为没有变化或永久归属冲突

#### Scenario: Fill history has a gap
- **WHEN** 成交分页失败或变化不能与当前仓位核对
- **THEN** 系统保留已知事实、缺项和重试进度，不推进完整性检查点，不伪造一笔成交补平差额

### Requirement: External orders become manageable after verified enrollment
范围内外部新增订单 SHALL 在核验身份、方向、入场/退出作用及交易所能力后纳入管理，允许有权限的调用者使用既有 edit/cancel。普通订单、条件母单和执行子单 SHALL 保持独立身份及关联。新增订单若与待核验提交可能为同一笔 SHALL 先恢复关联，不能重复登记。系统 SHALL NOT 猜测未知语义。

#### Scenario: Human adds a limit entry
- **WHEN** 外部新增未成交限价入场单且身份、作用与范围已核验
- **THEN** 管理者可读取、修改或撤销该单，部分及后续成交更新同一管理关系

#### Scenario: Human changes or cancels an existing order
- **WHEN** 交易所现有管理订单的价格、数量或状态被外部修改
- **THEN** 系统刷新该订单事实，不自动恢复旧参数；下一次管理使用最新已核验状态

#### Scenario: Conditional execution identity changes
- **WHEN** 外部条件母单触发出执行子单
- **THEN** 系统关联当前执行身份和成交，edit/cancel 按当前有效目标核验，不混用相同数字的普通订单 ID

#### Scenario: Unsupported or ambiguous external order
- **WHEN** 订单方向或作用无法核验，或适配器不支持对应修改
- **THEN** 返回具体订单的可读状态、缺项及支持能力，只限制受影响动作，不把整个范围永久锁死

### Requirement: Enrollment preserves strategy intent and execution permissions
自动发现、对账和本地纳管 SHALL NOT 自行开仓、撤销外部订单或重配固定退出档位。后台 SHALL 仅执行已授权的周期清理及已持久化维护意图；策略性调整由 Agent 请求。MCP trade/cancel 权限及绑定聊天审批 SHALL 保持，read 工具 SHALL NOT 提交、修改或取消交易所订单。

#### Scenario: Human adds a stop order
- **WHEN** 系统发现并纳管同范围新增止损单
- **THEN** 保留其参数和当前保护事实，供 Agent 决定是否调整，不因纳管自动撤销

#### Scenario: Read-only client sees an external order
- **WHEN** 仅有 read 权限的调用者查询已纳管订单
- **THEN** 返回允许查看的事实，不能通过查询触发交易所写入或取得 trade/cancel 权限

### Requirement: Human and system concurrency uses current execution facts
管理工具 SHALL 在提交前重新核验目标订单及动作依赖的仓位，对内部并发使用跨进程互斥和版本校验。人工操作造成变化时 SHALL 在有界预算内刷新并继续可执行请求，或返回目标级变化原因。减仓 SHALL NOT 增加或反转仓位；不明确的写入 SHALL 保留原操作身份并只核验，不盲目重复提交。

#### Scenario: Human reduces position before a close submission
- **WHEN** Agent 请求固定数量减仓，提交前最新仓位已被人工减小至不足该数量
- **THEN** 按固定请求语义返回明确数量冲突；可核验的 close_all 使用提交时剩余量，不能静默改写固定数量或反向开仓

#### Scenario: Two processes attempt the same enrollment
- **WHEN** Web 和独立调度器同时发现同一外部订单及成交
- **THEN** 仅建立一个有效管理关系，旧证据不能覆盖新证据，不重复交易写入

### Requirement: Management authority survives pauses and has explicit retirement
决策暂停 SHALL NOT 解除独占范围或停止已授权的周期维护。撤销独占管理、改变账户凭证/市场/范围或转移管理者 SHALL 明确处理活跃仓位、订单和待核验写入；未完成交接 SHALL NOT 静默释放范围。归属冲突和范围外资源 SHALL NOT 被自动抢占。

#### Scenario: Pause decisions while orders remain active
- **WHEN** 指定管理者停止定时决策但有活跃仓位和订单
- **THEN** 原管理范围继续有效，后台对账与已授权维护继续，不让第二管理者接管

#### Scenario: Remove an active management scope
- **WHEN** 用户删除或缩小仍有活跃资源或未知写入的管理范围
- **THEN** 系统拒绝静默变更并提供具体待处理资源，保留原归属和事实

#### Scenario: Resource already belongs to another manager
- **WHEN** 范围内发现明确属于其他有效管理者的订单或周期
- **THEN** 返回真实竞争归属，不将它标成可自动接管的外部资源
