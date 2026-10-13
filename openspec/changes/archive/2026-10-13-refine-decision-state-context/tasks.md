# Tasks

## 1. 按身份读取执行证据并修正时间语义

- [x] 1.1 在 `trade_recovery.py` / `trade_evidence.py` 增加按可信任务、账户、标的及原操作身份批量读取最新证据的接口，支持批次父子关联和窗口外引用；用临时库验证超过 20 条历史、跨账户隔离、相似数量不同事件及 not_executed 尾项。
- [x] 1.2 在回执投影与查询中分开实际观察、查询及本地生成时间，局部读取失败时不复用旧允许/禁止建议；扩展 `tests/test_independent_receipt_recovery.py`，验证 local-only 不伪装交易所刷新、原已确认执行事实不降级。
- [x] 1.3 为后台回执投影添加可恢复的证据水位/指纹，按未完成关联与实质变化更新；扩展恢复及并发测试，验证重复维护不增加已结束历史修订，completed + cleanup.pending 仍推进且重启不漏项。
- [x] 1.4 更新 `docs/TRADE_RECEIPTS_AND_RECOVERY.md` 的时间、维护与查询语义，并运行该组恢复、回执及 `tests/test_trade_evidence_concurrency.py` 测试；确认示例与兼容字段一致，无新增金融写入。

## 2. 构建当前交易状态与相关异常视图

- [x] 2.1 新增共享当前状态加载/纯投影/文本渲染模块，规范持仓、有效订单、fixed/all、覆盖及分项证据来源；新增 `tests/test_decision_state_context.py`，验证 REAL/mock 等价语义、母子身份、订单去重和未知量不补零。
- [x] 2.2 复用订单分类与动作依赖形成相关异常集合，覆盖活跃周期、旧 all、未知提交和终态缺成交；用“历史第 21 条仍未决”“无关 Hedge 方向”“One-way 共享仓位”场景验证既不漏报也不形成全局禁令。
- [x] 2.3 从 `exit_context.py` 的当前状态路径移除 `plan.error` 驱动的全局待核验，保留必要覆盖与数量冲突；扩展 `tests/test_independent_exit_context.py`，验证正常等待触发和无执行影响拒绝不降级、读失败与已确认无异常不同。
- [x] 2.4 在 `docs/TRADING_WORKFLOW.md` 记录当前状态、数据缺口和按需历史的展示边界；运行本组状态测试及 `tests/test_independent_admission.py`，确认展示没有引入第二套交易准入。

## 3. 修复每轮及滚动记忆的最新证据输入

- [x] 3.1 修复 `memory_updates.py` 与 `database_memory.get_summary_logic_between` 的可信 config/symbol 传递，并统一记忆证据读取；在 `tests/test_turn_memory_and_review.py`、`tests/test_memory_workflow.py` 中捕获实际模型输入，验证两条入口都收到同身份最新结果。
- [x] 3.2 调整 `decision_record.py` 的记忆消费路径，使最新回执进入实际执行证据而非仅进入 `messages.current_receipt`；验证原始 decision_json/首次回执未改写、父子批次状态准确且本轮来源没有重复拼接。
- [x] 3.3 将旧记忆投影执行引用纳入证据集合，不受四小时或最近页限制；验证旧 unknown 已解决、平仓已完成但清理未决、0.37/0.30 不同事件、账户变更及读取失败的不同结果。
- [x] 3.4 更新 `summary_prompts.py`、记忆整理规则及 `docs/PROMPT_DESIGN.md` 的证据优先级和职责；运行上述记忆测试，验证有效策略条件与历史教训仍保留，模型不负责解锁或证明成交。

## 4. 建立可追溯的策略文字投影与旧记忆过渡

- [x] 4.1 添加 `decision_context_projections` 本地表及版本化载荷，保存来源 ID、内容哈希、策略文字、执行引用、未关联诊断和处理状态；新增迁移测试，验证旧库/空库重复初始化、原文完整、人工原地编辑使旧投影失效。
- [x] 4.2 扩展适用模式下的 `memory_agent.py` 响应与记忆幂等结果持久化，同一次正常整理生成新记忆和近期摘要投影；更新 `tests/test_memory_agent.py`，验证仅一次正常模型调用、来源/范围校验、执行引用不能赋予权限，以及原模式兼容。
- [x] 4.3 在既有记忆 worker 中处理缺失投影的批量后台转换，幂等键包含格式版本和源哈希，无新摘要时也可处理；扩展 `tests/test_memory_update_retries.py`，验证退避上限、租约、重启、重复任务及旧完成结果不会跳过新投影。
- [x] 4.4 更新 `decision_context.py` 读取 ready 策略投影并按当前证据解析执行引用，与统一异常去重；验证记忆模型失败或排队时已解决限制仍在下一次读取消失，最近三轮摘要不能复活旧禁令。
- [x] 4.5 实现无投影/投影失败/人工编辑后的明确回退，只保留有效策略投影及当前状态，不注入旧混合正文；覆盖“等四小时收盘确认”与“旧 unknown 禁止加仓”并存场景，验证前者保留、后者随证据退出且无字符硬截断。
- [x] 4.6 更新 `docs/TRADING_WORKFLOW.md` 的记忆格式、迁移暂缺策略背景和失败行为，以及 `docs/PROMPT_DESIGN.md` 的策略/执行分工；运行投影、记忆完整性和重试测试，确认原文审计、人工编辑及现有配置模型仍可用。

## 5. 接入定时决策与绑定任务聊天

- [x] 5.1 在 `agent_graph.py` 使用共享状态和策略投影，取消完整 `trade_operation_receipts` 默认注入并合并重复订单段落；用合成 14 条回执样本验证默认 prompt 只包含当前单据和相关异常，保留市场、消息及策略背景。
- [x] 5.2 在 `chat_graph.py` 移除重复 `recovery_context_text` 默认注入并共享当前视图；扩展 Agent/Chat 集成测试，验证相同证据呈现一致、本轮新工具事实不被旧快照覆盖、Chat 审批和原始消息保留。
- [x] 5.3 保持四个现有上下文占位符兼容并避免旧摘要回流；扩展 `tests/test_decision_memory_context.py`、`tests/test_retired_memory_context.py` 和 prompt 解析测试，验证自定义模板不重复注入、不改用户源文件，其他交易模式不被错误套用。
- [x] 5.4 同步 `README.md` 和 `docs/PROMPT_DESIGN.md` 的正常/异常 prompt 示例，运行本组集成测试与 `tests/test_scoped_trade_rounds.py`；确认显式历史查询、本轮回执、MCP 授权和批次停止/依赖语义保持。

## 6. 跨链路验收

- [x] 6.1 用 fake exchange、临时库和固定模型响应完成 `design.md` 验收矩阵：加入 100 条无关已完成历史后当前状态文本不变；后台解决旧异常后，即使记忆失败，Agent/Chat 下一轮仍不接收旧禁令；真实 unknown 不增加金融写入次数。将场景结果和必要 prompt 对照记录到本 change 的 `verification.md`。
- [x] 6.2 运行受影响的状态、恢复、记忆、并发、MCP 和提示词测试及 `npm run build --prefix frontend`，检查 `git diff --check` 与 `openspec validate refine-decision-state-context --strict`；记录通过结果或具体阻塞。验证使用离线夹具，不调用真实交易或模型连接烟测来替代行为验收。
