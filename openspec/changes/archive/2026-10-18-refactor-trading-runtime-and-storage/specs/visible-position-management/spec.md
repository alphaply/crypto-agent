## ADDED Requirements

### Requirement: All supported historical policies normalize to shared management
已有 strict_owned、exclusive_scope 和 visible_first_claim 配置 SHALL 统一使用当前授权范围共同管理；原始创建者、订单、成交、预算和未知执行身份保留。旧工具入口 SHALL 保留并复用统一内部检查和执行服务。

#### Scenario: Human modifies a position under a historical policy
- **WHEN** 旧配置升级后发生人工加减仓，当前事实可核验且动作获授权
- **THEN** 不因旧策略名称拒绝后续合法管理，不伪造人工成交为 Agent 成交

### Requirement: Attached required protection follows the saved system target
强制止盈止损 SHALL 按系统最新成功保存的目标维护。Agent 与已登录前端均可调整；人工无需二次密码。交易所外部改动不得替换系统目标。独立退出 SHALL 观察外部事实并交给后续 Agent 决策，不自动生成交易意图。

#### Scenario: External cancellation does not clear a required target
- **WHEN** 强制模式仍保存有效止损目标，但交易所上对应保护被人工取消
- **THEN** 后台按最新系统目标维护；后续 Agent 或前端保存的新目标取代旧目标

## MODIFIED Requirements

### Requirement: Later successful explicit modifications govern shared targets
多个系统内调用者修改同一目标 SHALL 按跨进程执行协调和成功提交顺序处理，后续成功保存的明确修改生效。强制止盈止损模式中，交易所外部人工修改或撤销只更新观察事实，不替换系统保存的目标。后台维护 SHALL 使用当前有效目标版本，不恢复被后续明确修改或撤销取代的旧参数。提交前 SHALL 重核当前仓位、订单及共享预留，保留幂等、未知提交核验和只限制实际依赖的规则。

#### Scenario: Another connection changes a stop loss
- **WHEN** A 设置止损，B 随后通过系统成功保存对该止损的修改，A 的旧后台任务继续运行
- **THEN** B 的成功修改保持有效，旧维护不恢复 A 的参数；若旧 worker 已读取旧版本，提交前检测失效并刷新或停止旧意图

#### Scenario: Concurrent reductions cannot reverse a shared position
- **WHEN** 两个已授权进程同时请求减少同一实际仓位，后提交者观察到数量已经变化
- **THEN** 按最新数量及固定量/全平原语义准入，不重复成交、不超量平仓、不因不同连接绕过版本协调
