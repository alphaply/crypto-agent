## MODIFIED Requirements

### Requirement: Queues and retained work sets remain bounded
系统 SHALL 限制实际运行数并保持公平进度。已受理的策略、新闻和定投触发 SHALL 持久保存，同一触发身份去重，不合并不同触发，不按等待时长或忙碌丢弃；其他类别继续使用其原有排队及等待限制。队列、待处理快照、历史扫描及保护轮询的内存工作集 SHALL 随声明容量受限，不能一次装载全部历史或以配置数量增长绕过界限。不可接受的容量配置 SHALL 明确拒绝，不能默默恢复成无界默认。

#### Scenario: More configurations arrive than workers can serve
- **WHEN** 配置数或触发数超过当前可服务容量
- **THEN** 已受理策略、新闻和定投触发保留在持久队列并显示等待，按最早可运行顺序领取；不在内存积累无界 futures 或全量任务副本，其他类别继续遵守其原有队列与等待限制

#### Scenario: Long history is processed under pressure
- **WHEN** 原计划、成交或 Paper 历史超过单次内存工作集
- **THEN** 按有界分页/批次推进并保存进度，不把读取失败或未扫描尾部表示为无订单、无持仓或对账完整

#### Scenario: A single financial record exceeds the work budget
- **WHEN** 单个账户或回执超过声明的读取/解析预算，完整资金承诺无法在预算内核验
- **THEN** 保留原记录并返回资源不足、未处理和可核对身份，不通过缩小页数后无界解析，不把部分预留作为完整准入依据或执行该账户新的金融变化
