# durable-agent-queue Specification

## Purpose

使定时策略、新闻触发与现货定投在运行名额不足时保留每次已受理触发，按可执行顺序持续处理，并清晰展示当前运行及排队状态，避免忙碌丢轮、重复预算或重启后重复交易。

## Requirements

### Requirement: Accepted triggers remain queued until eligible
系统 SHALL 持久保存每次合格触发，容量不足时排队，不因等待时长丢弃，不合并不同触发。同一触发身份去重；同一任务不重叠，跨任务按最早可运行项使用空闲名额。排队不占运行线程。

#### Scenario: Busy task does not block another ready task
- **WHEN** A 正在执行，队列依次为另一次 A、B，且仍有运行名额
- **THEN** B 可以先执行，另一次 A 保留并等待前次结束

#### Scenario: DCA retains each accepted slot
- **WHEN** 三个定投时槽已经入队但尚未执行
- **THEN** 三轮分别执行并保留各自原时槽预算身份，不合并或重置预算，仍受余额及总预算限制

### Requirement: Queue lifecycle follows current configuration
系统 SHALL 在运行前读取最新模型、策略、金额、行情和账户事实；暂停、删除或账户/交易所/标的/交易模式变化 SHALL 取消未开始的旧项。取消队列不得删除已提交订单的恢复证据。

#### Scenario: Configuration changes while waiting
- **WHEN** 模型或策略参数在排队期间修改
- **THEN** 未开始项执行最新参数；交易范围改变则旧项取消，后续新触发使用新范围

### Requirement: Restart preserves queue and original execution identities
未开始项 SHALL 在重启后继续排队；已开始且中断的轮次不得直接重新调用模型重发交易，已尝试订单 SHALL 继续按原身份核验。

#### Scenario: Process stops after a possible submission
- **WHEN** worker 在可能已向交易所提交后退出
- **THEN** 保留中断及未知执行事实，不将该轮重新作为全新交易执行

### Requirement: Running and queued work are displayed separately
界面 SHALL 同时表达当前运行、排队数量与下一等待项，不让新排队或取消记录覆盖实际仍在运行的状态。

#### Scenario: A second trigger arrives during execution
- **WHEN** 当前策略运行中且下一触发入队
- **THEN** 页面仍显示运行中，并单独显示待运行项
