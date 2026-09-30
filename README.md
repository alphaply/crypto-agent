# Crypto Agent

Crypto Agent 是一个基于 FastAPI、React 和 LangGraph 的加密货币交易代理项目。它提供多策略 Agent 调度、行情分析、K 线可视化、交易记录、成本统计、聊天控制台和 Web 化运行配置。

项目适合用于本地研究、策略复盘和自托管部署。默认不会把交易所、模型 API Key 等敏感配置写进代码，运行后的策略、模型、交易所密钥、提示词和价格配置主要在 WebUI 中维护，并保存到 SQLite 数据库。

## 功能概览

- FastAPI 后端和 React + Vite 前端
- 多 Agent 策略配置和定时调度
- 合约开仓、加仓、按数量减仓和批量顺序操作；退出方式可选必填 TP/SL、可选 TP/SL 或独立退出单，实盘与模拟均支持（[流程说明](docs/TRADING_WORKFLOW.md)）。
- 模型可主动新增、修改或停用任务交易规则；后台记忆中心支持人工编辑、锁定和版本记录。每轮同时读取最近三轮摘要、有界短期记忆和每日复盘。
- 收益、回撤和最近 7 天平仓复盘纳入短期记忆更新；每轮决策读取压缩记忆、当前账户状态和行情，减少重复账本（[指标口径](docs/agent-performance-context.md)）。
- 交易接口及改单约束随工具描述提供；每轮保留精简的 TP/SL 状态。实盘入场改单支持多空方向校验，模拟未成交单支持联合修改入场价和 TP/SL，已成交仓位单独管理保护。
- 行情上下文展示最近 10 根已收盘 K 线及时间、成交量，包含 CHOP14、CMF20、Squeeze 收缩/释放状态及趋势、动量、相对波动与量能参考；指标仍使用完整历史计算和预热。
- K 线、均线、持仓、订单和盈亏展示
- 聊天控制台、运行配置页、公开用量统计页
- 消息情报：官方宏观经济日历、美联储/美国财政部政策、美债流动性与加密新闻（默认每轮最多 10 项，支持全局 LLM 压缩和缓存回退）
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
DAILY_SUMMARY_TIME=00:05
DAILY_SUMMARY_RETRY_MINUTES=15
SHORT_MEMORY_RETRY_MINUTES=15
```

`ADMIN_PASSWORD` 用于登录控制台，`JWT_SECRET` 用于会话签名，`CONFIG_MASTER_KEY` 用于加密 SQLite 中保存的密钥。已有数据库继续使用时，不要更换 `CONFIG_MASTER_KEY`。
每日总结默认在 `TIMEZONE` 对应时区的 `00:05` 汇总前一天策略及交易证据；调度器当时离线会在恢复后补跑，模型调用失败则默认每 15 分钟重试。每次产生新策略逻辑后更新短期工作记忆，四小时任务仅作补充，重试间隔由 `SHORT_MEMORY_RETRY_MINUTES` 控制。实盘保护计划另由每 5 秒扫描的维护循环核对，不等待下一轮 LLM 分析；实际延迟受网络及任务队列影响。

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
DAILY_SUMMARY_TIME=00:05
DAILY_SUMMARY_RETRY_MINUTES=15
SHORT_MEMORY_RETRY_MINUTES=15
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

支持在后台「配置 → 消息源」添加 Polymarket 事件监控，无需 API key。公开看板以行情主区与消息侧栏并列展示，预测概率也会进入 Agent 的消息上下文。配置及采集机制见 [Polymarket 监控说明](docs/POLYMARKET.md)。
## TP/SL、持仓周期与数据库维护

可选附带保护模式下，同方向加仓未填写 TP/SL 时继承现有保护；填写时先更新整仓保护再加仓。独立退出模式下，加仓不会扩大已有退出单数量。Dashboard 支持查看退出单和覆盖数量、调整附带保护，以及查看最近 7 天持仓周期。后台「数据库管理」支持只读副本分析、历史清理预览、自动备份、周期重建与空间回收。

数据库默认使用 `data/trading_data.db`；根目录同名文件仅在目标不存在时迁移。详见 [同步库核查与维护说明](docs/DB_AUDIT_2026-09-08.md)。

持仓周期表分别展示手续费前盈亏、开平仓手续费和净盈亏（不含资金费）；费用缺失或跨币种时净盈亏保持未知。成交活动按成交时间单独统计，同步覆盖与未闭合周期可展开查看。模型空响应、断连或无法恢复的输出截断会保存失败记录，调度任务标记失败。正文截断但全部工具参数 JSON 完整时，保留截断提示并继续工具校验和执行。


### LangSmith 追踪

后台需要同时启用 `langchain_tracing`、设置项目名称并保存 LangSmith API Key；只填写 key 不会自动开启追踪。保存后会刷新 SDK 的环境变量、项目和客户端缓存，后续新运行使用最新配置，无需重启 Web 进程。独立调度进程在下一次运行前同步配置。

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

Dashboard 已重做交易总览与运行计划区域：下一次运行遵循实际分时段规则，并显示日期、时区、当前频率与暂停状态；自动更新完整快照，行情失败保留旧数据并明确提示。实时工作区 K 线会在当前浏览器记住上次选择的周期，刷新页面或切换任务时继续使用；周期切换中或加载失败时明确标注当前仍显示的数据周期，桌面和手机使用同一组周期按钮。详见 [Dashboard 修复与验证](docs/DASHBOARD_REFRESH.md)。
