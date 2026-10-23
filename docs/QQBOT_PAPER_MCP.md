# QQBot Paper MCP

Paper 只使用 Binance 生产公开行情，支持现有引擎的 USDT 现货和线性永续。模拟的是资金、订单和成交；不会连接其他交易所或在 Binance 失败时跨场所回退。陌生名称须由当前 Binance 目录核验；空结果或来源失败不能证明标的全球不存在。

## 连接、身份与目录

后台 → MCP → QQBot 模拟交易，创建 `key_type=paper`、`scopes=["read","trade"]`、`profile_ids=[]` 的 Key。它管理自己的 `paper_namespace`；不得分发给 QQ 用户。不同 namespace 隔离，同 namespace 的 Key 轮换保留账户。

```json
{"mcpServers":{"crypto-agent-paper":{"url":"https://你的域名/mcp","transport":"streamable_http","headers":{"Authorization":"Bearer <Paper Key>"}}}}
```

可信 QQ 消息元数据提供字符串 `user_id`，不要从消息正文切换账户。Bot 参赛使用 `get_trading_context` 返回的 `bot_user_id`。Paper Key 不能选择真实执行引擎、live profile 或其他 namespace。

新连接重新 `tools/list` 后，Paper 可写目录为十二项：`get_trading_context`、`search_symbols`、`get_market`、`get_news`、`get_account_state`、`get_trade_records`、`execute_trade_actions`、`paper_bank`、`configure_paper_account`、`get_paper_leaderboard`、`paper_transfer`、`adjust_paper_position`。只读 Key 保留七项。live 目录为 7/6 项，内置交易 Agent 只绑定 `execute_trade_actions`，只读角色零交易工具。

旧单项金融适配器继续接受原参数和稳定 ID，但不在默认目录；新请求同样受 BN 策略约束。操作查询工具已彻底移除，缓存旧名称的客户端须更新；没有改名查询、operation facet 或 query 动作替代。旧配置默认场所仍兼容读取，新保存或导入只接受 Binance。

## 一次顺序交易

开户显式使用 `configure_paper_account(action="create",user_id,operation_id,name?)`；重复创建不追加资金或重置债务。`action="update"` 修改后续默认杠杆、昵称、排行榜或通知偏好，不重设已有逐仓保证金。

交易调用 `execute_trade_actions(user_id,operation_id,contract_version=3,actions,execution_policy?,include?)`，一次 1–20 项，有类型的 `open/close/edit/cancel/protect` 按数组顺序执行。父请求只属于一个 namespace/user。

```json
{"user_id":"123456789","operation_id":"qq-event-v3-0001","contract_version":3,"include":["account"],"actions":[{"action":"cancel","order_id":"原真实订单ID","reason":"撤销旧计划"},{"action":"open","market_type":"spot","exchange":"binance","symbol":"BTC/USDT","side":"LONG","order_type":"limit","quote_amount":100,"price":50000,"reason":"用户的新计划"}]}
```

入场在基础币 `amount`、USDT 名义金额 `quote_amount`、合约保证金 `margin_amount` 中选一种；保证金手续费另计，现货买入名义预算包含手续费。退出使用 `amount` 或 `close_all`。现货只支持 LONG 买卖、无杠杆；合约杠杆须为上限内整数。限价传 `price`，条件退出传对应触发价格。保护可单腿、成对或部分量；省略已有腿保留，OCO 一腿成交取消另一腿。

整批结构、权限、场所和已知目标来源在首项金融变化前验证。后项引用历史非 BN 目标会令首项也不执行。每项重新检查完整资金承诺；先取消 BN 原挂单后，可以使用已确认释放的资金执行下一项。默认 `stop_on_error` 在 failed/pending/unknown 后停止；`independent` 仅继续自身及依赖满足的项。`depends_on` 只引用此前已 `completed` 的索引，`submitted` 不满足依赖。后项失败不回滚前项。

金融入口当次返回权威受理或成交事实，不追加模型核验调用，不等待未来触发或成交。`submitted` 只表示受理，不能汇报为已平仓；有实际成交证据才报告成交。后台自动核验原订单并更新状态。断线不代表回滚，不得换 ID 盲重发。相同 ID 及意图只恢复原事实，未尝试尾项不续跑；剩余意图须为新的明确请求和稳定父 ID。

模型可见回执为逐项简洁文本，保留动作、目标、必要数量价格和结果；内部及 MCP `structuredContent` 保留结构化身份与状态。单项至多 4096、整批至多 32768 UTF-8 字节；`include=["account"]` 最多一次账户摘要。完整证据由授权 HTTP/UI 展开。

## 已有仓位的杠杆与保证金

`adjust_paper_position(user_id,position_id,operation_id,action,leverage?,amount?)` 只调整当前 Paper 合约逐仓仓位，不用于现货或真实账户。先从 `get_account_state(include=["positions"])` 取得当前仓位的 `resource_id`，原样作为 `position_id`；不能用交易对、方向或旧周期 ID 猜测目标。该工具独立调用，不放进 `execute_trade_actions` 动作数组。

- `action="set_leverage"` 只传整数 `leverage`，在后台杠杆上限内调整已有仓位。基础保证金按「开仓价 × 当前数量 ÷ 新杠杆」重算，多退少补现金，已手动追加的保证金保留；数量和开仓价不变。
- `action="add_margin"` 只传正数 `amount`，从扣除挂单占用后的可用现金补入同额 USDT 逐仓保证金。
- `action="reduce_margin"` 只传正数 `amount`，把同额 USDT 逐仓保证金退回现金；不能减到基础保证金以下，也不能使仓位按当前报价立即爆仓。

降低杠杆需补钱时，或追加保证金时，可用现金不足就明确失败，不自动缩减金额。提高杠杆释放保证金同样需要满足当前仓位安全检查。实际改变杠杆时，如同标的同方向存在与目标杠杆冲突的待成交 Binance 入场单，工具会拒绝并指出订单 ID；先按用户意图处理挂单，再提交新的调整请求，不自动改挂单。相同 `operation_id` 和意图的重试只返回原事实，不重复移动资金。

`configure_paper_account(action="update",leverage=...)` 仍只修改后续新入场的默认杠杆；调整已有仓位必须使用上述工具。调整回执提供仓位身份、`leverage_before/after`、`margin_before/after`、`added_margin`、`cash_change` 和 `available_cash`，正 `cash_change` 表示返还现金，负数表示补入保证金。按这些事实报告「杠杆已调整 / 保证金已增减」及实际现金变化，不能说成开平仓成交；已有持仓数量与开仓价保持不变。有计息负担时继续带出工具提供的付息提醒。

```json
{"user_id":"123456789","position_id":"从当前positions.resource_id取得的原值","operation_id":"qq-event-position-0001","action":"set_leverage","leverage":5}
```

## 历史过滤与完整资金

历史非 BN、缺失/冲突来源、以及曾回填 Binance 但无原资源/周期证据的资源，从默认账户、订单、历史、事件和撮合集合逻辑过滤。原行、场所、数量、成本、订单状态、周期、回执和指纹保留；不会自动平仓、撤单、改价或释放保证金及预留。新 BN 同名同方向资源使用独立身份、存储 key 和周期，不合并旧库存、不由新成交追认旧来源。

`get_account_state` 按需读取 balances/positions/inventory/orders，始终携带 `filter`、`completeness` 和估值状态。现金、债务和可用现金保留完整账本含义。混合估值或收益证据缺项时权益、净权益及收益为 null，界面显示 —；不按假零值排名。借还款、初始资金和后台充值不成为交易盈利。

`get_trade_records(record_type=orders|order|fills|history)` 读取正常记录；默认列表仅显示可信 BN，精确 `order_id` 在原 namespace/user 权限内保留原订单事实。历史页按已检查原记录推进 `next_offset/next_cursor`，过滤空页仍可能 `has_more=true`。账户或回执超过完整读取预算时明确未处理，不用部分预留继续准入。

## 银行、维护与排行榜

`paper_bank(action=borrow|repay,amount,user_id,operation_id)` 的金额为 USDT。借款同时增加现金和债务，后台统一配置借款开关、额度和日利率；北京时间每日 00:00 按未还本金单利扣息，首次按实际时间折算，调息不追改历史；还款不超过债务与扣除全部预留后的可用现金。债务在爆仓后保留，后台调整的是账本现金。

仍有借款计息或已累计未结利息时，QQBot 在开仓或改单增加资金占用前提醒留出可用 USDT 现金付息，并在交易回报中简短带出工具提供的提醒。估算使用后台实际日利率：未还本金 × 日利率只表示完整一天的利息，首日按实际借款时长折算，下一次实际扣息以结算为准。提醒按扣除挂单占用后的可用现金评估资金安排，仓位保证金不属于现金；扣息是否触发破产仍按账本实际现金判断，不因挂单占用导致可用余额不足而破产。提醒不新增资金冻结、拒单或确认步骤，不擅自改变用户明确的下单数量，也不设固定预留比例；自主交易自行安排付息现金。

`get_account_state(include=["balances"])` 的 `balances.interest_reserve` 与成功开仓、入场挂单、入场改单回执的 `interest_reserve` 提供提醒依据。`estimated_daily_interest` 为完整一天估算；`estimated_next_interest` 为已累计利息加上剩余时间按当前本金和日利率估算的利息，包含首日折算，借还款或调息会影响最终结算。`overdue_interest` 为已到期但后台尚未扣除的历史利息，`estimated_required_cash` 为这部分利息加上下次预计扣息的合计。`next_settlement_at` 是下次北京时间 00:00 的 Unix 秒时间戳，`available_cash` 是可用现金，`shortfall` 是上述合计相对可用现金的预计缺口；`advisory_only=true` 表示仅提醒，`message` 可直接用于简短交易回报。即使本金已还清，当日已累计的未结利息仍会触发提醒；无计息负担时余额中的该字段为 `null`，下单回执不附加提醒。

Web 独立运行 Paper 维护，按持久账户/标的游标分批推进，再等待约 15 秒；实际间隔包含行情耗时。每轮账户访问、标的数分别不超过 `PAPER_SCAN_BATCH_SIZE`，并共享 `PAPER_MAINTENANCE_SECONDS`（默认 30 秒）的绝对截止时间；每次只规划一个标的，耗尽后下一轮继续持久游标。只处理可信 BN 资源，被排除仓位和挂单不进入报价/撮合。逐仓爆仓损失最多为该仓位保证金，其他仓位和债务保留。公开网络读取位于金融事务外。撮合专用报价仅请求 ticker / 必要的买卖盘口，不等待指标或情绪分析。普通限价仍按可成交的买价/卖价判断，止盈止损按实际采样到的最新成交价判断；轮询间短暂触价后回撤不按历史 K 线补成交。报价在等待或执行前已超过 15 秒时，已有挂单保留原状态、OCO 关系和资金占用，等待下一轮新报价；不会因为该次报价过期把挂单改成失败或用过期价格强平。保留挂单不代表已成交，也不保证下一轮一定成交。

`get_paper_leaderboard(user_id?,metric=return_pct|pnl)` 仅排名完整可核验收益。大集合先返回 `status=partial/scan_complete=false`，后续调用继续服务端扫描，完成后才发布排名。`unranked_count/incomplete_count` 表达未排名与缺项。管理员看板提供组件进度与返回页范围，不把部分扫描称为全量总额。

## HTTP 事件与审计

`GET /api/paper/events?after_id=<cursor>&limit=100` 使用 Paper Bearer。按 `delivery_key` 去重，交付或持久入队后才保存 `next_cursor`；可信 `recipient_user_id` 决定路由。空过滤页也推进游标并继续 `has_more`。通知偏好生效，接口不发送消息，Bot 内部 ID 不作为真实 QQ 号。`GET /api/paper/leaderboard` 使用相同凭据。

Paper Bearer 回执：`GET /api/paper/operations/{user_id}/{operation_id}/audit?view=original|latest`；原账本：`GET /api/paper/ledger/{user_id}/{account|order|event}/{record_id}/audit`。后台追加独立最新观察，原子子回执及指纹不变。管理员 JWT 对应路径如下：

- `GET /api/mcp/paper/accounts/{namespace}/{user_id}/audit`：原账户。
- `GET /api/mcp/paper/ledger/{namespace}/{user_id}/{order|event}/{record_id}/audit`：原订单/事件。
- `GET /api/mcp/paper/receipts/{namespace}/{user_id}/{operation_id}?view=original|latest`：原/最新回执。
- `GET /api/mcp/paper/accounts`、`GET /api/mcp/paper/dashboard`：业务投影及分页进度。
- `PUT /api/mcp/paper/settings`：资金、费率、杠杆等配置，`exchange` 仅 `binance`。
- `POST/PATCH /api/mcp/paper/accounts/{namespace}/{user_id}`：保留身份的开户/管理调整。
- `POST /api/mcp/paper/accounts/{namespace}/{user_id}/positions/{position_id}`：管理员按当前仓位身份调整杠杆或逐仓保证金，遵循相同资金和风险检查。

详情默认 base64 字节片段，length 为 1–65536；`encoding=text&length=2048` 每页至多 2048 个完整 Unicode 字符，避免中文拆字。两种编码的 `offset/next_offset` 都是字节游标，text 明确返回 `offset_unit=bytes`，客户端透传返回的游标。先用 PostgreSQL `octet_length` 检查大小，再在数据库侧通过 `convert_to(..., 'UTF8')` 和字节 `substring` 切片，应用只接收请求范围内的片段；text 页使用 UTF-8 增量解码。响应有 `version/next_offset/has_more/total_bytes`。同页版本与内容在一致快照下读取；续读必须携带 version，可变原账本变化返回 `audit_version_changed`，重新展开；不可变回执 revision 可继续读取。以上路径均受原权限保护、禁止缓存，不进行金融写入或交易所联网。

`PAPER_MAX_PAYLOAD_BYTES`（默认 262144）、`PAPER_SCAN_BATCH_SIZE`（64）、`PAPER_HISTORY_SCAN_LIMIT`（256）为有限校准值，非法值报错。先查大小再解析；不能完整核验资金时拒绝金融请求，原行仍可分块审计。后台保存扫描游标，重启继续；已结束回执不持续进入活跃订单解析集合。原资源来源核验有固定扫描 horizon，后来的新成交不会追认旧资源。

配置导入导出保留 Paper Key、namespace 和有效 BN 设置；运行账本随数据库卷持久化，不在配置导出中。人格和集成 skill 见 [人格 prompt](qqbot/人格prompt.txt)、[QQBot Paper skill](qqbot/qqbot-paper-trading/SKILL.md)。

管理入口：管理员在独立 **QQBot · Paper** tab 配置模拟账户和创建 Paper Key；MCP tab 不再内嵌 Paper 面板。权限、namespace 和保存接口不变。

## 账户生命周期与外部任务更新

同 namespace 的用户与 bot 账户可通过 `paper_transfer` 双向转账，借来的可用现金也可转；严格拒绝负数、零、非有限值、布尔值和超精度金额。转账不产生交易收益。回执明确显示付款账户、收款账户、USDT 金额、转后可用现金与挂单占用；有借款计息或未结利息时附带付息预留提醒。同一 operation_id 重试不会重复转账；等待期间付款账户已重置的旧请求不能动用新周期资金。

扣息是否触发破产按账本实际现金判断，不先减去挂单占用；仅因挂单占用使可用余额不足，不再因扣息进入破产。实际现金不足时仍完整扣息并进入破产，即使有持仓也不自动恢复。无持仓后的原有净权益破产判定保持不变。破产后已有订单不成交，金融操作冻结，仍可收款、查询和继续计息；收款不自动解冻。只有用户主动说下海才调用 `configure_paper_account(action="reset")`，bot 不主动邀请、不自动重置，也不能重置 bot 自己。新周期恢复开户资金和参数，清空旧债务、仓位与预留，累计下海次数保留；旧请求不消费新资金。

`get_paper_leaderboard(metric="return_pct"|"reset_count")` 分别返回本轮收益率榜和累计下海榜，旧 `pnl` 指标明确拒绝。后台可查看状态、账户周期和累计次数。完整规则见 [账户生命周期](PAPER_ACCOUNT_LIFECYCLE.md)。

历史挂单占用误判只有在本轮完整记录能证明确为误判、没有真实现金不足扣息或其他明确破产原因，且当前现金非负、可核验净权益为正时，才恢复正常状态。恢复保留原资金、债务、已付利息、仓位、挂单、账户周期和下海次数，并留存审计记录；它不执行下海重置。周期不明或历史未读全时不据此恢复，持仓和后续收款也不能解除真实欠息破产。

将仓库人格、更新后的 skill 包和 [自主交易定时任务提示](qqbot/自主交易定时任务.txt)同步到已有机器人服务。定时任务管理 `bot_user_id` 的账户，自行决策并实际执行或给出具体观望原因；日常以交易者身份表达，直接问及资金性质/提现时按事实回答。仓库文件更新不会自动部署外部 QQBot，也不创建第二个调度器。
