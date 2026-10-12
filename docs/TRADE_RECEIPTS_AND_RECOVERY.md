# 独立交易回执与恢复

独立退出请求仍使用 `contract_version=2`。扩展回执使用 `receipt_version=1`，由
`backend/utils/trade_receipts.py` 的 `TradeReceipt` 和纯投影函数定义。

| 操作 status | 含义 | 下一步 |
| --- | --- | --- |
| completed | 本次动作已核实完成；不等于当前仓位为空 | 检查 position、cleanup；下一次交易重新准入 |
| submitted | 订单已接受，仍挂单或等待触发 | 不把挂单描述为成交 |
| pending | 本次动作已有进展，但缺少完成证据 | 查询原 operation_id |
| unknown | 请求可能到达，尚不能确认提交结果 | 按原 client ID 核验，不换 ID 重发 |
| failed | 本次请求明确未执行或被拒绝 | 依据阶段和原因修正新请求 |
| skipped | 已可靠确认无需执行 | 例如目标方向为空仓 |
| not_executed | 前序失败，本项未执行 | 恢复不会自动重放本项 |

顶层 `error` 属于本次动作；旧单清理错误在 `cleanup.error`。
`filled_amount`、`remaining_amount` 等无法核实时为 null；最后已知成交量和时间分别是
`last_known_filled_amount`、`last_known_fill_at`，不能拿它们冒充当前最终量。
原请求哈希不变；首次工具回执和模型原文保留，最新投影另存修订。

## 市价平仓被旧单清理阻塞

以下为合成示例，不是现场订单的结论：

```json
{
  "receipt_version": 1,
  "contract_version": 2,
  "operation_id": "example-close-1",
  "action": "close",
  "status": "completed",
  "order_id": "regular:example-close",
  "filled_amount": 0.5,
  "execution": {"phase": "verify", "submission_state": "submitted", "fill_status": "verified"},
  "position": {"side": "SHORT", "amount": 0, "status": "verified", "observed_at": 123},
  "cleanup": {"status": "pending", "orders": [{"order_id": "algo:example-stop", "reason_code": "fill_unverified"}]},
  "continuation": {"blocked_actions": ["open", "close", "edit"], "side": "SHORT", "advisory": true},
  "recovery": {"tool": "get_trade_operation", "target_operation_id": "example-close-1"}
}
```

1. Agent 读取本次成交结果和具体清理单，不把 cleanup.pending 解释为市价订单未知。
2. 调用 `get_trade_operation(target_operation_id)`；任务、账户和标的由运行环境注入。
   该工具只查询订单与仓位并保存观察，不 create/edit/cancel，也不恢复仓位归属。
3. REAL 维护任务继续处理已有清理意图，刷新原操作投影。撤销 ACK 缺成交量时最多补查
   3 次、总预算 2 秒；单次请求 timeout 限制为剩余预算。持续不明时保留退避时间和 blocker。
4. 相关订单、数量及周期状态核实后，Agent 可以显式提出新的交易。旧批次中
   not_executed 的动作始终保留未执行，不自动补发。

`snapshot.unresolved_orders` 包含终态但成交量待核验的入场/退出单；这些单不算有效保护。
已成交后晚到入场造成的残仓放在 `position`，不会把原 close 改成失败，也不会自动补平。
Hedge 的方向隔离需要当前证据支持；One-way 的共享净仓不能依赖另一方向旧回执放行。

## MCP 与持久化

MCP 使用已有 `get_operation(profile_id, operation_id)`。外层 principal/profile/operation ID
映射到内部 stable ID；不同调用方不能查询或重放对方的操作。普通交易查询与同 ID 重放
刷新同一份证据，read 权限不产生金融写入。

新增表 `trade_cycle_evidence`、`trade_operation_evidence` 和 `trade_receipt_revisions`
保存跨周期证据及原始/后续回执；MCP 外层有对应 links/revisions 表。迁移为添加式，
不更改原请求哈希，不清空周期。网络读取在事务外，短事务比较版本并拒绝倒退成交/终态。

回滚前暂停交易与维护进程，保留新增证据表和备份。不能混跑两版交易 worker，不能通过
清空未知回执、删除周期或更换 ID 恢复交易。旧二进制忽略新表，不删除证据。
恢复旧服务前必须确认它能解释当前周期记录；否则保持只读。离线演练覆盖备份、旧表读取、
重新升级与证据保留，不代表允许旧版继续管理新格式的在途订单。

## 缺失周期的诊断与恢复

`independent_position_cycle_missing` 保留兼容错误码，`details.reason_code` 区分
`no_history`、`account_scope_mismatch`、`competing_ownership`、`successor_cycle_exists`、
`incomplete_history`、`incomplete_ownership_evidence`、`history_read_unverified`、
`pending_order_race` 和 `quantity_mismatch`。可靠空仓返回 skipped。

原账户、任务、标的、方向及订单归属必须一致；逐单最终成交净额必须完整解释当前仓位。
明确标记历史不完整、存在后继周期、归属冲突或订单仍有竞态时不恢复。
已授权交易入口可修复同一个 episode_id，并原子保存审计；只读查询仅返回诊断。
交易所显示有仓位本身不足以证明归属，人工仓位仍使用已有显式接管流程。

## 横向验收矩阵

| 流程 / 故障 | 回执与恢复入口 | 限制 / 离线覆盖 |
| --- | --- | --- |
| 市价全平、部分平仓后清理失败 | completed + cleanup.pending；查询原操作 | 平仓只提交一次；残仓不自动补平；integration、receipt_recovery |
| open/加仓提交超时、落库失败 | unknown 保留 client ID；已知成交保留 completed | 不换 ID 重发；仓位未同步阻止后续写；integration、independent_exits |
| fixed/all 条件 TP/SL | 等待触发 submitted；母子身份与实际量分开 | 六种类型、LONG/SHORT、Hedge/One-way、数量与通道；real_v2、binance_v2 |
| 普通 LIMIT edit 拒绝/超时 | 明确拒绝 failed；超时 unknown；按原单查证 | 原单保留、最多一次 edit；receipt_recovery |
| 条件 cancel_replace | 旧单 canceled 不代表 edit completed | 替代成功才完成；耗尽/拒绝/超时及重启无重复替代；real_v2 |
| cancel ACK 缺成交、倒退或旧 open | pending；有界补查后保存 blocker | 母子总补查 ≤3 次 / 2 秒；后台遵守退避；主动查询可立即读取 |
| batch / legacy aliases | 已执行项刷新，尾部 not_executed | completed 清理告警后下一项重新准入；拒绝后停止尾部；batch、integration |
| Agent / Chat 多工具轮次 | 失败后只读 get_trade_operation 仍可运行 | 新交易保留审批、不自动执行旧尾部；decision_memory_context |
| MCP get_operation / 重放 | 外部 ID 关联内部 stable ID，回执同步修订 | principal/profile 隔离，read 零金融写入；mcp_trade_recovery、gateway |
| mock / 旧 all / 其他交易模式 | 共用扩展结构，legacy 保守解析 | mock 账本、旧 all 实际量、现货预算、附带保护均跑全量回归 |
| 缺失周期 | 只读诊断，授权路径恢复完整原周期 | 缺历史、数量矛盾、跨账户/任务或后继周期拒绝；receipt_recovery |

真实未知、旧 all 未核实、读接口持续失败和成交矛盾仍阻止依赖它们的写入。
后台退避只影响自动轮询；人工/模型只读查询可以提前查证，但不产生金融写入。
测试文件完整路径及桌面/移动证据见本次变更的 `verification.md`。

## 证据边界

本次回归使用 fake exchange 和临时数据库。截图对应线上调用未在本地日志中找到；本地
最新运行记录为 2026-09-07，无法确认截图那笔市价订单的现场成交状态。离线修复不表示
线上已部署或该笔仓位已核验。
