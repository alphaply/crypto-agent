# Proposal

## Why

新闻轮次按上次启动时间滚动导致半小时任务逐渐偏离整点，Agent 回执重复展开影响阅读。QQBot 的自有决策与 Paper 资金生命周期需要补全；live 账户的长期管理者独占又使其他已授权连接或 Agent 被 `Another manager owns this account resource` 阻断。

## What Changes

- 新闻定时轮询按北京时间固定时刻触发；30 分钟配置对应 12:30、13:00、13:30，不随上一轮运行耗时漂移。核查并接续工作区已有定点修改，不覆盖它。
- Agent 工具结果统一使用简短卡片：名称、真实状态、简短结果，交易保留必要方向、数量与成交事实；完整结果点击展开，不重复铺开字段。
- Paper 新增同 namespace 的 USDT 双账户原子转账，覆盖用户互转、用户与 bot 双向转账及 bot 自主接济。借款所得可用资金可转出；拒绝非有限数、零、负数和超过可用余额的转账。
- 银行由后台统一配置借款开关、额度和日利率；按未还本金单利计息，北京时间每天 00:00 收取前一日利息，首日按时长折算，调息从修改时生效、不追改历史。扣息可产生负现金。
- **BREAKING**：净权益不大于零或扣息资金不足时进入破产状态。禁止用户金融操作，已有订单不再成交；仍可查看状态、接受转账和继续扣息，收款不自动解除破产。
- 用户必须主动表达“下海”才可重置自己的破产账户；不由 bot 邀请或自动触发。重置现金、债务、持仓、挂单、账户参数与本轮收益率，恢复配置的初始状态，保留不可变审计历史及累计下海次数。bot 不使用此重置流程。
- **BREAKING**：公开排行榜提供本轮交易收益率榜与累计下海次数榜，不再以绝对盈利金额排名。转账及重置补款不算交易利润，利息计入损益；重置后从新周期计算。
- 区分 bot 自有账户和用户账户的决策授权：bot 在既有市场、资金和杠杆限制内自主分析与执行自己的交易，已有定时任务每轮检查机会，观望需具体理由，不强制交易。用户委托只作用于该用户账户；黑话和数量有歧义时主动询问。
- QQBot 日常以交易者身份按实际回执表达“买入”“成交”，不主动提及 paper、模拟或虚拟；询问真实资金及提现性质时仍如实回答。工具事实、实际执行引擎与权限不因角色措辞改变。
- **BREAKING**：全局取消真实账户资源的长期管理者独占，覆盖默认 visible_first_claim、旧 exclusive_scope、现货、合约、MCP 连接及内置 Agent。不同已授权调用者可开平仓、修改或撤销其他连接创建的订单和保护资源，不要求先释放或交接 owner；不只解锁报错中的单个连接。
- 同一目标以后续成功提交的明确修改为准，旧后台维护不得恢复被取代的参数。保留当前权限、账户/标的/市场范围、审批、短暂跨进程互斥、版本校验、共享库存预留、幂等及未知提交核验；原提交来源与私有回执权限不因共管改变。

## Capabilities

### New Capabilities

- `news-polling-schedule`: 新闻轮询固定北京时间槽位及不漂移行为。
- `tool-receipt-presentation`: Agent 工具结果的精简卡片和按需展开。
- `paper-account-lifecycle`: 转账、统一银行与计息、破产冻结、主动重置及双排行榜。
- `qqbot-autonomous-trading`: bot 自有账户自主交易、用户账户隔离及沉浸式回报。

### Modified Capabilities

- `model-tool-catalog`: Paper 目录增加转账，账户配置增加显式重置，榜单支持收益率及下海次数。
- `paper-ordered-trading`: 交易执行、后台撮合及重试遵守破产状态和重置周期隔离。
- `visible-position-management`: 取消首次认领及持久唯一 owner，改为授权范围内共管、成功修改版本协调和保留证据的迁移。
- `exclusive-position-management`: 移除旧独占配置的排他行为，保留已授权维护、证据核验与并发防重复约束。
- `trading-tool-contracts`: 跨连接撤改单、平仓及保护的共享管理契约，不以其他 owner 为拒绝理由。
- `spot-inventory-reconciliation`: 不同连接共享真实基础资产和预留，共管不复制库存或以独占限制卖出。
- `decision-state-context`: 按实际权限展示可执行动作，不因历史 owner 把有权限调用者标为只读。

## Impact

- 后端：`backend/app/services/news_service.py`、`backend/utils/news_schedule.py`；`backend/mcp/paper*.py`、`canonical_tools.py` 以及 Paper 后台配置路由、维护入口和 PostgreSQL 持久化结构。
- 真实账户协调：`backend/utils/visible_management.py`、`position_management.py`、`management_lock.py`、保护计划/执行恢复、`backend/agent/unified_dispatch.py`、MCP 实盘服务与现货退出、相关配置和状态投影。主规范中的长期独占条款将由本变更的增量取代，规划期间不直接改主规范。
- 前端：`DecisionMessages.jsx`、`ToolResultContent.jsx/.css`、`ChatRunProgress.jsx`、`ChatPage.jsx`、`PaperTradingPanel.jsx`、`PaperDataPanel.jsx` 及现有新闻配置/监控组件。
- 文档与集成：`docs/qqbot/人格prompt.txt`、`docs/qqbot/qqbot-paper-trading/SKILL.md` 及其分发包；更新 README 和相关运维说明。仓库内没有外部 QQBot 定时任务的实际配置，本次提供与已有触发器衔接的指令及验收步骤，不新增第二个调度器。
- 接续已有 `simplify-memory-and-ordered-execution` 的模型简短文本回执计划；本次不另改模型返回契约或回执字节上限。主规范已存在的大小限制与当前代码差异留给所属变更处理。
- 仅生成计划，不实现代码、不改主规范、不提交、不归档、不发布、不操作生产或真实账户。保留全部无关本地修改。
