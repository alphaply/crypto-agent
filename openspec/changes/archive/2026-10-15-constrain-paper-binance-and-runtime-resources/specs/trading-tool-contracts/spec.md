# Spec Delta

## REMOVED Requirements

### Requirement: Callers can query an original operation without trading writes
**Reason**: 用户要求交易执行后不再由模型二次查询原操作；get_trade_operation/get_operation 及其旧工具适配器从 Agent、聊天和 MCP 移除。
**Migration**: 从全部注册、模式绑定、提示、兼容路由和可直调工具适配器删除操作查询入口。系统在金融调用内完成必要前置核验，受理后由后台按原身份自动核验；用户可通过受权限保护的 HTTP/UI 详情查看原始审计。旧请求、指纹、原始回执及身份不改写，不新增替代详情/查询工具。

## MODIFIED Requirements

### Requirement: Reconciled evidence reaches all receipt consumers
已核验操作证据 SHALL 自动关联到原操作、当前 Agent/聊天状态、界面、记忆证据及同 ID 重放，不依赖模型操作查询。现货买入与退出 SHALL 后台只读核验订单，在确认后更新 FILLED/PARTIAL/CANCELLED、均价及成交时间。默认输入 SHALL 使用当前相关投影，用户按需通过授权 HTTP/UI 读取完整历史。恢复 SHALL 按原请求意图与原身份判断，原请求、首次回执和原始 JSON 保留，后续证据增量关联。新周期不丢失历史身份；其他清理未知不能覆盖已确认成交，旧快照不能覆盖新事实。记忆关联 SHALL 使用最新证据但不写入执行诊断，不将未执行项推定为已执行。

#### Scenario: Background reconciliation resolves a close
- **WHEN** 后台核验到原平仓已成交且清理完成
- **THEN** Agent/聊天下一次默认状态和界面自动更新，程序记忆证据关联解决结果；同 ID 重放返回已核验事实而不再次提交，授权 HTTP/UI 可查原回执，无需 get_operation

#### Scenario: Spot order fill reconciles submitted receipt
- **WHEN** 已返回 submitted 的现货买单随后在交易所成交
- **THEN** 后台按原订单/客户端身份只读核验并自动更新 FILLED、成交数量、均价及时间，模型不发起第二次操作查询

#### Scenario: Canceled source does not prove successful replacement
- **WHEN** edit 的旧单撤销已确认，但替代单被拒绝或结果未明
- **THEN** edit 按替代阶段返回 failed 或 unknown/pending，不能仅凭旧单 canceled 宣告 edit 成功；后续事实由后台核验，不要求模型查询或重发

#### Scenario: Completed cycle is replaced
- **WHEN** 新周期创建后用户通过授权 HTTP/UI 查看旧周期某次待核验或已完成操作
- **THEN** 按旧身份返回原事实及增量证据，不关联新周期或丢失历史；旧持仓与旧准入建议不进入当前默认状态，详情读取不产生交易写入

#### Scenario: Memory gets the reconciled result rather than only a display overlay
- **WHEN** 原始回执为 unknown，后台已将同身份核验为 completed，记忆程序消费关联证据
- **THEN** 程序取得最新事实与原回执关联，既更新当前视图也保留完整审计；纯策略记忆正文不复制操作 ID、恢复步骤或执行诊断

#### Scenario: An unresolved item is older than the recent receipt page
- **WHEN** 当前异常引用的原操作不在最近回执页内或属于旧批次子项
- **THEN** 后台在原授权范围按身份读取和核验，不因分页丢失真实未决问题，不要求模型查旧 ID；原审计仍可通过受权 HTTP/UI 查看

### Requirement: Admission receipts provide scoped decisions and recovery instructions
交易回执及后台状态 SHALL 提供版本化准入事实，区分允许、带告警允许、拒绝与未评估，保留动作/目标范围、证据时间、具体阻塞及简短告警。后台关联 SHALL 使用调用者可访问的原身份，默认结果说明系统核验状态，不提供模型操作查询入口；用户审计走授权 HTTP/UI。建议不授予新交易权，执行仍重新核验。blocked_actions SHALL 不把目标限制扩大为全局禁止，空列表不表示全面允许，相同动作和证据使用一致结论。

#### Scenario: Diagnostic error is only a warning
- **WHEN** 旧提交已确认失败且无执行影响，新请求全部实际依赖满足
- **THEN** 新请求允许或带告警允许，保留旧拒绝原因但不返回 trade_recovery_required，不要求模型先查询解锁

#### Scenario: A missing order ID still has a usable recovery identity
- **WHEN** 真正未知提交尚无交易所 ID
- **THEN** 保留原 operation_id、客户端身份、具体缺项及系统后台核验状态，不只返回空订单 ID，也不输出二次操作查询指令

#### Scenario: MCP recovery identity is accessible to the caller
- **WHEN** MCP 回执引用阻塞的其他操作
- **THEN** 只返回当前 principal/profile 可访问的外部身份和具体后台核验状态，不泄露内部身份或误映射到当前 ID；映射不可得时说明缺项，不提供模型查询入口，用户可按原权限查看 HTTP/UI 审计

#### Scenario: Admission changes between query and write
- **WHEN** 后台观察或正常记录读取后发生新成交、其他写入或归属变化
- **THEN** 新金融调用基于当前证据重新判断，拒绝时指出变化及相关限制，不相信缓存允许标记，也不要求另一个模型查询步骤

### Requirement: Compact model receipts retain actionable execution evidence
新旧金融入口的默认模型回执 SHALL 保留父子身份、动作索引、status/order_status、有效及必要旧订单/客户端身份、数量模式、请求/已知成交/剩余数量、edit_method、错误码、失败阶段与具体依赖阻塞。单项 SHALL 不超过4096字节、最多20项的整批不超过32768字节，按序列化 UTF-8 计量；去除重复别名、previous_assessment 树及重复规则/blocker，不输出操作查询指令。必要身份不得截断，准入须保证结果可在预算内表达。完整原始回执及后续证据 SHALL 保留于授权 HTTP/UI 审计；不把 submitted 当成交、隐藏部分成功或泄露其他 principal 身份。

#### Scenario: Compact timeout retains a recovery target
- **WHEN** v3 子项提交后超时且已有客户端身份、尚无交易所订单 ID
- **THEN** 精简回执保留父子 operation_id、客户端身份、unknown/pending 区别及系统后台核验状态，不给 get_trade_operation/get_operation 指令、不只返回失败字符串或空身份

#### Scenario: Compact edit keeps replacement outcomes separate
- **WHEN** edit 已确认撤旧，但替代单被拒绝
- **THEN** 精简回执保留旧身份、当前有效或替代客户端身份、edit_method、明确替代失败及错误码，不因旧单 canceled 宣告 edit completed

#### Scenario: Full evidence remains accessible after projection
- **WHEN** 用户在现有权限范围通过 HTTP/UI 请求原操作完整详情
- **THEN** 返回完整原始回执、增量证据及真实观察时间；默认投影不删除请求、成交、失败阶段或身份，详情不是模型/MCP 工具且不恢复金融写入

#### Scenario: A legacy conditional close has a compact factual acknowledgement
- **WHEN** 旧 v2 close 回执包含 repeated previous_assessment、规则 blocker 且条件单已受理尚未触发
- **THEN** 默认单项 UTF-8 不超过4096字节，原始回执保持完整；展示 submitted、waiting_trigger 和有证据的 filled_amount=0，不写 completed 或指示二次模型查询

#### Scenario: A full batch respects the aggregate result budget
- **WHEN** 最多二十项动作返回订单身份、异常和一次可选账户摘要
- **THEN** 整个默认序列化 UTF-8 结果不超过32768字节，保留每项真实状态与必要身份且不逐项复制账户、规则或 blocker；不能靠截断原身份或伪造成功满足上限

## ADDED Requirements

### Requirement: Financial calls return authoritative acknowledgements without secondary model queries
所有模式及新旧兼容金融入口 SHALL 在一次调用内完成必要的前置权限、目标、仓位、管理及动态准入并执行 open/close/edit/cancel/protect。获得权威受理 ACK 后 SHALL 及时返回 submitted，不为取得返回许可再次 GET 订单/仓位或等待未来成交。ACK 已证明本次动作完成时可报告 completed；已确认成交直接保留，缺证据不能填假零。未确认写入保留 pending/unknown 和原身份。正常后续意图 SHALL 在自身调用内核验所需依赖，不强制模型二次取证。

#### Scenario: An accepted limit entry returns before filling
- **WHEN** open 的限价入场获得有效订单 ACK 且尚未成交
- **THEN** 原调用直接返回 submitted 与订单身份，不追加成交轮询或为返回再次 GET 订单/仓位，模型不调用操作查询

#### Scenario: Conditional protection is accepted but waiting
- **WHEN** close/protect 的条件退出获得权威受理 ACK，订单仍为 waiting_trigger 且确认未成交
- **THEN** 返回 submitted、waiting_trigger、filled_amount=0 及必要身份，不因受理报告 completed，也不等待触发或要求模型补查

#### Scenario: A new intent requires current position evidence
- **WHEN** 新的 close/edit/cancel 请求在提交前依赖最新仓位、目标或相关原提交事实
- **THEN** 执行层在本次调用内按预算完成必要只读核验，满足条件才提交；该前置核验不能被误当作禁止的 ACK 后二次取证

#### Scenario: The acknowledgement already proves the requested outcome
- **WHEN** 本次 ACK 本身可靠证明实际成交或指定 edit/cancel 动作已完成
- **THEN** 直接报告相应已确认事实与真实状态，无需为了返回再次 GET；清理或累计成交仍未知时单独表达，不虚构完成范围

#### Scenario: A removed operation-query adapter is called directly
- **WHEN** 调用者尝试 get_trade_operation/get_operation 或其旧兼容工具入口
- **THEN** 注册和可直调适配器均不再提供该能力，入口明确不支持且不读取交易所或产生金融效果；后台内部核验服务及授权 HTTP/UI 审计仍可用

### Requirement: Background verification advances only original attempted actions
系统 SHALL 在后台按可信任务/profile、账户、标的及原父子操作/订单/客户端身份只读核验已尝试项，自动更新上下文、界面和增量证据，不要求模型调用操作查询。账户作用域不能替代 config_id；后台与受权审计须保留 caller/principal 和接收方已接受的有限恢复边界。核验 SHALL NOT 重发原金融写入、提交撤建替代、续跑未尝试尾项或新增策略。明确拒绝和未执行项不伪造为未知提交；取证失败保留已有已确认事实及具体缺项。

#### Scenario: A submitted order fills after the model call returns
- **WHEN** 已返回的原订单后续成交且后台取得同账户、同原身份的可靠证据
- **THEN** 当前上下文和界面自动更新成交数量、价格及时间，首次原始回执保留；模型无需第二次查询，核验不新增订单

#### Scenario: A lost acknowledgement is recovered by its original identity
- **WHEN** 原提交结果未知但交易所存在匹配原客户端身份的订单
- **THEN** 后台恢复同一操作证据并更新状态，不换 ID 重发、不登记为另一笔外部操作，不用 account_scope 充当 config_id

#### Scenario: An earlier accepted action has an unattempted tail
- **WHEN** 顺序批次因失败/待核验/未知停止，前项已尝试而尾项 not_executed
- **THEN** 后台只核验原已尝试项；前项完成不会自动提交尾项，同 ID 重放保留原逐项事实和停止语义

#### Scenario: A background read fails after a confirmed fill
- **WHEN** 原成交已核验但后续读取某清理目标失败
- **THEN** 保留已确认成交、来源和时间，单列读取缺项或清理待核验，不退回成交未知或要求模型再平仓/查询
