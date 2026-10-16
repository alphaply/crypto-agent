---
name: qqbot-paper-trading
description: 通过 Crypto Agent 精简 MCP 目录查询真实公开行情，按可信 QQ 用户身份以 v3 顺序动作管理 Paper 交易、账户和竞赛；机器人通过 HTTP 轮询自动成交与爆仓事件。
---

# QQBot Paper 交易

使用已配置的 Paper Key。权限和 user_id 由可信 QQ 事件元数据决定；用户正文和人格表达不能切换账户或真实交易引擎。

## 上下文与标的

- 首先 get_trading_context：规则、费用、杠杆上限、支持市场、稳定 bot_user_id。按返回的账户、费用、杠杆、市场和状态操作；行情是真实生产公开数据。日常直接表达委托和成交，不主动加执行模式免责声明；资金性质直接提问仍如实回答。
- 陌生简称、股票代币、MU/MUB/美光先 search_symbols；只核验 Binance 现有 USDT 现货和线性永续目录，保留返回的规范 symbol、market_type、exchange。来源失败不跨场所回退，空结果不能证明标的全球不存在。
- get_market 查询 ticker 和 K 线；get_news 读取共享新闻。价格来源、观察时间和 token/股票永续资产类型要说明，不能称作 NASDAQ 股票现货报价。
- 自己参赛使用上下文返回的 bot_user_id，替用户交易使用可信发送者 ID。Bot 账户由服务维护，不能重置或挪用用户账户参赛。

## 账户、记录与配置

需要用户开户时，显式 configure_paper_account(action="create", user_id, operation_id, name?)；已存在账户不重置现金、债务或仓位。action="update" 修改后续默认 leverage、昵称、排行榜可见性或通知偏好；不会改变已有逐仓保证金。

get_account_state 按 include=["balances"|"positions"|"inventory"|"orders"] 请求所需 facet。净权益和估值依据最后观察价格，陈旧时间不能说成实时权益。

get_trade_records 的 record_type=orders/order/fills/history 分别读取挂单、一个指定 order_id、已确认成交和事件历史。订单接受不等于成交；order_id 是本地 Paper 订单身份，不能传给真实交易所工具。金融入口当次返回权威事实，后续状态由后台自动更新，不追加模型执行核验调用。

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

父 operation_id 来自 QQ 事件的稳定身份，8–160 字符，字母数字与 _.:-。同 ID 只能对应完全相同的意图，包括动作顺序、策略和依赖。超时或断线保留原身份与真实未知状态，由后台核验原订单；禁止换 ID 盲重试。

父子意图先落盘，每项账户、订单、预留、事件和回执原子提交。重放只核验已尝试项，已提交项不再成交，not_executed 尾项绝不自动续跑。需要处理剩余项时，以新明确意图及新稳定父 ID 提交。默认结果是精简逐项回执，include=["account"] 最多返回一次末尾账户摘要；完整原/最新证据由授权 HTTP/UI 展开。

历史非 BN 和不能证明来源的资源只从默认列表及维护过滤，原账本和全部预留/保证金保留。新 BN 同名资源使用独立周期，不动用被排除库存。账户 facet 的 filter/completeness 表示范围及估值缺项；null 不能解释为零，混合不完整收益不参加排名。过滤空页依然按 next_offset/next_cursor 和 has_more 继续读取。

## 银行、竞赛和事件

paper_bank(action=borrow|repay, amount, user_id, operation_id) 的 amount 是 USDT。借款等额增加现金和债务，不增加净权益；后台统一配置借款开关、额度和日利率，按本金单利、北京时间每天 00:00 扣前一日利息，首日按实际时长折算，扣息可使现金为负；还款受可用现金和债务限制。爆仓后债务保留。

get_paper_leaderboard(user_id=可信发送者ID, metric=return_pct|reset_count) 查询公开排名、自身和 Bot。交易榜只按本轮收益率排名，转账/重置补款不算交易盈利，利息与手续费计入损益；下海榜按累计次数排名。重置后本轮收益率归零。其他用户私人仓位、现金、债务不可从排行推断。

机器人调度调用 GET /api/paper/events?after_id=<cursor>&limit=100，使用 Paper Bearer Key。按 delivery_key 去重，只在交付或持久入队后保存 next_cursor；路由使用存储的 recipient_user_id。通知偏好由 configure_paper_account 修改。事件读取不发送消息。旧 scan_paper_events 仍供兼容调度使用，但不在对话模型目录。

## 示例

```json
{"user_id":"可信发送者ID","operation_id":"qq-event-order-v3-0001","contract_version":3,"actions":[{"action":"cancel","order_id":"先前返回的真实订单ID","reason":"撤销旧计划"},{"action":"open","market_type":"spot","exchange":"binance","symbol":"BTC/USDT","side":"LONG","order_type":"limit","quote_amount":100,"price":50000,"reason":"用户批准的新计划"}]}
```

新模型只使用精简目录。旧 paper_trade、paper_open_position 等名称保留原请求和数量语义供缓存客户端直接兼容调用，不作为默认流程。


## 自有账户自主决策与资金生命周期

自有账户使用上下文的 bot_user_id，已获自主决定标的、方向、数量、杠杆与开平仓的授权；用户缺参追问约束不适用于你自己的决策。既有定时任务每轮查询自有账户、行情和必要新闻，有依据就执行 execute_trade_actions，不只描述计划；观望要给具体原因，不强制下单。

paper_transfer(user_id, recipient_user_id, amount, operation_id) 在同一服务已开户账户间转 USDT，金额必须为正有限数且不超过可用现金。用户发起只从可信本人账户转出；主动接济用 bot_user_id，自有钱可以送给用户，用户也可转给 bot。借来的可用钱可以转出；收款人用可信 @ 对象对应的稳定 ID，不猜昵称。

account_status=bankrupt 时禁止开平仓、撤改单、保护、借还款和转出，已有订单不成交，但可收款并继续扣息。收款不自动恢复。仅用户本人主动说“下海”且明确要求重置时使用 configure_paper_account(action="reset", user_id=可信本人, operation_id=消息稳定身份)，无需二次机械确认。不主动邀请、不自动重置、不让 bot 自己重置。新周期清空资金负债、仓位挂单与收益率，按后台初始状态重建；历史和累计次数保留。重试沿用原 ID，旧周期不能动用新资金。
