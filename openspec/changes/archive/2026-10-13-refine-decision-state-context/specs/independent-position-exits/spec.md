# Spec Delta

## MODIFIED Requirements

### Requirement: Visible state is consistent across real and simulated trading
内置 Agent 上下文、MCP 查询和前端展示 SHALL 对齐 fixed/all、订单类型、触发/限价、当前有效身份、真实剩余量、SL 覆盖估计与数量冲突；一致性指事实和语义一致，不要求默认输入包含相同的完整历史回执。未触发或限价止损的挂单覆盖 SHALL 不被描述为保证成交。模拟与实盘 SHALL 采用相同数量语义与周期规则。已终态但成交仍待核验的订单 SHALL 在单独的待核验记录中保持可见，包含身份、最后已知成交、未核实事项及时间，不把最后已知量当成最终成交量或有效保护覆盖。默认决策上下文 SHALL 仅呈现当前相关异常；完整历史保留按需查询。

#### Scenario: Dynamic coverage display
- **WHEN** 账户存在一笔已核验的 stop_market all 与部分定量止盈
- **THEN** 展示止损为“剩余全部”和当前覆盖估计，定量止盈保持自身剩余量，不把重复覆盖相加为更多仓位

#### Scenario: Canceled order still needs fill verification
- **WHEN** 入场单或退出单已确认撤销但最终成交量未确认
- **THEN** 挂单列表可将其排除出活跃单，但 Agent 当前相关异常、MCP 和前端的待核验视图保留该单及原操作关联，不只显示笼统 pending

#### Scenario: Historical receipts and current views have different detail
- **WHEN** 原周期已确认结束且无残留影响，当前为新的已核验周期
- **THEN** Agent 默认状态只展示当前周期事实及相关异常，MCP 和历史页面仍能查询原周期完整回执，两者不把旧周期持仓当成当前持仓

### Requirement: Reconciliation makes bounded progress
系统 SHALL 对撤单及成交待核验执行有界的前台查证和可恢复的后台重试，保留最后尝试时间、下一次核验提示与阻塞证据。单笔失败 SHALL 不妨碍收集其他订单只读证据；清理与撤建写入仍须满足原意图和依赖。暂时网络失败不得清空已确认事实，核验长期失败须可诊断而非无限静默等待。查询、后台维护与写入准入 SHALL 使用一致的证据分类；已确认无执行影响的拒绝不再进入交易所核验队列，历史分类修正不得删除原始记录或重发订单。已终结且无未解决关联的操作 SHALL 不因常规维护反复生成无事实变化的回执修订或刷新其证据时间；新相关证据仍须更新关联结果，按需查询继续可用。

#### Scenario: Cancellation acknowledgement lacks fill fields
- **WHEN** 撤销 ACK 已确认但缺少累计成交，随后原身份查单返回完整终态
- **THEN** 在允许的查证窗口或后续后台轮次完成恢复，不必要求 Agent 再次发起撤单或等到下一次交易请求

#### Scenario: One unresolved order among several
- **WHEN** 某张旧单查证失败，其他订单仍能可靠查询
- **THEN** 更新其他订单的证据并保留具体未解决项，不将所有订单状态一起降级或越过写入依赖

#### Scenario: Legacy rejection is classified from evidence
- **WHEN** 升级后加载一条没有订单 ID 的历史 rejected 记录
- **THEN** 仅在原阶段、请求结果及关联记录证明没有执行影响时解除其限制；证据含糊时保留具体待核验事项，不因 rejected 字样直接放行

#### Scenario: Persistent read failure remains actionable
- **WHEN** 未知提交在多次查证后仍无法确认
- **THEN** 保留最后错误、尝试次数、下次重试和受影响动作；不以超时、订单不存在或单次空仓自动证明未执行，也不阻止无依赖动作

#### Scenario: Unchanged terminal history stays historical
- **WHEN** 连续后台维护期间，一笔已结束操作及关联订单、清理结果均无证据变化
- **THEN** 不反复刷新该操作的回执修订、更新时间或核验时间，默认状态不因维护轮次将它重新列为当前事项

#### Scenario: A completed execution still needs cleanup updates
- **WHEN** 原平仓 completed，但关联清理或晚到入场仍未核实
- **THEN** 后台继续推进相关证据并更新原操作投影，不因为顶层 completed 就停止恢复，也不重复提交平仓

### Requirement: Trading availability explains specific dependencies
Agent、MCP、模拟交易与前端 SHALL 以一致事实表达原操作结果、当前证据、告警及动作限制；默认 Agent 输入仅投影当前相关部分，历史诊断保留按需查看。每个阻塞 SHALL 提供作用范围、具体原因、可用的操作/订单/客户端身份、真实证据观察时间及恢复建议；未知可用性不得显示为全面允许。缓存建议 SHALL 明示为参考，执行仍重新准入；相同证据与动作不得产生相反的查询和执行判断。旧周期或未刷新建议 SHALL 不作为当前禁止或允许结论，单纯历史 error 不构成整个周期待核验。

#### Scenario: Query and immediate admission agree on a rejected stop
- **WHEN** 查询确认原止损无执行影响，随后无证据变化地请求新的有效止损
- **THEN** 查询不显示该拒绝为待核验，执行也不以同一拒绝记录阻塞

#### Scenario: Target-specific warning does not imply every edit is blocked
- **WHEN** 仅订单 A 的修改结果未知，订单 B 的修改依赖可独立核实
- **THEN** 展示 A 的限制及 B 需按目标核验的事实，不用一个全局 edit 禁止标记代替目标范围

#### Scenario: Missing evidence is not an empty allow list
- **WHEN** 当前可用性未核验或查询失败，只保留旧操作结果
- **THEN** 展示待刷新及缺失证据，不把空 blocked_actions 解释为所有新交易可执行，也不将旧 blocked_actions 继续展示为当前封锁

#### Scenario: Historical diagnostic does not change coverage certainty
- **WHEN** 当前仓位和止损单证据已核验，周期保留一条无执行影响的旧错误说明
- **THEN** 当前保护覆盖与异常状态不因该错误字符串降级，旧说明仍可在审计中查看
