# Spec Delta

## MODIFIED Requirements

### Requirement: Capabilities and exchange results stay explicit
系统 SHALL 公开本次支持的订单组合并在已知不支持时于交易写入前失败。请求已被交易所拒绝 SHALL 保留真实拒绝原因；请求结果不明 SHALL 区分于拒绝。系统不得把其他交易所或附带保护模式的特殊回退策略用于改变独立退出意图。创建、改单和撤单 SHALL 按实际执行阶段保留订单身份及证据；明确拒绝不能因异常封装变为 unknown，超时或含糊响应不能仅凭异常类型推断为明确拒绝。

#### Scenario: Unsupported adapter cannot emulate all silently
- **WHEN** 一个执行适配器没有原生条件全平能力
- **THEN** 创建条件 all 返回不支持，不转换成固定当前数量、不新增轮询合成单

#### Scenario: Unknown exchange outcome
- **WHEN** 发出条件单请求后网络超时且无法确认是否创建
- **THEN** 返回 unknown/pending 并保留原请求的查询身份，不把它当成明确失败自动换 ID 重试

#### Scenario: Native edit is definitively rejected
- **WHEN** 普通 LIMIT 修改收到可确认的交易所拒绝
- **THEN** 修改操作返回 failed 并保留原单状态和当前身份，不落入只有错误文本的 unknown 回执

#### Scenario: Ambiguous transport error after write
- **WHEN** 请求可能已送达交易所但收到断流、超时或无法验证的响应
- **THEN** 保留提交阶段和原客户端身份，执行查询核验，不根据错误类名将请求标记为未执行

## ADDED Requirements

### Requirement: Cancellation acknowledgement and final fill are separate evidence
撤销确认 SHALL 只证明指定目标的撤销事实；缺少累计成交字段、适配器补出的零值或挂单列表中消失 SHALL 不单独证明零成交。系统 SHALL 按普通单或条件母单/子单的确切身份补查终态，保留最后已知成交量及待核验状态；只有可核实的最终证据才能结束成交核验。

#### Scenario: Algo cancellation acknowledgement has no execution fields
- **WHEN** 条件单撤销 ACK 包含成功码及 algoId，但没有原生成交字段
- **THEN** 记录撤销已确认并查询原条件身份，不能把转换后的 filled=0 当作最终零成交证明

#### Scenario: Untriggered terminal parent is verified
- **WHEN** 后续查询确认母单已撤销且未触发，没有执行子单或与已有证据冲突
- **THEN** 按该接口已验证的终态语义结束母单核验，不因母单天然缺少固定数量字段永久等待

#### Scenario: Trigger wins cancellation race
- **WHEN** 撤单过程发现母单已触发并关联实际执行子单
- **THEN** 对账沿已验证关联查询子单真实成交；母单已撤销或结束本身不能证明子单成交量或剩余委托状态

#### Scenario: Stale open response follows cancellation
- **WHEN** 已确认撤销后查询短暂返回旧 open 状态或倒退累计成交
- **THEN** 保留撤销事实与最后已知累计成交，标记证据冲突并继续核验，不复活旧单、不创建重复替代单

### Requirement: Recovery preserves exit execution intent
恢复过程 SHALL 保留原请求的 fixed/all、基础币单位、持仓方向和 ordinary/algo 通道；只有完成原修改的必要步骤才能延续已持久化撤建意图。已成交平仓不得在恢复时重发，部分成交或目标耗尽不得按原计划量补量。

#### Scenario: Restart after a filled market close
- **WHEN** 市价平仓成交后清理中断并发生进程重启
- **THEN** 只恢复原订单核验和必要清理，不再次提交市价平仓；残仓由 Agent 基于新鲜证据明确决策

#### Scenario: Replacement target consumed by fills
- **WHEN** 条件退出 edit 撤旧期间新增成交已经耗尽目标剩余量
- **THEN** 返回目标已被成交耗尽的确定结果，不创建零量替代单或重新补回原剩余量
