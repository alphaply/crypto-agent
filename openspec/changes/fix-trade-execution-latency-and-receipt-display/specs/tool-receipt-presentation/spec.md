## MODIFIED Requirements

### Requirement: Default tool results use concise cards
前端 SHALL 对交易工具结果逐项直接展示操作类型、标的、方向、数量、价格及本次操作成功／失败，不要求点击展开。成功 SHALL 表示本次指令已获权威接受，不表示挂单已成交；明确拒绝或明确未执行 SHALL 不显示成功。交易结果区 SHALL 不展示重复数量、成交明细、内部身份、核验状态或内部原因树；缺失字段不猜测或填默认值。非交易查询工具 SHALL 保持原简短摘要与按需读取行为，模型和持久化回执保留原真实状态。

#### Scenario: Trading receipt contains repeated nested evidence
- **WHEN** 交易结果包含父子回执、账户摘要和重复核验字段
- **THEN** 直接显示每项的六项精简内容，不重复展示父子结果；已接受但未成交的限价单显示操作成功，不显示已成交或要求展开

#### Scenario: Generic tool returns a long text or records
- **WHEN** 非交易查询工具返回长文本或多条记录
- **THEN** 卡片仍展示简短摘要，长文本与完整记录在展开后读取，不强行转换成交易六字段

#### Scenario: Multiple operations have different outcomes
- **WHEN** 同一批次包含成功提交、明确失败和未执行项
- **THEN** 按原动作顺序直接展示各项，成功项显示成功，明确失败和未执行项显示失败，不用单个父状态覆盖全部子项

#### Scenario: Requested quantity differs from filled quantity
- **WHEN** 限价入场请求数量为 0.05、价格为 2460，成功受理时已成交数量为 0
- **THEN** 该项显示数量 0.05、价格 2460 和成功，不以已成交 0 替代委托数量，不再重复列出另一份数量或成交情况

#### Scenario: A submission has no authoritative outcome yet
- **WHEN** 原回执为执行中或提交结果未知，尚无足以判定成功或明确失败的证据
- **THEN** 不生成虚假的成功／失败终态，也不展示核验树、JSON 或审计入口；后台继续沿原身份更新，取得明确结果后使用同一精简行展示

### Requirement: Complete results remain available on demand
非交易工具 SHALL 保留按需展开完整结果的既有行为。交易工具结果区 SHALL 移除完整结果、原始 JSON 和审计展开入口，不显示调用参数 JSON、失败原因树或核验详情；本次调整 SHALL 不删除后台原始请求及执行证据，不更改已有授权审计接口和模型回执。旧版文本结果 SHALL 保持可读，不能因缺少结构化字段编造操作或成功状态。

#### Scenario: User expands a failed result
- **WHEN** 用户查看一条失败的交易结果
- **THEN** 直接看到其可取得的操作字段和失败结果，不出现完整结果、原始 JSON、核验或审计展开入口；已有后台证据不被删除

#### Scenario: Legacy result is plain text
- **WHEN** 结果是旧版纯文本而非 JSON
- **THEN** 前端保留可读精简文本，不展示原始 JSON、不因解析失败崩溃或编造结构化字段；中英文与窄屏界面均可阅读

#### Scenario: A trading call has a matching parameter message
- **WHEN** 运行过程或聊天中，交易工具结果同时关联一条调用参数消息
- **THEN** 该交易在页面不再通过调用参数展开暴露原始 JSON，展示收敛到同一操作的精简字段，不重复显示一份复杂结果

#### Scenario: Authorized evidence is requested outside the result card
- **WHEN** 已有授权客户端通过既有审计接口读取原交易证据
- **THEN** 原权限、请求、回执及增量证据仍保留，不因删除结果区入口而删除数据或新增交易写入
