# QQBot Paper MCP

## 默认十一工具与顺序交易

新连接重新 tools/list 后只看到 `get_trading_context`、`search_symbols`、`get_market`、`get_news`、`get_account_state`、`get_trade_records`、`execute_trade_actions`、`get_operation`、`paper_bank`、`configure_paper_account`、`get_paper_leaderboard`。read-only Key 隐藏执行、银行及配置，保留八项读取能力。旧 paper_* 适配器仍可按原请求直接调用，下面旧示例属于兼容入口。

开始时调用 get_trading_context 读取规则和自己的 bot_user_id；需要用户开户时显式 `configure_paper_account(action="create", user_id, operation_id, name?)`，已有账户不重置。update 只修改后续默认 leverage 或 name/leaderboard_visible/notifications_enabled。user_id 必须来自可信 QQ 发送者或已返回的 Bot 身份；不要从用户消息正文切换账户。

交易统一使用 contract_version=3；一次父请求只属于一个 namespace/user。市场和 exchange 在动作中明确，执行引擎及 namespace 来自 Key，不能由参数选择真实交易。例：用 USDT 保证金市价开多后设置单腿止损：

```json
{"user_id":"123456789","operation_id":"qq-event-v3-0001","contract_version":3,"include":["account"],"actions":[{"action":"open","market_type":"swap","exchange":"binance","symbol":"BTC/USDT:USDT","side":"LONG","order_type":"market","margin_amount":100,"leverage":5,"reason":"用户开多"},{"action":"protect","market_type":"swap","exchange":"binance","symbol":"BTC/USDT:USDT","side":"LONG","close_all":true,"stop_loss":58000,"reason":"用户止损"}]}
```

amount 为基础币数量；quote_amount 为 USDT 名义金额（现货买入含手续费预算）；margin_amount 为合约入场保证金且手续费另计。入场三选一，退出选择 amount 或 close_all；现货仅 LONG 买卖，无杠杆。限价需要 price，只返回接受状态，不等于成交。保护允许单腿、成对及部分量；省略已有腿保留，OCO 一腿成交同时取消另一腿。close_all 只跟随当前 Paper 周期，不平掉后来重开的新周期。

每个父请求 1–20 项，默认失败/待核验/未知后停止。先 cancel 后 open 会先提交撤单并释放预留，下一项按新余额核验。independent 只继续符合实际条件的项；depends_on 仅引用已 completed 的此前索引，submitted 不满足依赖。整批非原子，单项账户、订单、预留、事件、子回执及父进度原子提交，公开网络报价在事务外读取。

同父 ID 相同意图只恢复原结果，改变顺序、策略、依赖或金额须为新的明确请求。崩溃或断线后 `get_operation(user_id, operation_id, detail=true)` 可读取完整父子证据；已提交子项不重复成交，未尝试尾部不自动续跑。默认回执精简，include=["account"] 只在末尾返回一次账户摘要。

账户按需读取 `get_account_state(user_id, include=["positions"])` 或 balances/inventory/orders；订单、单 ID、成交和事件历史分别用 get_trade_records 的 orders/order/fills/history。银行、排行榜和配置仍是独立能力。机器人事件轮询继续用 `/api/paper/events`，不属于对话模型目录，不由事件读取发送消息。

## 连接与旧客户端兼容

后台 → MCP → **QQBot 模拟交易 · Paper**，点击「创建 Paper Key」。默认命名空间 `qqbot`，权限 `read` + `trade`。复制该 key，使用已有 MCP HTTP 地址（网站根地址加 `/mcp`）：

```json
{
  "mcpServers": {
    "crypto-agent-paper": {
      "url": "https://你的域名/mcp",
      "headers": { "Authorization": "Bearer <后台创建的 Paper Key>" }
    }
  }
}
```

Paper key 无需绑定真实交易所账户，不能访问真实账户或真实下单工具。可读取公开行情以及 `get_news` 共享评分情报。实际交易全部写入独立模拟账本，持久化在现有数据卷的 SQLite 中。

## 按用户 ID 使用

QQBot 必须从可信消息事件取得发送者 ID，作为**字符串**传给所有账户工具；不要让模型从消息正文选择其他用户的 ID。此 key 是 bot 服务凭据，能够管理其命名空间内所有用户；不要分发给 QQ 用户。同命名空间的 keys 共享账户，轮换 key 不会重置资金；不同命名空间互相隔离。一个用户的现货和合约共享 USDT 现金。

旧客户端兼容调用顺序（新模型使用开头的 v3 入口）：

1. `get_paper_config` 查看默认资金、杠杆上限、借款上限和手续费。
2. `create_paper_account` 传 `user_id`、稳定 `operation_id`，可选 `name`。重复创建返回原账户，不充值或重置。
3. `get_paper_market` 查看 `symbol`、`market_type: spot|swap`、可选 `timeframe` / `limit`。
4. `paper_trade` 传 `user_id`、`market_type`、`symbol`、`action`、`amount`、`operation_id`，合约开仓可传 `leverage`。
5. `get_paper_account` / `get_paper_history` 查资金、负债、仓位及历史；`get_paper_operation` 查原回执。

```json
{"user_id":"123456789","name":"QQ 用户","operation_id":"qq-event-0001-create"}
```

现货买入示例（`paper_trade`）：

```json
{"user_id":"123456789","market_type":"spot","symbol":"BTC/USDT","action":"buy","amount":0.001,"operation_id":"qq-event-0002-buy"}
```

合约开多示例（`paper_trade`）：

```json
{"user_id":"123456789","market_type":"swap","symbol":"BTC/USDT:USDT","action":"open_long","amount":0.001,"leverage":10,"operation_id":"qq-event-0003-long"}
```

`amount` 始终是基础币数量（例：BTC 数量），不是 USDT 金额或张数。现货 `buy` / `sell`；合约 `open_long` / `open_short` / `close_long` / `close_short`，支持部分平仓。市价交易按真实公开 ask/bid 模拟撮合并计入手续费。支持 USDT 现货和线性永续，现已支持限价委托与 OCO 止盈止损；暂不模拟资金费。行情为交易所生产环境的真实公开数据，只有资金和成交账本为 Paper。

每个消息动作必须用独立且稳定的 `operation_id`，推荐基于 QQ 事件 ID 加动作序号生成（8–160 字符，字母数字和 `_.:-`）。成功操作重复请求返回首次价格与回执，行情断网也可重试；同 ID 不同请求报错。交易、借还款、账户创建与回执在同一个 SQLite 事务完成。请求超时先查 `get_paper_operation`，不要换 ID 盲重试。

## 杠杆、爆仓与银行

`set_paper_leverage(user_id, leverage, operation_id)` 修改后续合约开仓默认杠杆，上限由后台控制（最多 125×）。逐仓仓位的杠杆与保证金在该仓位周期保持固定；变更同一仓位杠杆需先平仓。

Web 服务启动独立模拟维护任务，不依赖真实策略 scheduler。每轮扫描各有仓位的标的并等待约 15 秒；请求行情的时间也计入实际检查间隔。每个标的有独立超时预算，行情失败保留上次估值，不假造报价。`get_paper_account` 是只读，返回最后观察价与 `oldest_mark_at`，应检查时间后再描述净资产。

当逐仓保证金加未实现盈亏不高于维护保证金（当前价格 × 数量 × 维护比例），仓位爆仓并写入历史。亏损最多为该仓保证金，剩余保证金扣手续费后归还现金；其他仓位、现货和自由现金不参与该仓爆仓。借款负债保留。

`paper_bank` 使用 `action: borrow|repay`、`amount`（USDT，最多 8 位小数）、`user_id` 和 `operation_id`。借款增加等额现金和债务，不增加净资产；零利息。还款不能超过可用现金或负债。爆仓后可以继续申请额度内的贷款。

后台可按命名空间和完整用户 ID 创建/检索账户，修改名称、可用现金、负债、借款上限、后续开仓杠杆、允许市场和启用状态。调整写入账户历史。初始资金与默认借款上限仅影响新账户。暂停账户禁止新交易和借款，仍可还款，后台仍会维护已有仓位。`realized_pnl` 记录退出净收益；开仓/买入手续费另外累计在 `fees` 并已从现金扣除。

## 后台 API

以下路径使用管理员 JWT，不接受 MCP key；响应禁止缓存。

- `PUT /api/mcp/paper/settings`：修改 `enabled`、`initial_balance`、`max_leverage`、`loan_limit`、`fee_rate`、`maintenance_margin_rate`、`exchange`（`binance` / `okx`）。
- `GET /api/mcp/paper/accounts?namespace=qqbot&user_id=123456789&limit=20&offset=0`：分页查询。
- `POST /api/mcp/paper/accounts/{namespace}/{user_id}`：创建账户。
- `PATCH /api/mcp/paper/accounts/{namespace}/{user_id}`：修改 `cash`、`debt`、`loan_limit`、`leverage`、`markets`、`enabled`、`name`。
- `POST /api/mcp/keys`：`{"name":"QQBot Paper","key_type":"paper","paper_namespace":"qqbot","scopes":["read","trade"],"profile_ids":[]}`。

现有完整配置导入导出保留 paper key 类型、命名空间及全局设置；账户、回执和历史属于运行数据，随数据库卷持久化，不在配置导出中。


## V2：真实行情、挂单、PK 与通知

`get_paper_market` 明确返回 `market_data_mode: live_public`、`market_data_is_simulated: false`、`source_environment: production`；不要把真实报价称为模拟价格。公开行情源支持 Binance、OKX、Bitget、Bybit，工具可显式传 `exchange`，持仓与委托固定记录行情场所。未指定交易所的标的搜索并行查询四个场所；只读查价在默认场所失败时可解析到已确认的其他场所，并明确返回实际来源。金融写入不会静默切换交易所。

陌生标的先使用 `search_paper_symbols(keyword, market_type?, exchange?)` 查询真实目录。MUB 与 MU、美光科技的映射来自 Binance 官方 bStocks 披露，只有目录确认的标的才可交易。部分查询失败会返回 `partial` / `missing_sources`；一个场所的空结果不能证明全球不存在。

- `get_paper_positions`：持仓与基础币数量适配视图。
- `paper_open_position`：明确 LONG/SHORT，数量、USDT 名义金额、USDT 保证金三选一，支持市价/限价及附加止盈止损。
- `paper_close_position`：部分/全部平仓，支持市价/限价；不反向开仓。
- `paper_place_order`：现货/合约限价委托或触发退出单。限价传 `price`，触发单传 `trigger_price`。
- `get_paper_orders`、`paper_cancel_order`、`paper_edit_order`：查看、撤销和修改委托；限价单预留现金、保证金或现货数量。
- `paper_set_protection`：设置/更新互斥 OCO 止盈止损，支持动态本周期全部或固定部分数量。平仓和爆仓会取消旧周期退出单。
- `get_paper_bot_account` / `ensure_paper_bot_account`：bot 独立账户，默认 ID `bot:xiaojin-mengzhizi`。每个 Paper Key 命名空间自动创建，升级启动时为旧 keys 补建；bot 使用普通 Paper 工具交易。
- `get_paper_leaderboard`：用户与 bot 排行、发件人 self/bot 比较。分数扣除负债和后台资金调整；隐藏其他用户私人账户字段。
- `set_paper_preferences`：公开昵称、排行榜可见性及通知偏好。
- `scan_paper_events(after_id, limit, user_id?)`：自动成交、爆仓与委托失败的分页事件。

### 定时扫描接口

`GET /api/paper/events?after_id=0&limit=100` 使用 Paper Bearer Key，不使用管理员 JWT。返回 `events`、`next_cursor`、`has_more`；事件包含接收者 `recipient_user_id` 与去重键 `delivery_key`。同样提供 `GET /api/paper/leaderboard`。

QQBot 的定时任务应每约15–30秒调用接口，按可信账号到 QQ 会话的路由将事件可靠入队或投递，再保存游标。失败不丢弃未入队事件，按 delivery_key 去重。扫描只读，不自动发送消息；用户关闭通知后，事件不会出现在通知流。bot 自己的事件由集成路由到配置的比赛频道，不把内部 bot ID 当成真实 QQ 号。

撮合是离散行情观察：限价单在真实观察的 ask/bid 满足价格后模拟成交，TP/SL 到触发条件后用观察到的买卖价成交。不会回放两次扫描间每一笔成交，不保证触发价成交，也不模拟全部盘口深度。新附加保护可能在后续扫描触发。

后台 MCP 页面新增持仓、挂单/止盈止损、排行榜、成交/爆仓事件视图。管理员数据接口为 `GET /api/mcp/paper/dashboard?namespace=qqbot&user_id=...`；MCP key 无权访问管理员页面。

完整可部署人格和 skill：
- [人格 prompt](qqbot/人格prompt.txt)
- [QQBot Paper skill](qqbot/qqbot-paper-trading/SKILL.md)

人格核心性格参考 [作品官方角色资料](https://fxkurumi-info.com/)；MUB 映射参考 [Binance 官方公告](https://www.binance.com/en/square/post/340313977073537)。粉爪银行和模拟比赛为同人追加设定。

腾讯 `TENCENT/USDT:USDT` 已经实际查询 Bitget 目录与报价验证；中文“腾讯”可匹配。若默认 Binance 永续请求因网络或地区可用性失败，结果保留 missing_sources，不解释为不存在。后续交易使用搜索返回的 exchange（例如 bitget）。
