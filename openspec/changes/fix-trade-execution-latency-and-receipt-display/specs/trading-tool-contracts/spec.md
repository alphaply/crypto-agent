## ADDED Requirements

### Requirement: Foreground trading does not depend on historical reconstruction
金融调用 SHALL 仅等待当前动作所需的授权、目标、数量、并发协调和实际交易请求，不等待全量历史订单归属整理或收益历史重建。后台历史工作 SHALL NOT 通过共享长时间锁或同步回调间接阻塞下单。权威受理回执 SHALL 作为提交成功依据，保存原身份与回执后返回，不为确认提交再次查询交易所，不以历史整理完整作为返回条件。当前依赖确实缺失时 SHALL 保留既有具体拒绝与未决语义，不放宽交易权限或伪造事实。

#### Scenario: Existing managed entry proceeds while historical indexing is incomplete
- **WHEN** 已管理范围内的合法限价入场具备所需当前事实，但历史归属整理尚未完成
- **THEN** 本次调用按正常当前准入提交并返回交易所受理事实，不扫描全部历史、不等待历史整理结束，不把历史积压单独作为提交阻塞条件

#### Scenario: Background reconstruction is slow while a financial request arrives
- **WHEN** 后台正在处理大量历史记录，另一获授权调用请求操作同账户资源
- **THEN** 该请求不等待历史批次完成才能取得金融协调；真正冲突的金融写入仍按原范围、预算和版本约束串行处理

#### Scenario: An accepted order returns without secondary submission checks
- **WHEN** 交易所已返回有效订单身份和权威受理回执，挂单尚未成交
- **THEN** 保存提交成功后直接返回，不追加确认本次提交的订单或仓位查询，不等待未来成交；内部 submitted 与 completed 及批次依赖规则保持原含义

#### Scenario: Historical work fails after an accepted submission
- **WHEN** 原提交已获权威成功回执，后台历史、成交或持仓更新随后失败
- **THEN** 保留原提交成功事实，缺项只影响相应后台观察，不将原提交改为失败或未知，不重发订单或补执行批次尾项

#### Scenario: Current target evidence remains necessary
- **WHEN** close、edit 或 cancel 的当前目标、实际数量或相关原写入仍不明确
- **THEN** 仅依据当前动作真实依赖执行原有有界检查和错误处理，不借移除历史扫描跳过准入，也不要求模型先发另一工具调用
