# Spec Delta

## ADDED Requirements

### Requirement: Batch dependency guidance matches validation
模型可见的批次契约 SHALL 说明依赖只适用于显式 execution_policy=independent，depends_on 可省略但提供时必须为非 null 的严格整数列表，索引从零开始且只引用不重复的此前项。Schema、示例和运行时拒绝信息 SHALL 一致；错误指出相关字段和纠正条件。系统 SHALL 保留省略策略时的默认停止语义与既有序列化身份，不自动切换策略、丢弃依赖或执行非法批次。

#### Scenario: Dependency requires explicit independent policy
- **WHEN** v2 批次携带 depends_on 但省略 execution_policy 或选择 stop_on_error
- **THEN** 整批在写入前拒绝，错误指出动作索引和显式 independent 要求；工具说明区分批次策略与任务的 independent_exits 模式

#### Scenario: Schema does not suggest null dependencies
- **WHEN** Agent 或 MCP 取得 v2 批次 Schema
- **THEN** depends_on 展示为可省略的整数数组，不展示 null 为合法值；依赖示例显式指定 independent，首项省略依赖，后项引用此前 completed 项

#### Scenario: Invalid dependency leaves all actions unexecuted
- **WHEN** 依赖为 null、布尔值、字符串、重复索引、负值或自身及之后索引
- **THEN** 系统返回可定位的参数错误，在任何交易写入前拒绝且不自动修补；合法批次仍按既有 completed 依赖条件执行

#### Scenario: Omitted fields retain request identity
- **WHEN** 旧 v2 请求省略策略和依赖并被重新校验或重放
- **THEN** 序列化保持省略，操作身份与默认停止行为不变，已执行动作不重复提交
