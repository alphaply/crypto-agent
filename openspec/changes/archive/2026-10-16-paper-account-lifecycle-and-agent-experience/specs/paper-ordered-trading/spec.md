# Spec Delta

## ADDED Requirements

### Requirement: Bankruptcy admission is checked at financial commit
Paper 交易入口、兼容入口和后台订单成交 SHALL 在提交金融变化时检查账户破产状态。破产后 SHALL 拒绝新的用户金融操作，并停止已有订单成交；读取及原事实核验 SHALL 不被误当作新交易。并发破产状态更新 SHALL 不被较早的准入结果覆盖。

#### Scenario: Bankruptcy races with an order fill
- **WHEN** 报价已读取，订单成交提交前账户已进入破产状态
- **THEN** 提交前重新核验状态，不产生成交或覆盖破产状态，旧订单保持未成交事实

#### Scenario: Legacy endpoint tries to bypass the freeze
- **WHEN** 破产账户通过隐藏旧交易入口请求开平仓、撤改单或保护操作
- **THEN** 与新入口一样拒绝，不因 schema、旧工具名或管理员停用字段不同而绕过破产限制

### Requirement: Reset isolates new funds from old trading identities
下海重置 SHALL 建立新的账户周期并终止旧周期待执行资源。已持久化的父子身份、原回执和历史 SHALL 保留。旧周期操作的重试、后台恢复、撮合或异步结果 SHALL 不得再次成交、释放新周期预留、覆盖新周期状态或支配重置补款。

#### Scenario: Old batch is replayed after reset
- **WHEN** 用户下海后重放旧周期已保存的批次或旧订单事件晚到
- **THEN** 返回或核验原持久事实，不重新执行旧动作，不改变新现金、收益率、挂单、持仓或下海次数

#### Scenario: Reset races with matching
- **WHEN** 同一账户的下海重置与后台撮合并发
- **THEN** 账户周期与状态按同一金融提交边界串行核验，重置成功后不出现旧订单对新周期的成交
