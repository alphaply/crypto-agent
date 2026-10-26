# Crypto Agent

Crypto Agent 是一个基于 FastAPI、React 和 LangGraph 的加密货币交易代理项目。它提供多策略 Agent 调度、行情分析、K 线可视化、交易记录、成本统计、聊天控制台和 Web 化运行配置。

项目适合用于本地研究、策略复盘和自托管部署。默认不会把交易所、模型 API Key 等敏感配置写进代码，运行后的策略、模型、交易所密钥、提示词和价格配置主要在 WebUI 中维护，并保存到 PostgreSQL 数据库。

## 功能概览

- FastAPI 后端和 React + Vite 前端
- 多 Agent 策略配置和定时调度
- 策略、新闻触发和定投持久排队；运行名额不足时保留每轮触发，同策略串行、不同策略使用空闲名额。页面分别显示当前运行与排队，运行时读取最新配置；暂停、删除或交易范围变化取消旧等待项（[重构说明](docs/REFACTOR_RUNTIME_20261018.md)）。
- 现货 `SPOT_DCA` 单任务支持 1–10 个同计价币标的，可从交易所接口搜索选择，也可通过 API 配置；看板可按币种修改当前持仓数量与平均成本价，保存后只累加新增买入；默认分析 `4h / 1d / 1w`，支持任务级覆盖（[现货组合与 API 说明](docs/TRADING_WORKFLOW.md#现货组合任务)）。
- 现货计划支持每日/每周多个时点，每个计划时槽获得一份跨标的组合额度；同一时槽的重试、审批恢复和重启不会重置额度，另受任务总预算约束；单标的初始持仓数量及成本与本任务后续成交合并统计。
- 合约开仓、加仓、按数量减仓和批量顺序操作；退出方式可选必填 TP/SL、可选 TP/SL 或独立退出单，实盘与模拟均支持（[流程说明](docs/TRADING_WORKFLOW.md)）。
- 当前账户授权可见的仓位与挂单自动核验，多连接和 Agent 可共享管理；人工操作持续对账，原提交者保留审计。模型无需先 adopt 或释放其他管理者（[管理说明](docs/exclusive-position-management.md)）。
- 历史管理配置统一为共管。独立退出发现人工平仓时只更新现状与历史，不自动撤挂单或补仓；强制止盈止损继续维护系统最新保存目标，Agent 和已登录前端均可调整。
- 独立交易按每笔动作实际依赖准入，工具内自动取证，无需先查询解锁；历史明确拒绝不阻止有效新单。后台最新状态与原始受理结果分开保存。v2 批次可选 `execution_policy=independent`，用 `depends_on` 表达显式依赖；省略策略保留失败停止，未执行项不自动补发（[回执与批次示例](docs/TRADE_RECEIPTS_AND_RECOVERY.md)）。
- 交易 Agent 专注决策与执行，不再注入长期规则；短期记忆 Agent 在每轮摘要落库后，结合本轮摘要、该任务前 4 小时摘要和当前记忆更新，仅维护短期动态 memory，暂停自动 rule 维护。后台记忆中心支持人工编辑、锁定和版本记录。决策上下文与记忆写入已拆分，旧 Daily Memory 空区块和旧日报/四小时补跑路径已清理（[代码职责](docs/PROMPT_DESIGN.md#记忆代码职责)）。
- 每轮由配置的摘要模型（如 DeepSeek）按策略 Prompt 压缩完整决策与工具执行记录，完整保存返回的策略摘要，原始分析另行保留。独立退出模式的决策默认读取行情、消息、精简后的当前持仓/挂单、相关执行异常，以及已发布短期记忆和最近三条已发布策略摘要；整理、失败和重试期间继续使用有效内容及真实截至时间。历史回执按需查询，旧执行限制随同身份最新证据退出，长期规则不再自动注入。记忆以纯文本整理近期操作、策略判断、关键价位与有效教训；收益、盈亏与回撤由程序确定性计算，详细账本按需查看。原文完整保存，不按字符数截断（[提示词边界](docs/PROMPT_DESIGN.md#独立退出模式的当前状态与策略投影)、[三单精简示例](docs/DECISION_CONTEXT_EXAMPLE.md)、[指标口径](docs/agent-performance-context.md)）。
- 后台「Agent 运行」可查看实际模型输入、工具定义、输出、状态和 token 数；从升级后开始记录，无需依赖 LangSmith 查看拼接后的 Prompt（[设计说明](docs/PROMPT_DESIGN.md)）。
- 交易接口及改单约束随工具描述提供；每轮保留精简的 TP/SL 状态。实盘入场改单支持多空方向校验，模拟未成交单支持联合修改入场价和 TP/SL，已成交仓位单独管理保护。
- 新建合约任务默认每 60 分钟运行，1h 分析预设使用 `1h / 4h / 1d`，每周期计算 600 根历史并展示 `24 / 12 / 7` 根已收盘 K 线。最新价格独立取自同一交易所、同一标的的最新成交价并注明取样时间；获取失败显示 N/A，不以 K 线收盘价替代。既有间隔、时段和显式行情配置保留。
- K 线、均线、持仓、订单和盈亏展示
- Dashboard 展示跨任务数据、收益和共享消息；独立 Agents 页默认按顺序展示决策模型原文与工具调用、策略摘要、短期记忆更新；行情账户与交易记录分栏查看。收尾只生成纯文本策略摘要，不再生成固定报告或发起报告格式修复。单任务隐藏任务列表，多任务支持搜索切换；运行状态和调度时间集中展示。聊天采用“新建任务 → 聊天”，支持从交易所目录搜索、筛选及分页选择现货/合约标的，默认分析，可关联交易配置。
- 决策与聊天的交易回执直接展示逐项结果卡片，区分做多／做空与平多／平空，止损改单显示“修改止损”；一般查询结果保留按需展开。批量订单类型兼容大小写，平仓方向必须明确填写。后台批次汇总复用子操作回执，部分核验失败按原身份退避重试；归属索引不再因计划中的纯观察信息更新而反复重扫（[回执说明](docs/TRADE_RECEIPTS_AND_RECOVERY.md)）。
- 聊天每次成功压缩上下文后自动总结会话标题，支持 reasoning 模型；标题生成失败保留原名。后台可直接删除现货任务及本地关联记录，交易所持仓和挂单需自行处理（[操作说明](docs/WORKBENCH.md#聊天标题与任务删除)）。
- 模型按渠道独立计价，支持 models.dev 每 6 小时同步、人工覆盖、缓存费用和长期用量记录；未定价不显示为免费。
- 添加或编辑模型时，填写供应商地址、协议和 Key 后自动读取可用模型，支持按 ID / 名称搜索和手动别名；无需先保存，兼容 OpenAI、DeepSeek、Anthropic 与 Jev，列表读取不发起推理或修改价格（[操作与接口说明](docs/WORKBENCH.md#添加模型与供应商目录)）。
- 交易 Agent 只绑定 `execute_trade_actions`，一次调用顺序执行 1–20 项动作；主 Agent、小模型和绑定任务聊天一致，只读聊天与记忆/摘要零工具。操作查询工具已移除，权威受理结果当次返回，后续状态由后台自动更新。兼容金融工具不进入默认目录，历史详情通过授权 HTTP/UI 查阅。
- `/mcp` Streamable HTTP 服务为 WorkBuddy / ChatGPT 提供聚合查询及统一顺序交易：live 默认 7 个工具（只读 6），paper 默认 11 个（只读 7），保留现货与原生 TP/SL/OCO、权限和持久回执；标的可指定或明确设置为不限制（[工具与接入说明](docs/MCP.md)）。
- 全局消息支持独立抓取与摘要间隔，抓取最短 1 分钟：宏观与加密来源、Polymarket、律动官方 JSON API、币安官方公告连接。评分可关闭、使用 Jev 或普通 LLM 批量评分；普通模型生成滚动摘要，输入未变化时复用快照。消息触发可为各任务指定独立模型、来源/关键词/类别、冷却与每日次数。后台提供本地处理明细及可选 LangSmith 追踪，各 Agent、Chat、Dashboard 与 MCP 共享摘要（[消息流水线与配置](docs/NEWS_PIPELINE.md)）。
- PostgreSQL 17 持久存储与版本化迁移
- Docker 部署，Web 服务和调度器分容器运行

## 项目结构

```text
backend/app/              FastAPI 应用、认证、API 路由和服务层
backend/app/core/         调度器、运行时和安全相关逻辑
backend/agent/            Agent 图、工具和提示词模板
backend/utils/            行情、指标、日志和 LLM 工具
backend/config.py         运行配置入口
backend/database/         按业务划分的数据库访问代码，保留 backend.database 公共入口
backend/storage/          PostgreSQL 连接与版本化迁移
frontend/                 React + Vite 前端
docs/                     部署和产品文档
```

数据库运行在 PostgreSQL；`.env`、共享 `DATA_DIR` 中的自定义 Prompt 和日志单独持久保存。不要提交真实密钥和运行数据。

## 普通安装

### 环境要求

- Python 3.11+（Docker 使用 3.11）
- Node.js 20+
- uv
- npm

### 安装依赖

```bash
uv sync
npm install --prefix frontend
```

### 初始化配置

```bash
cp .env.template .env
```

Windows PowerShell:

```powershell
Copy-Item .env.template .env
```

至少修改 `.env` 中的这些值：

```env
ADMIN_PASSWORD=your-strong-password
JWT_SECRET=your-long-random-jwt-secret
CONFIG_MASTER_KEY=your-long-stable-config-master-key
PORT=7860
RUN_SCHEDULER_IN_WEB=true
SCHEDULER_MAX_WORKERS=2
TIMEZONE=Asia/Shanghai
```

`ADMIN_PASSWORD` 用于登录控制台，`JWT_SECRET` 用于会话签名，`CONFIG_MASTER_KEY` 用于加密 PostgreSQL 中保存的密钥。已有数据库继续使用时，不要更换 `CONFIG_MASTER_KEY`。
每日记忆和旧固定 4 小时批次已停用，旧数据保留归档。每轮摘要与记忆待处理记录原子落库，短期记忆按任务串行更新，失败保留旧版本并只重试记忆步骤。实盘保护计划继续由原有独立维护循环核对，不等待下一轮 LLM 分析。操作说明见 [工作台配置](docs/WORKBENCH.md)、[MCP 接入](docs/MCP.md) 和 [交易流程](docs/TRADING_WORKFLOW.md)。

### 准备本地数据库

启动本地 PostgreSQL 17（可使用 Docker），在 `.env` 设置指向该数据库的 `DATABASE_URL`。本地 Python 使用 `127.0.0.1`，Compose 内使用服务名 `db`。不要沿用旧 `TRADING_DB_PATH` / `CHAT_CHECKPOINT_DB`。

```bash
uv run python -m backend.storage.migrate
```

测试数据库启动、隔离与清理见 [PostgreSQL 开发说明](docs/POSTGRES_DEVELOPMENT.md)。只运行 Web 时设 `RUN_SCHEDULER_IN_WEB=false`；首次导入设 `DEPLOYMENT_MAINTENANCE=true`，完成核对后再关闭。

### 启动开发环境

启动后端和调度器：

```bash
uv run python -m backend.app
```

启动前端开发服务器：

```bash
npm run dev --prefix frontend
```

常用地址：

- `http://localhost:7860/`：后端服务和生产静态页面入口
- `http://localhost:7860/health`：健康检查
- `http://localhost:7860/console/chat`：聊天控制台
- `http://localhost:7860/console/config`：运行配置
- `http://localhost:5173/`：Vite 开发服务器

### 构建前端

```bash
npm run build --prefix frontend
```

构建完成后，FastAPI 会用于生产静态文件服务。

## Docker 安装与更新

使用仓库的 `compose.yaml`：`db` 保存 PostgreSQL 数据，`web` 提供页面，`scheduler` 执行后台任务。数据库不暴露宿主机端口，两个应用容器使用同一镜像和共享应用卷。

首次从 SQLite 切换请按 [部署指南](docs/DEPLOYMENT_GUIDE.md) 操作：导出配置包、停旧服务、建立空 PostgreSQL、暂停执行时导入配置，再重启核对并启用调度。旧交易、聊天和记忆不搬迁；旧 SQLite 原样保留。

已经完成首次切换后，开发机 `docker push` 只是上传镜像。服务器进入保存 `compose.yaml`、`.env`、`scripts/` 的目录执行：

```bash
bash scripts/update-postgresql.sh
```

脚本依次拉镜像、停应用、备份、迁移、检查 Web、启动 scheduler；失败停在当前阶段，不会删除卷或自动恢复旧库。备份包含密钥，需私密保存。详细手动步骤及回退边界见 [部署指南](docs/DEPLOYMENT_GUIDE.md)。

从源码构建：

```bash
npm run build --prefix frontend
docker build -f Dockerfile.prebuilt -t alphaply712/crypto-agent:latest .
# 或完整构建前端与后端：
docker build -t alphaply712/crypto-agent:latest .
```

发布需另行执行仓库规定的归档/发布流程；构建成功不代表生产已经更新。

## WebUI 配置

编辑模型时，推理力度下拉框可选择具体 effort 或「跟随模型默认」；后者不同于显式 `none`。右侧箭头用于展开菜单，不再被悬停清空按钮覆盖。

页面加载时读取系统的「减少动画」偏好，通过 Ant Design 的 `motion` 配置关闭组件动画；更改系统偏好后刷新生效，避免动态切换导致表单重建、丢失未保存编辑。不要用全局极短动画时长覆盖组件样式，否则可能导致下拉菜单定位异常。浏览器回归覆盖普通/减少动画、页面/抽屉下拉框以及桌面和移动端触摸模式。

首次启动后进入 `/console/config` 维护运行配置，包括：

- Agent、交易对、模式、调度周期和提示词
- LLM 模型、API Base、API Key、temperature 和扩展参数
- 交易所 API Key、Secret 和 Passphrase
- 汇总提示词、短期记忆、模型价格和统计配置
- 任务退出管理方式；记忆中心的长期交易规则、人工锁定和修改记录
- Agent 运行记录：实际请求消息、工具定义、输出、执行状态和 token 用量

密钥会通过 `CONFIG_MASTER_KEY` 加密后保存在 PostgreSQL 中。

新建任务默认要求止盈和止损；旧 REAL 任务未配置 `exit_mode` 时保持可选，旧 STRATEGY 保持必填。选择「独立退出单」后，模型可多次 open 分批建仓、成交后在本轮或后续轮次 close 设置退出，Hedge 账户支持多空分别管理。close 支持市价、限价及条件市价/限价 TP/SL，可指定固定数量档和最后全部退出；edit 统一修改未成交量，cancel 撤销挂单。固定档不自动缩量或补量；条件市价 all 在触发时全平交易所账户该品种该方向（包括之后同向新增仓位），市价 all 只使用提交前核验快照。看板区分触发价、委托价、全部、实际成交和覆盖估计。有持仓、挂单或待核验操作时不能切换退出方式。调用示例及限制见 [MCP v2](docs/MCP.md#独立退出规范工具-v2)。

REAL / STRATEGY 任务的「调度设置」支持默认间隔加自定义时段：选择星期、时区、开始/结束时间和运行间隔（15–1440 分钟），按列表从上到下匹配第一条，其余时间沿用默认间隔。可一键填入「亚盘 30 / 美盘 20 / 周末 30 分钟」预设，再修改、保存任务，最后点击页面上的「保存配置」生效。规则从时段开始时间对齐，例如 09:30 起每 20 分钟在 09:30、09:50、10:10 运行；跨午夜的星期指开始那一天，全天使用 00:00–24:00。纽约时区自动适配夏令时。详见 [交易执行与证据说明](docs/TRADING_WORKFLOW.md)。

## 验证

```bash
uv run pytest -q
npm run build --prefix frontend
npm run lint --prefix frontend
npm run test:chat --prefix frontend
npm run test:dashboard --prefix frontend
uv run backend/utils/test_agent_connection.py
```

其中模型连通性测试需要先配置可用的模型 API Key。

## 安全提示

- 不要提交真实 API Key、`.env`、`pricing.json`、`trading_data.db` 或日志文件
- 生产环境务必设置强 `ADMIN_PASSWORD`
- 泄露后应立即轮换 `JWT_SECRET` 和相关 API Key
- 同一份数据库必须长期使用同一个 `CONFIG_MASTER_KEY`

## Polymarket 消息面

支持在后台「配置 → 消息聚合」添加 Polymarket 事件监控，无需 API key。公开看板展示共享消息，预测概率也会进入 Agent 的消息上下文。配置及采集机制见 [Polymarket 监控说明](docs/POLYMARKET.md)。
## TP/SL、持仓周期与数据库维护

可选附带保护模式下，同方向加仓未填写 TP/SL 时继承现有保护；填写时先更新整仓保护再加仓。独立退出模式下，加仓不会扩大固定退出档，动态 all 在触发时按剩余方向仓位执行；覆盖和 quantity_conflict 单独显示，覆盖不保证成交。Agents 页支持查看退出单和覆盖数量、调整附带保护，以及查看最近 7 天持仓周期。后台「数据库管理」支持只读副本分析、历史清理预览、自动备份、周期重建与空间回收。

数据库必须显式配置 `DATABASE_URL`。Docker 镜像启动 Web/scheduler 前自动执行待应用迁移，失败则停止启动；本地运行前执行 `uv run python -m backend.storage.migrate`；不再自动打开或搬迁 SQLite 文件。旧库核查见历史文档 [同步库核查与维护说明](docs/DB_AUDIT_2026-09-08.md)。

持仓周期表分别展示手续费前盈亏、开平仓手续费和净盈亏（不含资金费）；费用缺失或跨币种时净盈亏保持未知。成交活动按成交时间单独统计，同步覆盖与未闭合周期可展开查看。模型空响应、断连或无法恢复的输出截断会保存失败记录，调度任务标记失败。正文截断但全部工具参数 JSON 完整时，保留截断提示并继续工具校验和执行。


### Agent 运行记录

登录后在 `/console/config` 的「Agent 运行」查看已记录的模型请求。记录包含实际消息与角色、工具 schema、模型输出、调用状态和已返回的 token 用量；可以按交易、记忆、聊天、消息评分和摘要等用途筛选。普通任务保留最近 100 次调用、最长 30 天；全局消息调用单独保留最多 12,100 条、最长 30 天，避免一轮批量评分挤掉前面的失败记录。消息处理历史另保留最近 20 轮。不重建历史 Prompt。调用开始时保存当时已配置的模型费率，结束时按返回的 token 数估算费用；若有服务商返回费用则优先采用，页面标明来源。缺失用量或价格时估算费用未知；估算不含缓存优惠等差异，不等于服务商账单，之后改价不重算已记录调用。

交易 Agent、绑定任务聊天和短期记忆模型均不提供 `manage_trading_rules` 工具。记忆整理正常仅调用一次模型，输入上一轮有效记忆、最近 4h 含本轮的压缩摘要及旧版 7 天成交/平仓记录，输出单份纯文本 memory，不单独获取新闻、不要求 JSON 或多份投影；执行关联由后台保存，不再查询/修改规则或追加规则复盘段落。已有规则记录、人工锁和版本保留，不再注入交易决策上下文；硬性风控不变。失败保留旧记忆，持久任务最多尝试 3 次（间隔退避），之后停止自动重试，不重放交易；单次网络重试还受模型重试配置及最多 3 次请求限制。旧 `partial` 规则回执只用于兼容历史，不重新执行。记忆与策略摘要跟随 LangSmith 主开关，本地审计与用量始终保留。

### LangSmith 追踪

后台需要同时启用 `langchain_tracing`、设置项目名称并保存 LangSmith API Key；只填写 key 不会自动开启追踪。保存后刷新 SDK 配置，后续运行使用最新配置。Agent 决策、聊天、策略摘要和短期记忆跟随主开关追踪；`langchain_background_tracing` 仅控制其他辅助调用。摘要与记忆使用同名父运行覆盖输出校验，子运行记录模型调用；拒绝不完整响应时父运行标记失败，保留完整返回文本、停止原因、用量、推理和显式输出/推理参数。共享消息另设 `news.trace_mode`：默认 `off`，`summary` 每轮仅一条概要，`full` 保留采集、评分和摘要子运行用于排障。远程追踪关闭不影响本地处理、调用和费用记录；追踪失败不影响业务处理。这里没有对 Agent 执行做随机采样。

环境变量配置支持 `LANGSMITH_TRACING=true`、`LANGSMITH_PROJECT=crypto-agent`、`LANGSMITH_API_KEY`，也兼容旧版 `LANGCHAIN_*` 名称。美国区使用默认地址；欧洲区需在部署环境设置 `LANGSMITH_ENDPOINT=https://eu.api.smith.langchain.com`。不要将 key 提交到仓库。

排查时查看日志中的 `LangSmith tracing=true ... api_key_set=True`，并确认 LangSmith 所选项目和区域。SDK 缓存问题参见 [官方排查文档](https://docs.langchain.com/langsmith/troubleshooting-variable-caching)。

### LLM 断线重试与数据刷新

- 后台 `llm_timeout_seconds` 是每次请求的客户端超时（秒），首次请求和重试使用同一设置；`llm_max_retries` 是每个模型的额外重试次数。SDK 内部重试关闭，由应用统一重试。
- `Server disconnected without sending a response` 表示上游服务或中间网关已经断开连接。即使客户端设置 1200 秒，上游也可能在约 340 秒提前断开；增加客户端超时不能覆盖上游连接限制。连接失败后仍采用短暂退避（1、2、4 秒），超时值不代表重试间隔。
- 主决策模型、小模型每次重试，以及切换兜底模型前，重新获取配置周期的 K 线并计算指标，刷新余额、持仓、挂单和提示词时间。新闻沿用新闻模块自身缓存策略。
- 新快照替换旧提示词，保留已完成的工具调用与返回记录。失败流的部分回答不进入下一次请求，也不会由重试逻辑重新执行交易工具。
- 刷新缺少周期、K 线被标记过期、账户查询失败或提示词刷新异常时，中止本轮决策；不使用旧快照继续请求兜底模型。
- 日志记录每次请求的配置超时、实际尝试耗时和底层错误类型。部署后查看 `configured_timeout=1200s`，可确认运行进程加载的配置。


## 工作台体验与数据维护（2026-09）

- 聊天支持会话搜索、问题建议、只读临时聊天、移动端回车换行、请求阶段/耗时/输出字符时间线；推理区仅展示上游实际返回的内容。发送期间阻止切换会话，避免响应写入错误会话。
- Prompt 支持文件搜索、专注编辑、顶部保存、Ctrl/⌘+S、未保存提示和当前浏览器标签页的草稿恢复；切换文件前确认丢弃修改。
- 数据库导出使用 PostgreSQL `pg_dump` 自定义格式（`.dump`）和浏览器直接下载，不再在服务端及浏览器各复制完整文件到内存。下载凭据仅在 120 秒内对下载接口有效；快照准备时间仍取决于数据库大小、磁盘和写入负载。
- 数据库管理支持保留最近 7/30/90 天的快捷选择，可选择已移除任务的遗留 ID；清理先预览、再备份，保留成交及保护证据。
- 指标采用 SMA 初始化的 Wilder 平滑；剔除异常 OHLCV、提示时间缺口及陈旧数据，成交量倍数使用之前 20 根已收盘 K 线作基准。修正不等同于胜率保证，需要独立样本和后续实盘评估。

详见 [工作台改造与验证记录](docs/WORKBENCH_UPGRADE.md)。

Dashboard 专注数据总览；独立 Agents 页承载任务运行计划与实时工作区。下一次运行遵循实际分时段规则，显示日期、时区、当前频率与暂停状态；状态自动更新，行情失败保留旧数据并明确提示。K 线在当前浏览器记住上次选择的周期，刷新或切换任务时继续使用；周期切换中或加载失败时明确标注仍显示的数据周期，桌面和手机使用同一组周期按钮。详见 [Dashboard 修复与验证](docs/DASHBOARD_REFRESH.md)。

决策页面按轮次展示模型正文、服务商返回的推理文本/摘要、工具参数、执行状态与回执。运行过程保存在调度记录中，刷新页面仍可查看；仅返回推理 token 用量时显示用量和文本不可用说明。默认提示词按退出模式提供规则；自定义提示词文件保留，运行时追加独立工具事实，不强制独立模式当轮设置 TP/SL。更新后需重启后端和调度器并重新构建前端；活跃 v2 订单不可交给旧版本管理，升级与回滚见 [运行文档](docs/WORKBENCH.md#独立退出-v2-升级与恢复)。

策略记忆用纯文本整理近期操作、判断、关键价位和有效教训；新内容直接发布，旧投影转换与格式纠正已退役，原文与回执仍可在历史详情审计。收益默认显示权益变化、盈亏和快照回撤；REAL 按当前账户与标的统计全部已记录成交，STRATEGY 按任务 ID 统计。REAL 归属整理不阻塞已有成交与七天平仓统计，同步覆盖不足明确提示，缺失金额或费用保持未知；完整账本按需展开。详见 [工作台记忆与收益](docs/WORKBENCH.md#策略记忆与收益详情)。

共享仓位管理可在「管理页 → 任务与调度 → 编辑任务 → 基础设置」或「管理页 → MCP → 交易连接配置 → 编辑」开启。按账户、市场与标的授权，后台核验并持续核验范围内已有及人工变动的仓位/挂单；暂停保留原未决操作核验责任。当前状态展示来源、支持动作和核验时间；人工全平、重开及清理晚到成交保留真实边界和残仓。配置范围、界面示例和已验证的中英文手机/桌面入口见 [共享仓位管理](docs/exclusive-position-management.md)。

### QQBot Paper MCP

后台独立的 **QQBot · Paper** tab 可创建 Paper Key，为 QQBot 按用户 ID 管理模拟账户，仅交易 Binance 上现有引擎支持的 USDT 现货与线性永续、设置杠杆和借还款。历史非 BN 或来源不能证明的资源从默认查询和维护过滤，原账本、全部预留/保证金及授权审计保留；混合估值不完整时不按假零值排名。后台以资金概览、盈亏与费用、持仓/委托/事件表格展示 Paper 数据，标明当前筛选、本页统计范围和行情更新时间；缺失估值显示为未知，不补零。可调整现金与负债，统一配置银行借款开关、额度与日利率。详见 [接入文档](docs/QQBOT_PAPER_MCP.md)。

账户估值提示可查看具体缺项原因。合法下海重置后的收益核验按新周期进行，旧周期审计继续保留。页面按需加载、看板批量查询与 Paper 分页优化的检查结果及后续架构建议见 [加载性能与估值检查](docs/PERFORMANCE_REVIEW_20261025.md)。

Paper MCP 现已支持真实公开行情标注与标的搜索、bot 自有账户、用户 PK 排行、限价委托、OCO 止盈止损及分页成交/爆仓事件扫描；后台可查看持仓、委托和事件，并调整已有合约仓位的杠杆、追加或减少逐仓保证金。调杠杆保持数量与开仓价，基础保证金多退少补，手动追加部分保留；减少保证金需满足基础与强平检查。bot 可通过 `adjust_paper_position` 使用查询返回的 `resource_id` 执行相同操作。完整人格与 skill 见 [QQBot 接入文档](docs/QQBOT_PAPER_MCP.md)。

资源准入、限额与隔离验收见 [运行容量](docs/RUNTIME_CAPACITY.md)、[容量验证与回退](docs/CAPACITY_VERIFICATION.md) 和 [资源诊断](docs/RESOURCE_DIAGNOSTICS.md)。本地报告不代表生产已部署。

QQBot 模拟交易配置在管理页独立的 **QQBot · Paper** tab；原设置、账户和 Paper Key 创建功能保持可用。金融工具按请求顺序提交，依据交易所接口响应简洁返回，不在工具内补查或轮询；后台核验及必要前置检查保留。参见 [回执说明](docs/TRADE_RECEIPTS_AND_RECOVERY.md)。

金融工具不因历史归属整理积压而等待全量重扫。历史索引在后台按时间片保存进度、增量处理；当前动作权限、数量、并发、单次超时、重试和资金预算保留。交易所接受后直接返回提交成功，正常成交/持仓/盈亏更新继续在后台进行。前端交易结果直接显示操作、标的、方向、委托数量、价格及成功/失败，不再展开 JSON 或核验树。见 [耗时修复与验收](docs/TRADE_EXECUTION_LATENCY.md)、[运行说明](docs/RUNTIME_CAPACITY.md) 与 [部署指南](docs/DEPLOYMENT_GUIDE.md)。

- Paper 支持用户与 bot 双向转账、后台统一每日单利、破产冻结、用户主动下海重置及收益率/下海双榜；真实账户已授权连接可共管，保留权限、共享预留及并发保护。详见 [账户生命周期与共管说明](docs/PAPER_ACCOUNT_LIFECYCLE.md)。
