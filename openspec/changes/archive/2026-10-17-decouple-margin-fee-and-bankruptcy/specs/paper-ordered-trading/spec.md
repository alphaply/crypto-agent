# Spec Delta

## MODIFIED Requirements

### Requirement: Paper market execution and sizing retain existing meanings
Paper SHALL 仅读取 Binance 已核验且受现有能力支持的生产公开行情模拟成交，并保留 USDT 现货和线性永续、费用、杠杆及逐仓约束。基础币 amount、USDT quote_amount、开仓 margin_amount 和退出 close_all SHALL 按动作能力互斥；现货 SHALL 不接受杠杆，预算数量 SHALL 不用于退出，回执 SHALL 保留单位、venue 与真实行情来源。来源失败 SHALL 不跨场所回退。

#### Scenario: Spot buy uses a quote budget
- **WHEN** 现货买入选择 quote_amount
- **THEN** 按包含现货买入手续费的 USDT 预算计算基础币数量，不将金额解释为基础币或合约张数

#### Scenario: Perpetual entry uses margin sizing
- **WHEN** 永续开仓选择 margin_amount 并指定有效杠杆
- **THEN** 金额作为包含开仓手续费的总成本预算，系统自动根据杠杆与手续费率倒推持仓保证金与开仓手续费，两者扣款之和严格等于该金额，遵守账户最大杠杆及维持保证金限制，避免额外扣费导致现金透支

#### Scenario: Same instrument is available on several venues
- **WHEN** 用户旧账本在多个场所有同名标的，新动作需要匹配目标
- **THEN** 仅可执行可证明为 BN 且符合动作能力的目标；非 BN 与来源不明资源被过滤，不以 BN 同名资源替代被引用的旧目标，回执与完整原审计保留真实 venue

#### Scenario: Binance public quote is unavailable
- **WHEN** 当前合法 BN 动作的公开行情在预算内不可用
- **THEN** 返回明确依赖失败或未知执行事实，不使用其他场所报价填补，不以缓存缺项伪造市场成交

### Requirement: Bankruptcy admission is checked at financial commit
Paper 交易入口、兼容入口和后台订单成交 SHALL 在提交金融变化时检查账户破产状态。破产后 SHALL 拒绝新的用户金融操作，并停止已有订单成交；读取及原事实核验 SHALL 不被误当作新交易。并发破产状态更新 SHALL 不被较早的准入结果覆盖。持有有效持仓但净资产小于等于零的未破产账户，准入检查 SHALL 允许平仓与退出订单撮合，仅拒绝新增开仓。

#### Scenario: Bankruptcy races with an order fill
- **WHEN** 报价已读取，订单成交提交前账户已进入破产状态
- **THEN** 提交前重新核验状态，不产生成交或覆盖破产状态，旧订单保持未成交事实

#### Scenario: Legacy endpoint tries to bypass the freeze
- **WHEN** 破产账户通过隐藏旧交易入口请求开平仓、撤改单或保护操作
- **THEN** 与新入口一样拒绝，不因 schema、旧工具名或管理员停用字段不同而绕过破产限制

#### Scenario: Negative net equity account executes exit operations
- **WHEN** 账户仍持有有效持仓但账面净资产小于等于零，请求平仓、追加/撤销止盈止损或其退出订单满足触发条件
- **THEN** 金融提交准入允许该操作执行并完成撮合，不被误作为破产冻结拦截
