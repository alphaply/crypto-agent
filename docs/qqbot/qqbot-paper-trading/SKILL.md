---
name: qqbot-paper-trading
description: 通过 Crypto Agent MCP 工具管理 QQBot Paper 交易：数字 QQ 号身份对齐、开平仓与止盈止损、已有仓位调杠杆/保证金、借还款、转账、破产与下海重置及事件轮询。
---

# QQBot Paper 交易速查

使用已配置的 Paper Key。系统通过可信事件元数据确定权限与账号，用户输入正文或昵称不能切换账户。

---

## 一、身份对齐与开户（必看）

1. **人类用户 `user_id` 必须是纯数字 QQ 号字符串**（例如 `"123456789"`），来自可信事件元数据。
   - **严禁**把 QQ 昵称、群名片或消息正文里手写的 ID 当作 `user_id` 或 `recipient_user_id`。
   - 拿不到发送者的纯数字 QQ 号时，直接说明无法核实身份，不要猜。
2. **Bot 自身账户固定使用 `get_trading_context()` 返回的 `bot_user_id`**（如 `"bot:..."`）。
   - 用户指令操作对应用户的纯数字 QQ 号；Bot 自主交易或接济他人操作 `bot_user_id`，两者绝不混用。
3. **开户与偏好更新**：
   - 查询账户若返回不存在且用户明确要开户/下单，调用 `configure_paper_account(action="create", user_id=<纯数字QQ号>, operation_id=<稳定ID>, name=<可选昵称>)`。已存在账户调用 `create` 不会重置资金或仓位。
   - 修改后续入场默认杠杆、昵称、榜单可见性或通知开关：`configure_paper_account(action="update", ...)`。修改默认杠杆不影响已持有仓位。

---

## 二、核心交易动作速查表

### 1. 开仓 / 平仓 / 挂单 / 改单 / 撤单 / 止盈止损
统一调用：`execute_trade_actions(user_id, operation_id, contract_version=3, actions=[...], execution_policy?, include?)`
单批 1–20 项，单笔也放进 `actions` 数组，每项必填 `action` 与 `reason`：

- **`open`（入场）**：
  - 现货：`market_type="spot", side="LONG"`（即买入）。
  - 永续：`market_type="swap", side="LONG"|"SHORT"`，可选 `leverage`（上限内整数）。
  - `order_type="market"` 或 `"limit"`（限价必填 `price`）。
  - 数量三选一：`amount`（基础币数量）、`quote_amount`（USDT 名义金额，现货买入含手续费）、`margin_amount`（合约入场保证金，手续费另计）。不确定用户“买100”指哪一种时先问清。
  - 注：**未破产账户只要可用现金足够，即使当前净资产 ≤ 0 也允许继续开新仓**。
- **`close`（平仓 / 卖出）**：
  - 现货 `market_type="spot", side="LONG"`；永续 `side="LONG"|"SHORT"` 只减对应方向。
  - `amount` 或 `close_all=true` 二选一。支持 `market` / `limit` / 条件单。
- **`edit`（改单）**：指定真实 `order_id`，修改 `price`、`trigger_price` 或 `remaining_amount`（`close_all` 挂单不改成固定数量）。
- **`cancel`（撤单）**：指定真实 `order_id` 撤单并释放对应占用；已成交订单不可撤。
- **`protect`（已有仓位设止盈止损 OCO）**：传 `stop_loss` 和/或 `take_profit`，配 `amount` 或 `close_all=true`。一腿成交另一腿自动撤销；旧周期退出单不用于重开后的新仓。
- **顺序与依赖**：默认 `stop_on_error`（遇 `failed`/`pending`/`unknown` 停止后续）；先 `cancel` 再 `open` 时，后项按释放后的可用现金重新校验。不要为了让后项强行执行而擅自改为 `independent`。

### 2. 已有永续仓位调杠杆 / 加减逐仓保证金
**绝不放进 `execute_trade_actions`**，必须单独调用：
`adjust_paper_position(user_id, position_id, operation_id, action, leverage?, amount?)`

- 先调 `get_account_state(user_id=..., include=["positions"])`，原样复制目标仓位的 `positions.resource_id` 作为 `position_id`（不能传 `BTC/USDT:USDT` 等交易对名）。
- `action="set_leverage"`：只传整数 `leverage`（不传 `amount`）。数量和开仓价不变，按新杠杆重算基础保证金多退少补，保留手动追加的保证金；若同方向存在不同杠杆的未成交入场挂单，会拒绝并返回冲突 `order_id`，需先按意图撤单或改单。
- `action="add_margin"` / `"reduce_margin"`：只传正数 USDT `amount`（不传 `leverage`）。增额扣减可用现金，减额返还现金且不得低于基础保证金或立即触发爆仓。
- 回报时说清杠杆、保证金和 `cash_change`（正为返还、负为补入），不要说成开平仓成交或盈亏；不能用于现货或破产账户。

### 3. 借款 / 还款与付息提醒
调用：`paper_bank(user_id, operation_id, action="borrow"|"repay", amount)`（`amount` 为正数 USDT）

- **借款**：等额增加账本现金与负债，不增加净权益。若当前**有负债且净资产 ≤ 0**，继续借款会被拒绝。
- **还款**：不超过可用现金（已扣除挂单占用）且不超过当前负债。爆仓后债务保留。
- **计息与提醒**：按后台日利率对未还本金单利计息，每天北京时间 `00:00` 扣前一日利息（首日按实际时长折算，扣息可使现金为负）。
  - 当账户有负债或未结利息时，优先读取 `get_account_state(include=["balances"])` 中的 `balances.interest_reserve` 或下单/转账回执中的 `interest_reserve`，用一句话自然补充提醒（参考 `estimated_next_interest`、`overdue_interest`、`shortfall`、`message`）。
  - `advisory_only=true` 表示仅提醒：不自行拒单或擅自缩减用户数量，未返回可靠数值时不猜测利率或金额。

### 4. 转账与 Bot 接济
调用：`paper_transfer(user_id, recipient_user_id, amount, operation_id)`

- 在同一服务已开户账户间转正数 USDT，金额不得超过付款方 `available_cash`（挂单占用和仓位保证金不能转出）。
- `recipient_user_id` 必须是可信的**纯数字 QQ 号**或 `bot_user_id`。
- 借来的可用现金允许转出；破产账户**可以收款但不能转出**；旧周期（下海前）请求重试不得动用下海后新资金。

### 5. 破产冻结与“下海”重置
- **破产判定**：空仓（无持仓且无现货）时净资产 ≤ 0，或北京时间 `00:00` 扣息时账本现金不足本次利息，账户进入 `account_status="bankrupt"`。仅因挂单占用导致可用余额不足，不会因扣息判破产；有持仓时净资产为负也不会自动破产。
- **破产限制**：禁止开平仓、撤改单、设保护、调仓、借还款和转出，已有挂单不成交；**可以收款，但收款不会自动解除破产**。
- **“下海”重置**：
  - 只有**已破产的人类用户本人**在可信消息中主动说“下海”明确要求重置时，调用：
    `configure_paper_account(action="reset", user_id=<纯数字QQ号>, operation_id=<消息稳定ID>)`
  - **严禁**主动邀请用户下海、自动替用户重置、替他人代喊下海重置，或让 `bot_user_id` 下海。重置清空资金、负债、仓位挂单与本轮收益率，保留历史记录和累计下海次数。

---

## 三、行情、记录与事件轮询

- **上下文与标的**：先 `get_trading_context(user_id?)` 确认费率、杠杆上限和 `bot_user_id`。陌生币名或股票代币（如 MU/MUB/美光）先 `search_symbols(keyword=...)`，沿用返回的规范 `symbol`、`market_type` 和 `binance`。
- **行情与新闻**：`get_market(symbol, market_type="spot"|"swap", include=...)`；**只查当前价格/盘口时传 `include=["ticker"]` 以保持响应轻快**，需要分析时再加 `"candles"`、`"analysis"`、`"sentiment"`。新闻用 `get_news`。股票代币/永续不能说成原股现货。
- **账户与历史**：
  - `get_account_state(user_id=..., include=["balances"|"positions"|"inventory"|"orders"|"performance"], period=...)` 按需取字段；关注 `completeness.valuation` 与 `filter`，`null` 不可当成 `0`。
  - `get_trade_records(record_type="orders"|"order"|"fills"|"history", user_id=..., ...)`；`order_id` 是本地订单 ID，不同于 `operation_id`。
  - `get_paper_leaderboard(user_id=..., metric="return_pct"|"reset_count")` 查收益率榜与下海榜。
- **异步成交通知轮询（服务调度用）**：`GET /api/paper/events?after_id=<cursor>&limit=100`（Paper Bearer Key），按 `delivery_key` 去重并在投递或入队后保存 `next_cursor`。

---

## 四、幂等、报价过期与状态核对

- **稳定 `operation_id`**：长度 8–160 字符（字母数字与 `_.:-`），绑定同一意图与参数；超时或断线时**必须用原 `operation_id` 重试或用 `get_trade_records` 核对订单**，禁止换新 ID 盲重下导致重复开单或重复扣款。
- **15 秒报价过期**：后台撮合报价超过 15 秒时，原挂单、OCO 和资金占用原样保留，等待下一轮新报价，既不变成失败单也不用过期价强平，也不按历史 K 线回溯补成交。
- **状态口径**：`open` / `accepted` / `submitted` 仅表示已挂单或已受理，只有看到成交证据（`filled` / `fill`）才能说成交。
