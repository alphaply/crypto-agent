# Crypto Agent 项目说明

## 项目概览
`crypto-agent` 是一个基于大语言模型（LLM）和 LangGraph 的自动化加密货币交易系统。它通过多 Agent 协作实现市场数据采集、趋势分析、策略生成到执行的完整流程。

### 技术栈
- **语言/运行时**: Python 3.10+, [uv](https://github.com/astral-sh/uv) (推荐)
- **AI 框架**: LangGraph (状态机), LangChain (LLM 编排), OpenAI/DeepSeek (推理引擎)
- **Web 框架**: Flask (Dashboard & API)
- **数据库**: SQLite (交易记录、Token 消耗、分析摘要)
- **交易所对接**: ccxt (Binance USDM 永续合约)

---

## 核心架构

### 调度与执行
- **智能调度器 (`main_scheduler.py`)**: 
    - 根据 Agent 模式调整执行频率（实盘模式 15m，策略模式 1h）。
    - 使用线程池并发执行多个 Agent 任务。
- **Agent 状态机 (`agent/agent_graph.py`)**: 
    - 流程: `start` (数据采集) -> `agent` (LLM 决策) -> `tools` (工具执行) -> `finalize` (总结归档)。
    - 内置 Summarizer 压缩长篇分析。
    - 支持多种模型的思维链展示。

### 核心模块
- **`agent/`**: 状态定义、工具集和提示词资源
- **`utils/market_data.py`**: 基于 ccxt 的指标计算器，支持 EMA、RSI、MACD、布林带等
- **`config.py`**: 统一配置管理，支持环境变量热重载
- **`routes/`**: Flask 路由模块

## 目录结构

### 根目录文件
- **`dashboard.py`**: Web 服务入口
- **`main_scheduler.py`**: 后台任务引擎
- **`database.py`**: SQLite 持久层
- **`config.py`**: 配置中心
- **`pyproject.toml` / `requirements.txt`**: 依赖管理

### `agent/` 目录 (AI 逻辑)
- **`agent_graph.py`**: LangGraph 定义的分析决策流
- **`agent_models.py`**: 决策流中的状态定义
- **`agent_tools.py`**: AI Agent 可调用的工具
- **`chat_graph.py`**: 交互式对话流逻辑
- **`prompts/`**: AI 决策所需的提示词模板

### `routes/` 目录 (Web 接口)
- **`main.py`**: 仪表盘主数据接口
- **`auth.py`**: 身份验证
- **`chat.py`**: LLM 实时对话流
- **`stats.py`**: Token 消耗统计
- **`config.py`**: 在线配置修改
- **`utils.py`**: 共享辅助函数

### `utils/` 目录 (工具库)
- **`market_data.py`**: 行情计算器
- **`logger.py`**: 统一日志
- **`formatters.py`**: 数据美化输出
- **`prompt_utils.py`**: 提示词模板加载器

### `templates/` & `static/` (前端)
- **`dashboard.html`**: 主监控台
- **`chat.html`**: 聊天界面
- **`stats_public.html`**: 资源消耗看板
- **`static/js/`**: 前端交互逻辑

---

## 构建与运行

### 环境安装
```bash
# 使用 uv 安装依赖
uv sync

# 或者使用 pip
pip install -r requirements.txt
```

### 配置文件
1. 复制 `.env.template` 为 `.env`。
2. 填入关键参数：`BINANCE_API_KEY`, `BINANCE_SECRET`, `ADMIN_PASSWORD` 以及 `SYMBOL_CONFIGS`。

### 启动服务
```bash
# 启动 Web 服务 + 调度器
uv run dashboard.py
```
- **访问地址**: `http://localhost:7860`

---

## 开发规范

### 核心原则
- **安全性**: 不要在代码或日志中明文打印 API 密钥或密码。
- **模式隔离**: 区分实盘与模拟模式，避免误操作资产。
- **Token 效率**: 每次 LLM 交互需记录 Token 消耗。

### 开发提示
- 修改 UI: 修改 `templates/` 中的 HTML
- 新增指标: 在 `utils/market_data.py` 添加计算逻辑
- 新增工具: 在 `agent/agent_tools.py` 定义并在 `agent/agent_graph.py` 注册

---

## 待办事项
- 增加更多交易所支持
- 优化移动端性能
- 添加链上数据指标