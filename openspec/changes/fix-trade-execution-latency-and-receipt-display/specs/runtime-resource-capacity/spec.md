## ADDED Requirements

### Requirement: Historical ownership work yields with durable incremental progress
历史订单归属整理 SHALL 作为受现有总容量约束的后台工作推进，每次受实际生效的工作量与时间预算限制，保存已完成进度后退出；按页读取不得等价于一次循环跑完整个历史。新增或变化来源 SHALL 增量处理，不无条件清空已完成历史重新扫描。初次整理、变更及重启恢复 SHALL 保留全部原始证据和未完成状态，不能因预算耗尽发布不完整归属或丢弃晚提交的变化。

#### Scenario: A long initial history exceeds one work slice
- **WHEN** 初次历史整理超过本次工作量或时间预算
- **THEN** 本次后台调用保存有效进度并退出，下一次续接；未扫描尾部不标为完整，不因调用方是金融请求而同步跑完

#### Scenario: Source changes while history is being processed
- **WHEN** 后台整理期间新增事件、修改当前计划或更正旧来源
- **THEN** 后续处理能覆盖变化及晚提交来源，不无条件重扫全部已完成来源；只影响观察信息的变化不重置归属整理进度

#### Scenario: A worker restarts between slices
- **WHEN** 后台 worker 在批次间重启
- **THEN** 从持久进度与未完成变化继续，不要求前台下单承担重建，不丢失已有链接、来源或真实完整性信息

#### Scenario: Regular order evidence appears beyond the first slice
- **WHEN** 相同数字的条件母单和普通订单证据分布在不同历史批次
- **THEN** 在取得必要完整证据之前保留未完成状态，不提前删除有效普通订单链接或错误归属成交；后续继续处理而非为一次完成占用无界工作

### Requirement: Background history cannot monopolize financial coordination
后台全量扫描、索引整理及收益历史重建 SHALL 与金融动作的协调边界分离，不在金融共享锁内执行整个历史工作，也不让金融调用等待该历史工作释放长锁。短时发布 SHALL 保持版本一致性及原跨进程写入协调；后台工作仍计入总容量并去重推进，不能以新增无界线程逃避预算。

#### Scenario: One account has both background history and new trading
- **WHEN** 同账户存在历史积压，同时收到具备所需当前事实的合法金融动作
- **THEN** 历史积压不独占金融锁；交易在原当前准入与有界协调下执行，后台下次继续已有进度

#### Scenario: Two processes encounter the same historical backlog
- **WHEN** Web 与 scheduler 同时请求推进相同历史索引
- **THEN** 通过持久协调避免重复推进和互相覆盖检查点，无法取得后台工作权的一方及时退出，不长期占着金融锁排队
