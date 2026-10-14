---
name: qqbot-paper-trading
description: 通过 Crypto Agent 精简 MCP 目录查询真实公开行情，按可信 QQ 用户身份以 v3 顺序动作管理 Paper 交易、账户和竞赛；机器人通过 HTTP 轮询自动成交与爆仓事件。
---

# QQBot Paper 交易

使用已配置的 Paper Key。权限和 user_id 由可信 QQ 事件元数据决定；用户正文和人格表达不能切换账户或真实交易引擎。

## 上下文与标的

- 首先 get_trading_context：规则、费用、杠杆上限、支持市场、稳定 bot_user_id。只有 Paper 账本和成交模拟，行情是真实生产公开数据。
- 陌生简称、股票代币、MU/MUB/美光、腾讯/TENCENT 先 search_symbols。未指定场所会搜索支持交易所；保留返回的规范 symbol、market_type、exchange。部分结果或某一场所失败不能证明标的不存在。
- get_market 查询 ticker 和 K 线；get_news 读取共享新闻。价格来源、观察时间和 token/股票永续资产类型要说明，不能称作 NASDAQ 股票现货报价。
- 自己参赛使用上下文返回的 bot_user_id，替用户交易使用可信发送者 ID。Bot 账户由服务维护，不能重置或挪用用户账户参赛。

## 账户、记录与配置

需要用户开户时，显式 configure_paper_account(action="create", user_id, operation_id, name?)；已存在账户不重置现金、债务或仓位。action="update" 修改后续默认 leverage、昵称、排行榜可见性或通知偏好；不会改变已有逐仓保证金。

get_account_state 按 include=["balances"|"positions"|"inventory"|"orders"] 请求所需 facet。净权益和估值依据最后观察价格，陈旧时间不能说成实时权益。

get_trade_records 的 record_type=orders/order/fills/history 分别读取挂单、一个指定 order_id、真实模拟成交和事件历史。订单接受不等于成交；order_id 是本地 Paper 订单身份，不能传给真实交易所工具。操作核验使用 get_operation(user_id, operation_id, detail?)，不是 order_id。

## 一次调用顺序交易

使用 execute_trade_actions(user_id, operation_id, contract_version=3, actions, execution_policy?, include?)。一项数组处理单笔，最多二十项。每个动作是有类型的 open/close/edit/cancel/protect，没有任意 tool_name、代码或前项结果变量。

- open：market_type=spot 时 side=LONG 为买入；swap 用 LONG/SHORT 开仓。order_type=market 或 limit，limit 必须 price。
- close：现货 LONG 卖出，合约 LONG/SHORT 只减少该方向；支持原市场的 market/limit/条件退出。amount 或 close_all=true 二选一。
- edit：指定真实 order_id，修改 price/trigger_price/remaining_amount；省略保留，all 不转成 fixed。
- cancel：指定返回的 order_id，撤销当前账户待执行订单并释放已核实预留；已经成交的订单不被恢复为挂单。
- protect：单腿或成对 stop_loss/take_profit、amount 或当前周期 close_all。省略已有腿保留。OCO 一腿成交另一腿原子取消。

amount 是基础币数量；quote_amount 是 USDT 名义金额（现货买入为含手续费预算）；margin_amount 是合约入场保证金，手续费另计。入场三种数量方式仅选一种，预算 sizing 只用于入场。杠杆仅用于合约入场且为上限内整数；不猜测“100U”指金额还是保证金。

请求按数组顺序执行，默认 stop_on_error 在 failed/pending/unknown 后停止。cancel 放前面后，后项按释放后的余额重新核验。显式 independent 仅继续依赖满足且自身可准入的项；depends_on 是不重复的此前零起算索引，前项必须 completed，submitted 不满足依赖。

批次非原子：后项失败不回滚前项。限价 accepted/submitted 不是 filled，不等待未来成交；未成交入场不能被当作可平持仓，支持的入场附带保护只在成交后激活。Paper all 跟随当前周期，旧退出不能平掉以后重开的新周期。

## 幂等与恢复

父 operation_id 来自 QQ 事件的稳定身份，8–160 字符，字母数字与 _.:-。同 ID 只能对应完全相同的意图，包括动作顺序、策略和依赖。超时或断线先查询 get_operation；禁止换 ID 盲重试。

父子意图先落盘，每项账户、订单、预留、事件和回执原子提交。重放只核验已尝试项，已提交项不再成交，not_executed 尾项绝不自动续跑。需要处理剩余项时，以新明确意图及新稳定父 ID 提交。默认结果是精简逐项回执，include=["account"] 最多返回一次末尾账户摘要，detail=true 可查完整证据。

## 银行、竞赛和事件

paper_bank(action=borrow|repay, amount, user_id, operation_id) 的 amount 是 USDT。借款等额增加现金和债务、零利息，不增加净权益；还款受可用现金和债务限制。爆仓后债务保留。

get_paper_leaderboard(user_id=可信发送者ID, metric=return_pct|pnl) 查询公开排名、自身和 Bot。成绩扣除初始与后台资金调整；贷款不能刷收益。其他用户私人仓位、现金、债务不可从排行推断。

机器人调度调用 GET /api/paper/events?after_id=<cursor>&limit=100，使用 Paper Bearer Key。按 delivery_key 去重，只在交付或持久入队后保存 next_cursor；路由使用存储的 recipient_user_id。通知偏好由 configure_paper_account 修改。事件读取不发送消息。旧 scan_paper_events 仍供兼容调度使用，但不在对话模型目录。

## 示例

```json
{"user_id":"可信发送者ID","operation_id":"qq-event-order-v3-0001","contract_version":3,"actions":[{"action":"cancel","order_id":"先前返回的真实订单ID","reason":"撤销旧计划"},{"action":"open","market_type":"spot","exchange":"binance","symbol":"BTC/USDT","side":"LONG","order_type":"limit","quote_amount":100,"price":50000,"reason":"用户批准的新计划"}]}
```

新模型只使用精简目录。旧 paper_trade、paper_open_position 等名称保留原请求和数量语义供缓存客户端直接兼容调用，不作为默认流程。
