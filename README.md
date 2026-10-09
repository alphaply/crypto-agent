# Crypto Agent

Crypto Agent 是一个基于 FastAPI、React 和 LangGraph 的加密货币交易代理项目。它提供多策略 Agent 调度、行情分析、K 线可视化、交易记录、成本统计、聊天控制台和 Web 化运行配置。

项目适合用于本地研究、策略复盘和自托管部署。默认不会把交易所、模型 API Key 等敏感配置写进代码，运行后的策略、模型、交易所密钥、提示词和价格配置主要在 WebUI 中维护，并保存到 SQLite 数据库。

## 功能概览

- FastAPI 后端和 React + Vite 前端
- 多 Agent 策略配置和定时调度
- 现货 `SPOT_DCA` 单任务支持 1–10 个同计价币标的，可从交易所接口搜索选择，也可通过 API 配置；默认分析 `4h / 1d / 1w`，支持任务级覆盖（[现货组合与 API 说明](docs/TRADING_WORKFLOW.md#现货组合任务)）。
- 现货计划支持每日/每周多个时点，每个计划时槽获得一份跨标的组合额度；同一时槽的重试、审批恢复和重启不会重置额度，另受任务总预算约束；单标的初始持仓数量及成本与本任务后续成交合并统计。
- 合约开仓、加仓、按数量减仓和批量顺序操作；退出方式可选必填 TP/SL、可选 TP/SL 或独立退出单，实盘与模拟均支持（[流程说明](docs/TRADING_WORKFLOW.md)）。
- 手动合约仓可在独立退出模式下显式接管，核对账户、方向、实时数量和挂单后由模型管理平仓及止盈止损；接管本身不下单（[接管说明](docs/WORKBENCH.md#合约独立退出与平仓排查)）。
- 交易 Agent 专注决策与执行，只读长期规则；短期记忆 Agent 在每轮摘要落库后，结合本轮摘要、该任务前 4 小时摘要和当前记忆更新，仅维护短期动态 memory，暂停自动 rule 维护。后台记忆中心支持人工编辑、锁定和版本记录。
- 每轮由配置的摘要模型（如 DeepSeek）按策略 Prompt 压缩完整决策与工具执行记录，完整保存返回的策略摘要，原始分析另行保留。决策默认读取行情、账户、完整短期记忆、最近三轮策略摘要和有效规则；收益、回撤及最近 7 天平仓由记忆 Agent 整理。应用不按字符数截断输入和有效输出，模型压缩仍遵循自定义 Prompt；所选时间窗口内的成交证据完整传递（[指标口径](docs/agent-performance-context.md)）。
- 后台「Agent 运行」可查看实际模型输入、工具定义、输出、状态和 token 数；从升级后开始记录，无需依赖 LangSmith 查看拼接后的 Prompt（[设计说明](docs/PROMPT_DESIGN.md)）。
- 交易接口及改单约束随工具描述提供；每轮保留精简的 TP/SL 状态。实盘入场改单支持多空方向校验，模拟未成交单支持联合修改入场价和 TP/SL，已成交仓位单独管理保护。
- 新建合约任务默认每 60 分钟运行，1h 分析预设使用 `1h / 4h / 1d`，每周期计算 600 根历史并展示 `24 / 12 / 7` 根已收盘 K 线。既有间隔、时段和显式行情配置保留。
- K 线、均线、持仓、订单和盈亏展示
- Dashboard 展示跨任务数据、收益和共享消息；独立 Agents 页按任务查看运行状态、固定报告、持仓、短期记忆与 K 线。聊天采用“新建任务 → 聊天”，支持从交易所目录搜索、筛选及分页选择现货/合约标的，默认分析，可关联交易配置。
- 聊天每次成功压缩上下文后自动总结会话标题，支持 reasoning 模型；标题生成失败保留原名。后台可直接删除现货任务及本地关联记录，交易所持仓和挂单需自行处理（[操作说明](docs/WORKBENCH.md#聊天标题与任务删除)）。
- 模型按渠道独立计价，支持 models.dev 每 6 小时同步、人工覆盖、缓存费用和长期用量记录；未定价不显示为免费。
- `/mcp` Streamable HTTP 服务为 WorkBuddy / ChatGPT 提供共享数据、标的搜索和扁平参数交易工具，支持现货买入、归属库存卖出及交易所原生 TP/SL/OCO；独立交易配置、权限和持久幂等回执，标的可指定或明确设置为不限制（[工具与接入说明](docs/MCP.md)）。
- 全局消息支持独立抓取与摘要间隔，抓取最短 1 分钟：宏观与加密来源、Polymarket、律动官方 JSON API、币安官方公告连接。评分可关闭、使用 Jev 或普通 LLM 批量评分；普通模型生成滚动摘要，输入未变化时复用快照。消息触发可为各任务指定独立模型、来源/关键词/类别、冷却与每日次数。后台提供本地处理明细及可选 LangSmith 追踪，各 Agent、Chat、Dashboard 与 MCP 共享摘要（[消息流水线与配置](docs/NEWS_PIPELINE.md)）。
- SQLite 本地状态存储
- Docker 部署，Web 服务和调度器分容器运行

## 项目结构

```text
backend/app/              FastAPI 应用、认证、API 路由和服务层
backend/app/core/         调度器、运行时和安全相关逻辑
backend/agent/            Agent 图、工具和提示词模板
backend/utils/            行情、指标、日志和 LLM 工具
backend/config.py         运行配置入口
backend/database.py       SQLite 数据访问层
frontend/                 React + Vite 前端
docs/                     部署和产品文档
```

运行状态文件主要包括 `.env`、`trading_data.db`、日志和本地价格配置。不要提交真实密钥和运行数据库。

## 普通安装

### 环境要求

- Python 3.10+
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

`ADMIN_PASSWORD` 用于登录控制台，`JWT_SECRET` 用于会话签名，`CONFIG_MASTER_KEY` 用于加密 SQLite 中保存的密钥。已有数据库继续使用时，不要更换 `CONFIG_MASTER_KEY`。
每日记忆和旧固定 4 小时批次已停用，旧数据保留归档。每轮摘要与记忆待处理记录原子落库，短期记忆按任务串行更新，失败保留旧版本并只重试记忆步骤。实盘保护计划继续由原有独立维护循环核对，不等待下一轮 LLM 分析。操作说明见 [工作台配置](docs/WORKBENCH.md)、[MCP 接入](docs/MCP.md) 和 [交易流程](docs/TRADING_WORKFLOW.md)。

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

## Docker 安装

### 准备配置

```bash
mkdir crypto-agent
cd crypto-agent
curl -O https://raw.githubusercontent.com/alphaply/crypto-agent/beta/.env.template
cp .env.template .env
```

编辑 `.env`：

```env
ADMIN_PASSWORD=your-strong-password
JWT_SECRET=your-long-random-jwt-secret
CONFIG_MASTER_KEY=your-long-stable-config-master-key
PORT=7860
SCHEDULER_MAX_WORKERS=2
TIMEZONE=Asia/Shanghai
```

### 启动服务

```bash
docker pull alphaply712/crypto-agent:latest
docker run -d --name crypto-agent --restart unless-stopped \
  --env-file .env -e RUN_SCHEDULER_IN_WEB=false \
  -p 31421:7860 -v crypto_agent_data:/app/data \
  alphaply712/crypto-agent:latest
docker run -d --name crypto-agent-scheduler --restart unless-stopped --no-healthcheck \
  --env-file .env -e RUN_SCHEDULER_IN_WEB=false \
  -v crypto_agent_data:/app/data \
  alphaply712/crypto-agent:latest uv run --no-sync python -m backend.app.core.scheduler
```

默认访问地址：

```text
http://localhost:31421/
```

健康检查：

```bash
curl http://localhost:31421/health
```

以上命令启动两个容器，共用数据卷和密钥，且只有一个调度器：

- `crypto-agent`：Web API 和前端静态资源
- `crypto-agent-scheduler`：后台调度器

运行数据保存在 `crypto_agent_data` volume 的 `/app/data` 中。升级或重启时保留该 volume，并保持 `CONFIG_MASTER_KEY` 不变，否则已保存的加密密钥无法解密。

### 常用 Docker 命令

```bash
docker logs -f crypto-agent
docker logs -f crypto-agent-scheduler
docker restart crypto-agent crypto-agent-scheduler
```

升级需拉取镜像、备份数据并重新创建容器，详见 [部署指南](docs/DEPLOYMENT_GUIDE.md)。不要删除 `crypto_agent_data` 数据卷。仓库不提供 Compose 文件；已有自维护 Compose 可继续使用相同镜像、数据卷和环境变量。

### 从源码构建镜像

当前发布镜像为 `alphaply712/crypto-agent:latest`。构建并验证后推送同一标签：

- **极速构建（推荐，本地已有前端编译产物）**：
  ```bash
  npm run build --prefix frontend
  docker build -f Dockerfile.prebuilt -t alphaply712/crypto-agent:latest .
  ```
- **全量多阶段构建（自动在容器内编译前端）**：
  ```bash
  docker build -t alphaply712/crypto-agent:latest .
  # 若网络受限拉取 Docker Hub 慢或 EOF，可传入镜像源加速：
  # docker build --build-arg NODE_IMAGE=dockerpull.cn/library/node:22-bookworm-slim -t alphaply712/crypto-agent:latest .
  ```

```bash
docker push alphaply712/crypto-agent:latest
```

宝塔容器或已有 Compose 服务须使用完整镜像名 `alphaply712/crypto-agent:latest`，拉取新镜像后重新创建容器才会生效。更新时保留原有端口、环境变量、数据卷和 `CONFIG_MASTER_KEY`；只执行 `docker push` 不会更新运行中的容器。

```bash
docker run --rm -p 31421:7860 \
  -e ADMIN_PASSWORD=local-password \
  -e JWT_SECRET=local-jwt-secret \
  -e CONFIG_MASTER_KEY=local-config-master-key \
  -e RUN_SCHEDULER_IN_WEB=false \
  alphaply712/crypto-agent:latest
```

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

密钥会通过 `CONFIG_MASTER_KEY` 加密后保存在 SQLite 中。

新建任务默认要求止盈和止损；旧 REAL 任务未配置 `exit_mode` 时保持可选，旧 STRATEGY 保持必填。选择「独立退出单」后，开仓不附带整仓 TP/SL，模型通过带数量的市价退出、限价止盈或触发市价止损分批管理仓位。看板展示退出单及未被有效止损覆盖的数量。有持仓、挂单或待核验操作时不能切换退出方式。

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

可选附带保护模式下，同方向加仓未填写 TP/SL 时继承现有保护；填写时先更新整仓保护再加仓。独立退出模式下，加仓不会扩大已有退出单数量。Agents 页支持查看退出单和覆盖数量、调整附带保护，以及查看最近 7 天持仓周期。后台「数据库管理」支持只读副本分析、历史清理预览、自动备份、周期重建与空间回收。

数据库默认使用 `data/trading_data.db`；根目录同名文件仅在目标不存在时迁移。详见 [同步库核查与维护说明](docs/DB_AUDIT_2026-09-08.md)。

持仓周期表分别展示手续费前盈亏、开平仓手续费和净盈亏（不含资金费）；费用缺失或跨币种时净盈亏保持未知。成交活动按成交时间单独统计，同步覆盖与未闭合周期可展开查看。模型空响应、断连或无法恢复的输出截断会保存失败记录，调度任务标记失败。正文截断但全部工具参数 JSON 完整时，保留截断提示并继续工具校验和执行。


### Agent 运行记录

登录后在 `/console/config` 的「Agent 运行」查看已记录的模型请求。记录包含实际消息与角色、工具 schema、模型输出、调用状态和已返回的 token 用量；可以按交易、记忆、聊天、消息评分和摘要等用途筛选。普通任务保留最近 100 次调用、最长 30 天；全局消息调用单独保留最多 12,100 条、最长 30 天，避免一轮批量评分挤掉前面的失败记录。消息处理历史另保留最近 20 轮。不重建历史 Prompt。调用开始时保存当时已配置的模型费率，结束时按返回的 token 数估算费用；若有服务商返回费用则优先采用，页面标明来源。缺失用量或价格时估算费用未知；估算不含缓存优惠等差异，不等于服务商账单，之后改价不重算已记录调用。

交易 Agent、绑定任务聊天和短期记忆模型均不提供 `manage_trading_rules` 工具。记忆整理正常仅调用一次模型，输入旧 memory、本轮及前 4h 摘要和实际执行证据，输出新 memory，不再查询/修改规则或追加规则复盘段落。已有规则记录、人工锁和版本保留，交易端只读；硬性风控不变。失败保留旧记忆，持久任务最多尝试 3 次（间隔退避），之后停止自动重试，不重放交易；单次网络重试还受模型重试配置及最多 3 次请求限制。旧 `partial` 规则回执只用于兼容历史，不重新执行。记忆调用也遵守辅助调用远程追踪开关，本地审计与用量仍保留。

### LangSmith 追踪

后台需要同时启用 `langchain_tracing`、设置项目名称并保存 LangSmith API Key；只填写 key 不会自动开启追踪。保存后刷新 SDK 配置，后续运行使用最新配置。Agent 决策和聊天保留完整追踪；辅助摘要/记忆调用默认不上传，可通过 `langchain_background_tracing` 打开。共享消息另设 `news.trace_mode`：默认 `off`，`summary` 每轮仅一条概要，`full` 保留采集、评分和摘要子运行用于排障。远程追踪关闭不影响本地处理、调用和费用记录；追踪失败不影响业务处理。这里没有对 Agent 执行做随机采样。

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
- 数据库导出使用 SQLite 一致性快照和浏览器直接下载，不再在服务端及浏览器各复制完整文件到内存。下载凭据仅在 120 秒内对下载接口有效；快照准备时间仍取决于数据库大小、磁盘和写入负载。
- 数据库管理支持保留最近 7/30/90 天的快捷选择，可选择已移除任务的遗留 ID；清理先预览、再备份，保留成交及保护证据。
- 指标采用 SMA 初始化的 Wilder 平滑；剔除异常 OHLCV、提示时间缺口及陈旧数据，成交量倍数使用之前 20 根已收盘 K 线作基准。修正不等同于胜率保证，需要独立样本和后续实盘评估。

详见 [工作台改造与验证记录](docs/WORKBENCH_UPGRADE.md)。

Dashboard 专注数据总览；独立 Agents 页承载任务运行计划与实时工作区。下一次运行遵循实际分时段规则，显示日期、时区、当前频率与暂停状态；状态自动更新，行情失败保留旧数据并明确提示。K 线在当前浏览器记住上次选择的周期，刷新或切换任务时继续使用；周期切换中或加载失败时明确标注仍显示的数据周期，桌面和手机使用同一组周期按钮。详见 [Dashboard 修复与验证](docs/DASHBOARD_REFRESH.md)。
