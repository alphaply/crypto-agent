# model-reasoning-observability Specification

## Purpose

让用户能够核对模型推理配置实际发送到哪个协议和模型，并区分服务商报告的推理用量、可展示的推理文本与无法确认的情况，使任务、聊天及历史记录如实表达推理状态。

## Requirements

### Requirement: Effective reasoning requests are observable
系统 SHALL 记录并向有权查看该运行的用户展示请求模型、实际选择的渠道与协议、配置 effort 和最终发送的推理参数。回退模型、参数映射和关闭推理的实际行为 SHALL 可辨认。诊断 SHALL 只包含允许公开的配置字段，不包含密钥、鉴权头或完整提示词；HTTP 成功不得被描述为指定 effort 已由上游严格执行。

#### Scenario: OpenAI compatible effort is sent
- **WHEN** 渠道使用 OpenAI 兼容协议并配置 high effort
- **THEN** 诊断显示最终请求是否包含 reasoning_effort=high，并将该事实表述为“已发送”，不推断上游执行强度

#### Scenario: Native effort and fallback are identifiable
- **WHEN** 当前轮使用原生 Messages 的 output_config.effort，后续轮切换至回退渠道
- **THEN** 每轮分别展示实际协议、模型、推理参数和回退事实，不沿用主渠道的配置标签

### Requirement: Reasoning text and reported usage have distinct states
系统 SHALL 区分存在非空推理文本、仅有正数推理用量、已请求但未返回有效推理证据以及显式关闭的状态。空字符串、空白增量或只有签名的 thinking 块 SHALL 不被当成推理文本；缺少或报告零推理 tokens SHALL 不被单独解释为未思考。系统不得生成替代思考内容。

#### Scenario: Native response reports hidden reasoning
- **WHEN** Messages 返回空 thinking 块及 thinking_tokens=139
- **THEN** 显示“服务商报告 139 个推理 tokens，未提供可展示文本”，不隐藏用量或声称配置无效

#### Scenario: Chat response supplies no usable reasoning evidence
- **WHEN** 请求发送 high effort，但响应只有空白 reasoning_content 和 reasoning_tokens=0
- **THEN** 显示“已请求推理，上游未返回可展示文本或正数推理用量”，不声称已关闭推理或 high 已严格生效

### Requirement: Reasoning evidence survives streaming and tool turns
系统 SHALL 在非流式、流式、工具调用轮次及最终保存记录中保留上游提供的推理文本、推理用量和请求诊断。每轮用量 SHALL 正确归一化，累计与增量不得重复计算；推理签名 SHALL 按协议保留以支持工具结果续接，但不得作为用户可读思考展示。

#### Scenario: Stream reports usage at completion
- **WHEN** 流中 thinking_delta 为空，最终 usage 报告 thinking_tokens=143
- **THEN** 运行完成及历史页面均显示 143 个推理 tokens，不因先前空增量丢失最终用量

#### Scenario: Tool result continues native conversation
- **WHEN** 原生响应包含 thinking 签名与工具调用，下一轮回传工具结果
- **THEN** 原始内容块关联保持有效，诊断按轮次保存，模型调用次数不因缺失文本额外增加

### Requirement: Provider compatibility changes are explicit
现有渠道协议 SHALL 不因模型名称或缺少推理文本被自动切换。配置界面 SHALL 解释已知模型的协议差异和不支持参数，显式选择原生协议后使用对应参数。发起生成请求的在线推理或能力探测 SHALL 由用户显式触发并限制调用次数与输出预算；常规回归使用脱敏离线响应。配置界面可在用户完成连接信息后自动读取只读模型列表，但 SHALL 不因此执行生成请求、切换协议或宣称模型推理及工具调用能力已验证。

#### Scenario: Existing B AI Claude channel remains stable
- **WHEN** 升级包含新推理诊断能力
- **THEN** 已有渠道保持原协议和凭据，用户可以查看差异后显式切换；不承诺切换后一定获得推理文本

#### Scenario: Unsupported disable setting
- **WHEN** 已知模型不支持关闭推理或某个 effort/采样参数
- **THEN** 配置或请求校验给出具体原因，不静默将其描述为成功关闭；无法确认的网关能力标记未验证

#### Scenario: Automatic catalog discovery is not an inference test
- **WHEN** 配置界面自动取得供应商模型列表并由用户选中一个候选
- **THEN** 只更新模型草稿值，不额外调用生成接口、不改变协议和推理参数，也不把列表成功表述为这些能力已通过验证
