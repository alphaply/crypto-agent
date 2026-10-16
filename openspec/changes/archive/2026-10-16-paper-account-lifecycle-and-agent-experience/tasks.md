# Tasks

依赖：账户基础 → 转账/计息 → 重置与榜单 → 目录和 QQBot 集成。新闻、回执界面和真实账户共管可独立推进，共管内部按物理资源协调 → 修订/维护 → 去除独占 → 迁移验证的顺序实施。每组保留无关本地修改；不将本变更的完成等同于另一在途变更完成。

## 1. 新闻固定时刻轮询（news-polling-schedule）

- [x] 1.1 接续已有 news_service.py、news_schedule.py 及前端定点说明的本地修改，核查槽位去重与既有租约；以 tests/test_news_schedule.py 验证北京时间 12:30、13:00、13:30、重复 tick 及错误后下一槽位不漂移。
- [x] 1.2 更新 docs/NEWS_PIPELINE.md 与配置说明，手动核对 NewsPipelineMonitor 的 started_at 与定时槽位，说明手动/公告触发仍可额外发生；验收不误改 Agent 自身调度、不新增超时或补跑规则。

## 2. 账户生命周期与有符号现金（paper-account-lifecycle、paper-ordered-trading）

- [x] 2.1 在 backend/storage/migrations/ 添加生命周期、周期、次数及后续转账/计息所需的显式迁移，更新对应持久化适配；以隔离 PostgreSQL 验证新旧账户可读、迁移重复执行安全，原资金、操作身份及审计不变。
- [x] 2.2 区分账本有符号现金与正金额输入，修正 snapshot、可用余额、银行及排行榜读取；在 tests/test_paper_account_lifecycle.py 验证负现金可展示而负数/NaN/Infinity/布尔金额仍拒绝，未知估值不判零。
- [x] 2.3 实现独立破产状态及共享提交准入，覆盖批次、旧单项入口、匹配、条件退出与后台金融维护；以破产前后触发价相同及状态并发测试验证全部旧订单不再成交、用户金融操作拒绝、只读与计息可执行。
- [x] 2.4 更新账户状态 API、后台状态/周期展示及账户规则文档；验收普通 enabled 与破产状态可区分，查询破产账户不会自行恢复操作或触发成交。

## 3. 原子双向转账（paper-account-lifecycle）

- [x] 3.1 实现同 namespace 双账户转账、稳定锁序、操作指纹、双侧事件和资金/资本调整；tests/test_paper_account_lifecycle.py 覆盖用户互转、用户与 bot 双向转账、借款资金转账、破产收款和破产禁止转出。
- [x] 3.2 实现正有限金额、精度及扣除预留后的余额核验；在同一测试文件覆盖负数、零、非有限值、超精度、余额不足、错误身份/namespace 与昵称冒充，失败不改变任一方余额。
- [x] 3.3 验证重复请求、变更收款人复用 ID、并发转出/下单、双侧写入故障回滚及互转锁序；确认无重复入账、半边转账或可用现金透支，并同步转账契约文档。

## 4. 统一银行与每日扣息（paper-account-lifecycle）

- [x] 4.1 扩展 PaperSettings、management.py、配置导入导出及 PaperTradingPanel 的借款开关、统一额度和日利率，移除与统一政策冲突的有效账户额度覆盖；以 API 测试及后台保存/重载验证实际生效，更新设置说明。
- [x] 4.2 持久化本金区间、利率生效记录和结算身份，按北京时间每日零点单利结算；tests/test_paper_account_lifecycle.py 覆盖首日折算、借还款区间、日中调息不追改、按精度结算、非复利与旧零利息账本升级。
- [x] 4.3 将计息接入现有维护生命周期，覆盖无仓位/无订单/已破产账户；验证重复执行、重启跨日恢复、日界并发、3U 扣 10U 得 -7U 且进入破产，以及破产后继续扣息，更新每日计息运维说明。

## 5. 主动下海与新周期隔离（paper-account-lifecycle、paper-ordered-trading）

- [x] 5.1 实现显式 reset 事务，检查本人、破产用户账户及幂等身份；tests/test_paper_account_lifecycle.py 验证现金、债务、仓位、库存、挂单、预留、账户参数及收益率恢复初始状态，审计保留且累计次数只加一次。
- [x] 5.2 将 account_epoch 贯穿批次、子操作、匹配及计息提交边界；验证 reset 与撮合/转账/扣息并发，旧操作重放和晚到事件只保留历史事实、不消耗新资金或改变新周期次数。
- [x] 5.3 更新后台下海次数/当前周期展示及 reset 文档，验证 create/update 不隐式重置、非破产用户和 bot 不可下海、无次数上限且不增加审批步骤。

## 6. 收益率榜与下海榜（paper-account-lifecycle）

- [x] 6.1 扩展 paper_competition.py、榜单 HTTP 接口与 PaperDataPanel，提供 return_pct/reset_count 两种降序榜单；在 tests/test_paper_account_lifecycle.py 验证旧 pnl 排名返回明确指标错误、后台与公开榜口径一致。
- [x] 6.2 将转账资本调整、利息损益和 reset 新周期纳入计分；验证无交易互转不产生利润、贷款不刷收益、手续费/利息影响收益、重置 10000U 后赚 2000U 显示 +20%，旧轮损失不带入。
- [x] 6.3 保留参赛偏好、公开隐私、分页和估值不完整处理，下海榜使用累计次数；以隐私/不完整估值场景和后台查看流程验证，并更新榜单说明。

## 7. MCP 目录和金融入口衔接（model-tool-catalog）

- [x] 7.1 注册 paper_transfer，扩展 configure_paper_account(action=reset) 与榜单指标，更新 canonical_tools、兼容银行说明和 get_trading_context 的政策/状态；以 tests/test_mcp_canonical_catalog.py 和契约测试验证可写 Paper 十一项、只读七项、live 无 Paper 工具。
- [x] 7.2 为转账、利息、破产、周期和 reset_count 更新必要精简字段投影及授权审计读取；验证真实状态、金额单位、错误与未知事实可读，原幂等身份保持，不增加操作查询工具或更改另一变更的模型文本契约。

## 8. 简短工具回执卡片（tool-receipt-presentation）

- [x] 8.1 在 DecisionMessages、ToolResultContent 和运行/聊天消费点实现共享摘要卡片，移除重复核验块与默认全字段树；用实际脱敏返回样本验证纯文本、嵌套 JSON、多项交易、长列表、错误及 pending/unknown 均可摘要并展开。
- [x] 8.2 以桌面及窄屏、中英文手动核对名称/状态/简短结果，交易卡片区分提交与成交，失败原因直接可见，完整 JSON/审计可展开；保留截图并运行 npm run build --prefix frontend，更新相关界面说明。

## 9. 人格、技能和自主交易任务指令（qqbot-autonomous-trading）

- [x] 9.1 更新 docs/qqbot/人格prompt.txt 和 qqbot-paper-trading/SKILL.md，明确 bot 自有参数自主决策、用户委托只作用本人账户、黑话不懂就问、可自有接济与用户主动下海；以提示词场景核对无逐笔自有授权阻碍、无主动邀请重置及他人资金挪用。
- [x] 9.2 提供对接现有外部自主交易定时任务的指令片段，并重建仓库内 QQBot skill 分发包；检查包内文件与源一致，验证提示包含查账户/行情、执行或具体观望原因，不创建第二个调度器、不强制下单。
- [x] 9.3 协调人格、skill、工具说明和示例的沉浸表达，保留内部执行事实；以成交/挂单/未知、主动接济、用户询问提现、含糊金额和仅聊天未下海等场景验证，无模拟免责声明、假成交或未经主动请求的 reset，并更新部署接入说明。

## 10. 全局取消管理者独占（visible-position-management 及相关五项增量）

- [x] 10.1 将 visible_management/position_management 的长期 owner 归属与物理资源执行协调拆分，保留跨进程锁、版本、权限和操作幂等；扩展 tests/test_visible_position_management.py，验证不同 MCP profile、principal 与内置 Agent 可共管，read-only、停用、账户/市场/标的越界仍拒绝。
- [x] 10.2 在保护计划、现货退出和后台维护中引入共享目标有效修订及重复维护协调；以 A 设止损→B 成功修改/撤销→A 旧 worker 晚到的测试验证不恢复旧参数，失败/unknown 不取代成功意图，旧提交按原身份核验。
- [x] 10.3 全局移除默认 visible_first_claim 及旧 exclusive_scope 的排他准入，覆盖实盘开平仓、撤改单、保护和兼容入口；用模拟交易所及隔离数据库验证 mcp:connection-1791515539173 的旧 owner 不阻止其他合法连接，跨连接管理已有订单不需释放或交接，也不重放旧交易。
- [x] 10.4 保留 Hedge 分向、One-way 净仓及跨报价现货基础资产的共享协调；并发测试验证多个连接不会超卖库存、重复预留、超量平仓或复制基线，原私有操作回执不会因共管泄露。
- [x] 10.5 编写保留事实的 claims/配置/维护迁移并更新受影响管理测试和 browser_exclusive_management 场景；验证已有活跃订单、原未知提交、来源与预留不丢失，重启后不恢复长期独占、不要求清理单个连接 owner。
- [x] 10.6 更新管理配置/状态 UI、工具说明、决策上下文及 docs 中的独占/先释放/只读提示，保留实际权限与并发限制说明；以跨连接查询和金融样本验收能力与执行一致，文档说明最新成功明确修改生效及后台不恢复旧参数。

## 11. 跨模块验收

- [x] 11.1 在隔离数据库串联借款→用户/bot 互转→扣息负现金→冻结旧订单→破产收款→本人主动下海→旧请求重放→新周期交易和双榜；验证资金事件、收益率与次数一致，测试不访问真实交易账户。
- [x] 11.2 运行受影响 Paper/MCP/调度/实盘共享管理测试及前端构建，验证授权、BN-only、保护周期、共享预留与幂等回执未回归；记录实际命令、结果、界面截图和 QQBot 外部接入步骤，明确外部部署尚未验证的部分。
- [x] 11.3 核对全部十一份增量规范与完成任务一致，运行 openspec validate paper-account-lifecycle-and-agent-experience --strict，确认仅本变更相关文件纳入后续评审，不覆盖其他本地修改。
