# Spec Delta

## Purpose

让用户在新增或编辑模型渠道时，直接根据当前供应商连接取得模型候选并选择准确的模型标识。支持尚未保存的配置、适用的供应商协议和明确的失败回退，使候选列表与当前连接保持一致，同时保留手动添加及既有配置行为。

## ADDED Requirements

### Requirement: Discovery works before a model is saved
新增和编辑模型界面 SHALL 在用户完成有效地址、协议及可用凭据的填写后自动读取供应商模型列表，并提供手动刷新。读取 SHALL 不要求预先填写模型 ID 或保存渠道。打开已有有效连接时 SHALL 可自动读取；连接信息不完整时不发起请求，并说明需要补齐的字段。

#### Scenario: First model for a new endpoint
- **WHEN** 用户在新增模型界面填好地址和 Key，完成字段编辑，模型名仍为空且未保存
- **THEN** 界面自动展示加载状态并获取候选，无需先构造一个占位模型配置

#### Scenario: Edit an existing connection
- **WHEN** 用户打开已有地址、协议和凭据的模型配置
- **THEN** 系统可使用该连接获取列表，当前模型名在加载期间保持不变

#### Scenario: Connection is incomplete
- **WHEN** 地址无效或没有本次可用凭据
- **THEN** 不向供应商发送请求，用户仍能编辑模型名和其他字段

### Requirement: Candidates come from the configured supplier and protocol
模型候选 SHALL 来自当前供应商连接的只读列表接口，支持既有 OpenAI 兼容、DeepSeek、Anthropic Messages 及 Jev Decisions 配置。返回结果 SHALL 保留可用于请求的原始模型 ID，按 ID 去重并允许搜索；Jev 列表仅包含符合其用途的模型。静态预设、价格目录和其他连接的结果 SHALL NOT 被冒充为本次发现结果。

#### Scenario: OpenAI-compatible gateway exposes several model families
- **WHEN** OpenAI 兼容地址返回包含不同模型家族及重复 ID 的列表
- **THEN** 候选按精确 ID 去重，保留网关提供的别名，不因模型名称包含 Claude 或 Gemini 而改用其他协议或官方地址

#### Scenario: Native Anthropic catalog has more than one page
- **WHEN** 用户配置 Anthropic Messages，供应商返回带后续页标记的模型列表
- **THEN** 系统按该协议读取后续页或明确显示列表不完整，不将第一页称作完整模型列表

#### Scenario: Unsaved Jev configuration
- **WHEN** 尚未保存的 Jev 连接返回 Jev 与生成模型混合列表
- **THEN** 用户可选择返回的 Jev 模型，无需先有模型 ID，生成模型不进入 Jev 候选

### Requirement: Choosing a candidate preserves explicit configuration
用户 SHALL 能搜索并选择一个候选，或直接输入不在列表中的模型 ID。加载、刷新和选择 SHALL 保留既有单模型保存流程，不自动保存渠道、批量导入、修改其他模型、切换协议、改写推理参数或价格。列表未包含当前模型时 SHALL 保留该值；列表读取成功不得作为实际生成或工具调用能力的验证结果。

#### Scenario: Select one returned model
- **WHEN** 用户从列表选择一个模型
- **THEN** 仅当前草稿的模型字段被更新，用户保存后才按既有流程写入配置

#### Scenario: Current model is an unlisted alias
- **WHEN** 刷新结果未包含已填的模型别名
- **THEN** 原值保持可编辑和可保存，系统不自动改选第一项或判定别名无效

#### Scenario: Listing succeeds without pricing or capability evidence
- **WHEN** 供应商返回有效模型 ID，但没有可用价格或经验证的推理和工具调用能力
- **THEN** 既有价格、协议及推理配置保持不变，界面仅表述“已获取模型列表”

### Requirement: Discovery credentials are resolved for the current connection
模型发现 SHALL 使用现有管理鉴权，供应商请求由后端执行。草稿中显式提供的 Key 优先；仅在地址和协议仍对应同一已保存渠道时才可引用其保存的凭据。明确清除凭据 SHALL 禁止回退。新建或复制模型不得仅凭地址相同自动借用其他渠道的 Key；发现响应与错误 SHALL 不暴露凭据或上游原始错误正文。

#### Scenario: Preview a replacement key without saving
- **WHEN** 编辑已有渠道时输入新 Key 并读取列表
- **THEN** 使用新 Key 查询，成功或失败都不修改数据库里的旧 Key

#### Scenario: Stored connection has changed
- **WHEN** 请求仅引用已保存 Key，但草稿地址或协议与其已保存连接不同，或该渠道已被删除
- **THEN** 系统返回可理解的连接错误，不把旧 Key 发往新目标

#### Scenario: Cleared key and copied channel
- **WHEN** 用户清除 Key，或新建/复制模型只有相同地址但未提供本次凭据
- **THEN** 不回退其他渠道的 Key，不自动发送带凭据的发现请求

#### Scenario: Unauthorized request or unsafe upstream error
- **WHEN** 未登录调用发现接口，或供应商错误正文中包含 Key 和鉴权头
- **THEN** 未登录请求被拒绝且不访问供应商；已登录请求只取得脱敏错误，不回显这些敏感内容

### Requirement: Discovery responses belong to the active draft
候选和加载状态 SHALL 关联当前抽屉及当前连接。地址、协议、凭据或编辑对象变化后，旧候选 SHALL 失效，旧请求完成不得覆盖新草稿。自动读取 SHALL 合并同一已完成连接编辑触发的重复请求，不在每次按键或每次渲染时请求；关闭抽屉后 SHALL 停止更新，并保留手动刷新能力。

#### Scenario: Previous endpoint responds late
- **WHEN** 用户由连接 A 切到 B，A 的响应比 B 更晚到达
- **THEN** 界面只展示 B 的结果、错误和加载状态，A 不重新出现

#### Scenario: Typing and rendering do not generate request storms
- **WHEN** 用户持续输入地址或 Key，或只修改模型名、温度与价格导致重复渲染
- **THEN** 输入未完成时不发送请求，与连接无关的字段变更不重复读取；完成连接编辑后可自动读取一次

#### Scenario: Drawer closes during a request
- **WHEN** 用户关闭抽屉后打开另一个模型
- **THEN** 前一个模型的结果不能写入新抽屉，手动输入的模型值也不被异步响应覆盖

### Requirement: Unavailable catalogs have bounded and recoverable outcomes
发现 SHALL 区分正常空列表、列表接口不支持、凭据或权限错误、限流、连接失败以及响应格式错误，并在有限时间和资源内结束。分页未完成或达到容量上限 SHALL 明示为部分结果。失败、空列表和部分结果 SHALL 不阻塞手动填写或保存，不清空当前模型，也不自动反复重试供应商请求。

#### Scenario: Provider does not expose a list endpoint
- **WHEN** 供应商列表接口返回不支持或不存在
- **THEN** 界面提示手动填写，仍可按原流程保存模型，不改用硬编码列表冒充成功

#### Scenario: Empty result differs from failed parsing
- **WHEN** 一个供应商返回合法空数组，另一个返回不符合列表格式的内容
- **THEN** 前者显示没有返回候选，后者显示读取失败，不将两者都呈现为空模型列表

#### Scenario: Rate limited or disconnected
- **WHEN** 请求遇到限流、超时或网络错误
- **THEN** 显示对应可恢复状态并结束加载，用户可稍后手动刷新，原草稿保持完整

#### Scenario: Pagination cannot complete
- **WHEN** 已成功取得部分模型，但后续页失败、游标异常或读取达到上限
- **THEN** 已取得候选可用且标明不完整，不继续无限翻页或宣称“全部模型”

### Requirement: Discovery preserves existing runtime and scoring integrations
模型发现 SHALL 不写入模型配置、启动生成或评分请求、触发交易任务，亦不创建生成用量记录。现有已保存 Jev 列表查询 SHALL 保留鉴权、Jev 过滤和返回结构；新的模型添加界面 SHALL 复用统一发现语义，保留 Jev 供应商选择及用途说明。

#### Scenario: Discovery has no runtime side effects
- **WHEN** 新建模型尚未保存，用户成功或失败地读取列表
- **THEN** 持久配置、运行任务、消息评分及生成用量均保持不变，只有只读列表请求发生

#### Scenario: Existing Jev client still works
- **WHEN** 原调用方按已保存渠道查询 Jev 模型
- **THEN** 返回兼容的模型 ID、名称和描述，权限与错误脱敏要求保持有效
