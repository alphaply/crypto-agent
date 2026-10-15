# QQBot Paper MCP

Paper 只使用 Binance 生产公开行情，支持现有引擎的 USDT 现货和线性永续。模拟的是资金、订单和成交；不会连接其他交易所或在 Binance 失败时跨场所回退。陌生名称须由当前 Binance 目录核验；空结果或来源失败不能证明标的全球不存在。

## 连接、身份与目录

后台 → MCP → QQBot 模拟交易，创建 `key_type=paper`、`scopes=["read","trade"]`、`profile_ids=[]` 的 Key。它管理自己的 `paper_namespace`；不得分发给 QQ 用户。不同 namespace 隔离，同 namespace 的 Key 轮换保留账户。

```json
{"mcpServers":{"crypto-agent-paper":{"url":"https://你的域名/mcp","headers":{"Authorization":"Bearer <Paper Key>"}}}}
```

可信 QQ 消息元数据提供字符串 `user_id`，不要从消息正文切换账户。Bot 参赛使用 `get_trading_context` 返回的 `bot_user_id`。Paper Key 不能选择真实执行引擎、live profile 或其他 namespace。

新连接重新 `tools/list` 后，Paper 可写目录为十项：`get_trading_context`、`search_symbols`、`get_market`、`get_news`、`get_account_state`、`get_trade_records`、`execute_trade_actions`、`paper_bank`、`configure_paper_account`、`get_paper_leaderboard`。只读 Key 保留七项。live 目录为 7/6 项，内置交易 Agent 只绑定 `execute_trade_actions`，只读角色零交易工具。

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

## 历史过滤与完整资金

历史非 BN、缺失/冲突来源、以及曾回填 Binance 但无原资源/周期证据的资源，从默认账户、订单、历史、事件和撮合集合逻辑过滤。原行、场所、数量、成本、订单状态、周期、回执和指纹保留；不会自动平仓、撤单、改价或释放保证金及预留。新 BN 同名同方向资源使用独立身份、存储 key 和周期，不合并旧库存、不由新成交追认旧来源。

`get_account_state` 按需读取 balances/positions/inventory/orders，始终携带 `filter`、`completeness` 和估值状态。现金、债务和可用现金保留完整账本含义。混合估值或收益证据缺项时权益、净权益及收益为 null，界面显示 —；不按假零值排名。借还款、初始资金和后台充值不成为交易盈利。

`get_trade_records(record_type=orders|order|fills|history)` 读取正常记录；默认列表仅显示可信 BN，精确 `order_id` 在原 namespace/user 权限内保留原订单事实。历史页按已检查原记录推进 `next_offset/next_cursor`，过滤空页仍可能 `has_more=true`。账户或回执超过完整读取预算时明确未处理，不用部分预留继续准入。

## 银行、维护与排行榜

`paper_bank(action=borrow|repay,amount,user_id,operation_id)` 的金额为 USDT。借款同时增加现金和债务、零利息；还款不超过债务与扣除全部预留后的可用现金。债务在爆仓后保留，后台调整的是账本现金。

Web 独立运行 Paper 维护，按持久账户/标的游标分批推进，再等待约 15 秒；实际间隔包含行情耗时。每轮账户访问、标的数分别不超过 `PAPER_SCAN_BATCH_SIZE`，并共享 `PAPER_MAINTENANCE_SECONDS`（默认 30 秒）的绝对截止时间；每次只规划一个标的，耗尽后下一轮继续持久游标。只处理可信 BN 资源，被排除仓位和挂单不进入报价/撮合。逐仓爆仓损失最多为原保证金，其他仓位和债务保留。公开网络读取位于金融事务外。

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

详情默认 base64 字节片段，length 为 1–65536；`encoding=text&length=2048` 每页至多 2048 个完整 Unicode 字符，避免中文拆字。两种编码的 `offset/next_offset` 都是字节游标，text 明确返回 `offset_unit=bytes`，客户端透传返回的游标。先用 PostgreSQL `octet_length` 检查大小，再在数据库侧通过 `convert_to(..., 'UTF8')` 和字节 `substring` 切片，应用只接收请求范围内的片段；text 页使用 UTF-8 增量解码。响应有 `version/next_offset/has_more/total_bytes`。同页版本与内容在一致快照下读取；续读必须携带 version，可变原账本变化返回 `audit_version_changed`，重新展开；不可变回执 revision 可继续读取。以上路径均受原权限保护、禁止缓存，不进行金融写入或交易所联网。

`PAPER_MAX_PAYLOAD_BYTES`（默认 262144）、`PAPER_SCAN_BATCH_SIZE`（64）、`PAPER_HISTORY_SCAN_LIMIT`（256）为有限校准值，非法值报错。先查大小再解析；不能完整核验资金时拒绝金融请求，原行仍可分块审计。后台保存扫描游标，重启继续；已结束回执不持续进入活跃订单解析集合。原资源来源核验有固定扫描 horizon，后来的新成交不会追认旧资源。

配置导入导出保留 Paper Key、namespace 和有效 BN 设置；运行账本随数据库卷持久化，不在配置导出中。人格和集成 skill 见 [人格 prompt](qqbot/人格prompt.txt)、[QQBot Paper skill](qqbot/qqbot-paper-trading/SKILL.md)。

管理入口：管理员在独立 **QQBot · Paper** tab 配置模拟账户和创建 Paper Key；MCP tab 不再内嵌 Paper 面板。权限、namespace 和保存接口不变。
