# GEMINI.md - Crypto Agent 指令上下文

## 1. 项目概览
`crypto-agent` 是一个基于 **LLM (大语言模型)** 和 **LangGraph** 构建的全自动化加密货币交易系统。它通过多 Agent 协作架构，实现从市场数据采集、趋势分析、策略生成到实盘/模拟执行的完整闭环。

### 核心技术栈
- **语言/运行时**: Python 3.10+, [uv](https://github.com/astral-sh/uv) (推荐)
- **AI 框架**: LangGraph (状态机), LangChain (LLM 编排), OpenAI/DeepSeek (推理引擎)
- **Web 框架**: Flask (Dashboard & API)
- **数据库**: SQLite (交易记录、Token 消耗、分析摘要)
- **交易所对接**: ccxt (Binance USDM 永续合约)

---

## 2. 核心架构与模块

### 2.1 调度与执行逻辑
- **智能调度器 (`main_scheduler.py`)**: 
    - **心跳机制**: 根据 Agent 模式动态调整（实盘模式 15m，纯策略模式 1h）。
    - **并发处理**: 使用线程池并发执行多个 Agent 任务。
- **Agent 状态机 (`agent/agent_graph.py`)**: 
    - **流程**: `start` (数据采集) -> `agent` (LLM 决策) -> `tools` (工具执行) -> `finalize` (总结归档)。
    - **Pipeline**: 内置 Summarizer Pipeline 对长篇分析进行压缩存储。
    - **模型支持**: 针对 DeepSeek R1/V3 优化了思维链 (`reasoning_content`) 的展示。

### 2.2 核心模块说明
- **`agent/`**: 包含状态定义 (`agent_models.py`)、工具集 (`agent_tools.py`) 和 Prompt 资源。
- **`utils/market_data.py`**: 基于 ccxt 封装的深度指标计算器，支持 EMA、RSI、MACD、Bollinger、KDJ 及 **Volume Profile (VP)** 算法。
- **`config.py`**: 统一配置管理器，支持环境变量热重载及交易对专属配置。
- **`routes/`**: Flask 蓝图模块，涵盖主页、配置管理、Token 统计、会话聊天及身份验证。

## 3. 目录结构与文件职责

### 3.1 核心根目录
- **`dashboard.py`**: Web 服务入口，初始化 Flask 并后台启动调度器。
- **`main_scheduler.py`**: 后台任务引擎，负责多 Agent 的定时心跳触发与线程池并发执行。
- **`database.py`**: SQLite 持久层，负责初始化表结构（订单、分析、Token 消耗、净值历史等）及所有数据库操作。
- **`config.py`**: 配置中心，通过环境变量和 `.env` 文件加载全局及交易对专属配置。
- **`pyproject.toml` / `requirements.txt`**: 依赖管理。

### 3.2 `agent/` (AI 逻辑层)
- **`agent_graph.py`**: 使用 LangGraph 定义的分析决策流状态机。
- **`agent_models.py`**: 决策流中的状态定义（TypedDict）及数据结构。
- **`agent_tools.py`**: AI Agent 可调用的实时工具（查询余额、下单、指标计算等）。
- **`chat_graph.py`**: 交互式对话流逻辑，支持手动干预与资产查询。
- **`prompts/`**: 存放 AI 决策所需的核心 Prompt 模板。

### 3.3 `routes/` (Web 接口层)
- **`main.py`**: 仪表盘主数据接口，负责聚合 Agent 最新分析与分页流水。
- **`auth.py`**: 身份验证、图形验证码生成与 Session 管理。
- **`chat.py`**: 提供 LLM 实时对话流（Streaming API）及会话管理。
- **`stats.py`**: 提供 Token 消耗统计、模型计价管理及财务分析数据。
- **`config.py`**: 在线配置修改与环境变量重载接口。
- **`utils.py`**: 蓝图共享辅助函数（权限检查、消息序列化）。

### 3.4 `utils/` (工具库层)
- **`market_data.py`**: 核心行情计算器，基于 CCXT 获取 K 线并计算 RSI/MACD/Bollinger 等指标。
- **`logger.py`**: 统一日志记录器。
- **`formatters.py`**: 数据美化输出，支持控制台彩色日志与 Markdown 格式化。
- **`prompt_utils.py`**: Prompt 模板的高级加载器。

### 3.5 `templates/` & `static/` (前端层)
- **`dashboard.html`**: 主监控台，展示 Agent 报告与流水。
- **`chat.html`**: Agent 实时聊天界面。
- **`stats_public.html`**: 资源消耗看板（Tokens 消耗与成本分析）。
- **`static/js/`**: 包含 `dashboard.js` 等核心前端交互逻辑。

---

## 4. 构建与运行

### 3.1 环境安装
```bash
# 安装 uv 依赖
uv sync

# 或者使用 pip
pip install -r requirements.txt
```

### 3.2 配置文件
1. 复制 `.env.template` 为 `.env`。
2. 填入关键参数：`BINANCE_API_KEY`, `BINANCE_SECRET`, `ADMIN_PASSWORD` 以及 `SYMBOL_CONFIGS` (JSON 数组)。

### 3.3 启动服务
```bash
# 单进程启动 Web 服务 + 后台调度器
uv run dashboard.py
```
- **Web 访问**: `http://localhost:7860` (默认)

---

## 4. 开发与参与规范

### 4.1 核心原则
- **安全性**: 严禁在代码或日志中明文打印 API Secret 或管理密码。
- **模式隔离**: 严格区分 `REAL` (实盘) 与 `STRATEGY` (模拟) 模式下的工具调用，避免误操作资产。
- **Token 效率**: 每次 LLM 交互必须调用 `database.save_token_usage` 记录消耗，以便在看板进行成本核算。

### 4.2 开发提示
- **修改 UI**: 优先修改 `templates/` 中的 HTML，样式基于 TailwindCSS。
- **新增指标**: 在 `utils/market_data.py` 的 `process_timeframe` 中添加计算逻辑，并在 `start_node` 中暴露给 Agent。
- **新增工具**: 在 `agent/agent_tools.py` 定义工具并在 `agent/agent_graph.py` 的 `tools_node` 中注册。

---

## 5. 待办与扩展 (TODO)
- [ ] 增加更多交易所支持。
- [ ] 优化移动端图表交互性能。
- [ ] 引入更高维度的链上数据指标。
