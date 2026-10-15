# Spec Delta

## MODIFIED Requirements

### Requirement: Execution owns verification and recovery
默认上下文 SHALL 将核验及恢复交给执行层，Agent、聊天和 MCP 不再提供或引导调用原操作查询工具。模型可直接表达新的意图，金融调用在前置阶段按预算核验实际依赖，权威受理 ACK 后及时返回，不以二次 GET 订单/仓位取得返回许可。后续状态 SHALL 由后台按可信范围和原身份核验并自动更新上下文与界面；核验不重发金融写入、提交未尝试替代或续跑旧尾项，不新增策略。当前限制须按真实依赖表达，不能靠历史诊断要求模型查询解锁。

#### Scenario: Direct management after an old rejection
- **WHEN** 旧止损明确未被接受，Agent 根据当前仓位直接请求有效新止损
- **THEN** 系统在本次金融调用的前置阶段核验并处理，不要求 Agent 查询旧操作解锁；新请求获得权威 ACK 后及时返回，后台推进后续事实

#### Scenario: Unknown submission still prevents a duplicate
- **WHEN** 同方向平仓结果仍未知，新请求依赖该剩余数量
- **THEN** 执行层在本次前置核验内保留具体限制并后台按原身份核验，默认上下文不指示 get_trade_operation/get_operation，不通过重复平仓或续跑尾项解除未知

#### Scenario: Accepted conditional protection updates without a second tool
- **WHEN** 原 close/protect 已返回 submitted、waiting_trigger 和有证据的 filled_amount=0
- **THEN** 上下文和界面准确说明已受理未成交，后续触发和成交由后台自动更新；模型不二次取验，不因受理显示 completed

#### Scenario: Missing post-submission evidence is an execution responsibility
- **WHEN** 权威提交 ACK 已保存但后续订单或仓位事实尚未读取
- **THEN** 原调用仍按 ACK 返回事实，后台处理缺项并记录真实观察时间，不因等待额外 GET 延迟返回或把缺项变成模型手动恢复清单

### Requirement: Default tool results preserve compact actionable facts
新旧默认模型工具结果 SHALL 精简逐项事实，保留索引、父子操作/订单及必要客户端身份、操作/订单状态、数量模式与已知成交、具体失败阶段或依赖阻塞，不输出操作查询指引。单项 SHALL 不超过4096字节、最多20项整批不超过32768字节，以序列化 UTF-8 计量；不重复 previous_assessment、规则或 blocker 树。整批账户摘要 SHALL 最多一次且只含请求的当前信息。完整原始 JSON 和后续证据 SHALL 保留于授权 HTTP/UI 详情，不逐项嵌入或删除原事实。

#### Scenario: A twenty action paper batch
- **WHEN** 二十项 paper 动作都有已保存回执，调用者请求账户摘要
- **THEN** 默认整批 UTF-8 不超过32768字节，包含二十项精简真实结果与一份末尾摘要，不出现二十份账户快照或重复评估树；必要身份和未知事实不被省略成成功

#### Scenario: Failed and unexecuted steps remain distinguishable
- **WHEN** 第一步撤单待核验，默认停止策略使后续开仓未执行
- **THEN** 模型结果区分 cancel pending 与 open not_executed，保留原身份并说明系统后台核验，不把整批写成全部失败/已提交，不要求操作查询

#### Scenario: Complete evidence requested explicitly
- **WHEN** 用户在现有权限下通过 HTTP/UI 明确请求原操作完整详情
- **THEN** 返回持久化原始回执及增量证据和真实观察时间，不恢复金融写入；该详情不是另一个模型/MCP 工具，不自动进入默认模型结果

#### Scenario: A noisy legacy receipt is projected within the item budget
- **WHEN** 旧金融回执包含重复 previous_assessment、blocked_actions、规则及相同 blocker
- **THEN** 默认单项 UTF-8 不超过4096字节，只保留当前有影响的去重限制、真实状态和必要身份；原始 JSON 不改写，默认结果不附 get_trade_operation/get_operation 调用步骤

### Requirement: Detailed ledgers and diagnostics are available on demand
详细成交、费用、归属缺项、周期、同步覆盖及原操作回执 SHALL 通过用户主动访问且受现有任务、账户、namespace/principal 权限约束的 HTTP/UI 详情提供，保留原来源与真实观察时间。详情 SHALL NOT 注册为模型/MCP 工具或成为交易后的必调用步骤，读取不得恢复金融写入。默认记忆、收益摘要与例行决策输入 SHALL 不附完整资料；详情与摘要使用同一证据口径和时间，不重复计算盈亏，不改写历史 raw 记录。

#### Scenario: Expand performance details
- **WHEN** 用户主动打开收益或账本的授权 HTTP/UI 详情
- **THEN** 可查看原成交、费用、归属、周期和同步证据，默认收益区保持三类指标；详情不自动复制到下一次记忆正文，也不构成模型操作查询工具

#### Scenario: Historical snapshot is expanded
- **WHEN** 用户通过授权 HTTP/UI 查看旧记忆关联历史快照
- **THEN** 标明历史时间并保留原证据，不替换为实时账户状态或覆盖当前持仓，不因打开详情提交、撤销、修改订单

#### Scenario: Audit access cannot disclose another principal
- **WHEN** 用户请求不在其任务、账户或原 namespace/principal 授权范围的操作原文
- **THEN** 现有权限边界拒绝访问，精简回执及详情不泄露其他主体身份；移除操作查询工具不扩大 HTTP/UI 审计权限
