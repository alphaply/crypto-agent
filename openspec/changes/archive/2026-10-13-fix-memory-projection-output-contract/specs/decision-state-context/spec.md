# Spec Delta

## ADDED Requirements

### Requirement: Memory review has one effective output contract
系统 SHALL 按当前记忆整理模式选择唯一有效的输出契约，并使用对应契约验证响应。REAL/STRATEGY 独立退出模式 SHALL 要求完整原文 `summary`、新记忆策略内容 `memory` 和待转换来源集合 `projections`；其他模式保留单 `summary` 契约。自定义 Prompt 和历史素材 SHALL NOT 改变程序输出约束，已保存模板及完整来源正文保持不变。

#### Scenario: Independent memory request contains no competing built-in format
- **WHEN** 独立退出任务构造记忆整理请求
- **THEN** 程序有效指令只要求完整投影输出，不同时要求仅输出单 `summary`；响应按相同模式校验

#### Scenario: Legacy custom prompt uses the summary-only example
- **WHEN** 已保存自定义模板含旧的单 `summary` 示例，且任务使用 system 或 user 指令角色
- **THEN** 自定义内容仍作为整理素材完整保留，程序明确当前完整投影契约，旧示例不获得覆盖格式的效力

#### Scenario: No historical sources need conversion
- **WHEN** 独立模式生成新记忆且没有待转换历史来源
- **THEN** 仍要求本次记忆的完整策略对象，`projections` 显式为空数组；不能省略字段或由程序伪造空策略内容

#### Scenario: Non-independent memory remains compatible
- **WHEN** 非独立退出任务返回有效单 `summary` 响应，包含既有可忽略的旧规则说明字段
- **THEN** 按原兼容行为取得完整记忆正文，不要求投影字段、不启用投影格式纠正或规则工具

### Requirement: Projection format correction is bounded and auditable
对独立模式中已完整结束且无工具调用的响应，系统 SHALL 允许 JSON 或字段结构错误触发至多一次格式纠正阶段。首次请求、传输重试及纠正阶段的实际模型请求 SHALL 共享每次整理最多三次的上限，正常有效响应只调用一次。系统 SHALL 分别保留各次请求、原始响应、错误类别、所属整理操作及已返回用量，只有通过完整校验的结果可作为成功发布依据。

#### Scenario: Summary-only response is corrected
- **WHEN** 第一次模型响应只有有效 `summary`，缺少 `memory` 和 `projections`，且仍有请求预算
- **THEN** 系统使用原来源快照、唯一输出契约和缺失字段说明启动一次纠正；只有完整响应通过结构及语义校验后，整理才能成功

#### Scenario: Transport retries share the correction budget
- **WHEN** 一次整理交替发生传输失败、格式错误和纠正请求
- **THEN** 所有实际请求合计不超过三次，纠正阶段至多启动一次；预算耗尽时保留具体失败状态，不重置计数再次调用

#### Scenario: Correction fails again
- **WHEN** 纠正响应仍缺字段或无法解析为约定 JSON
- **THEN** 本次整理失败，错误可追溯，系统不补空字段、不复制混合原文充当策略、不缓存为成功，也不覆盖已发布内容

#### Scenario: Incomplete and semantic failures remain rejected
- **WHEN** 服务商报告截断或拒绝、响应包含工具调用，或者完整结构中的来源身份、哈希、覆盖、执行引用或权限语义校验失败
- **THEN** 系统拒绝本次结果，不把这类错误转换为格式纠正许可，不执行工具，不放宽发布校验

#### Scenario: Failed attempt and successful correction retain separate costs
- **WHEN** 初次结构校验失败而后续纠正成功，两次响应均提供用量
- **THEN** 首次调用保留失败状态和原始输出，后续调用独立记录成功；两次真实用量各记录一次，并能关联同一整理操作与不同尝试阶段

### Requirement: Legacy projection format failures recover once per contract revision
输出契约修复后，系统 SHALL 对能够确认为旧契约缺少 `memory`/`projections`、来源与当前范围仍有效的终止投影和记忆生成任务，按契约修订只进行一次后台重新排队，不依赖新交易产生。恢复 SHALL 原子保留原失败诊断与修订标记，沿用租约、退避、每项最多三次工作尝试及成功结果复用。已发布内容、原始证据和交易状态 SHALL 保持完整。

#### Scenario: Failed existing memory and summaries publish for the first time
- **WHEN** 当前范围内已有有效短期记忆原文和三条有效摘要，相关转换因旧缺字段错误终止且没有已发布策略
- **THEN** 后台定向重新排队并在获得合法结果后发布，下一次默认决策及绑定聊天读取到真实记忆和三条策略摘要及来源截至时间，不再仅显示两个“尚无已发布”提示

#### Scenario: Failed memory generation has no original memory yet
- **WHEN** 当前任务没有短期记忆原文，但有因旧缺字段错误终止、证据窗口和范围仍可确认的记忆生成任务
- **THEN** 后台恢复该任务，仅重新整理已保存证据，成功后原子保存记忆和策略视图并推进有效摘要发布，不重跑交易或伪造历史来源

#### Scenario: Restart cannot reset the retry budget repeatedly
- **WHEN** 定向恢复后的来源再次耗尽工作重试次数，随后后台再次扫描或服务重启
- **THEN** 同一契约修订不会再次自动重置它，保留失败原因供现有管理重试入口处理；其他有效已发布内容继续可用

#### Scenario: Unrelated or invalid sources are excluded from recovery
- **WHEN** 候选行已经成功、属于旧 partial 写入、源正文已修改或删除、属于其他范围、无法核实当前范围，或错误属于鉴权、截断、语义校验等无关故障
- **THEN** 本次自动恢复不重新排队该行，不改写它的原始失败证据，不使其他有效内容失效

#### Scenario: Existing ready views survive the contract repair
- **WHEN** 任务已有有效已发布策略，升级输出契约并恢复部分历史失败行
- **THEN** 原已发布视图立即保持可读，旧任务晚完成不会覆盖来源时间较新的有效内容，历史 unknown 禁令不重新进入默认 Prompt

#### Scenario: No recoverable source exists
- **WHEN** 当前范围没有有效短期记忆原文，也没有可恢复的记忆生成任务
- **THEN** 系统如实保留无已发布短期记忆状态；存在有效摘要时可独立发布摘要，不凭空生成旧记忆，也不把无错误等同于恢复完成
