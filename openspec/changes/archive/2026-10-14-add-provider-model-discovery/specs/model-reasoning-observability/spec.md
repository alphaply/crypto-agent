# Spec Delta

## MODIFIED Requirements

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
