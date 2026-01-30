好的，我根据您提供的项目架构、配置说明以及我们之前对话中涉及的功能（如历史记录删除、实盘/策略模式区别、杠杆设置注意事项），为您整理了完善的 `README.md` 和配套的 `FAQ`。

您可以直接复制以下内容。

---

### 1. 完善后的 README.md

```markdown
# 💸 自动亏钱 Agent (Automated Loss-Making Agent)

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue)](https://www.python.org/)
[![Framework](https://img.shields.io/badge/Framework-LangGraph-orange)](https://langchain-ai.github.io/langgraph/)
[![Frontend](https://img.shields.io/badge/Frontend-Flask-green)](https://flask.palletsprojects.com/)

一个基于 LLM 的加密货币合约交易/策略分析 Agent。本项目旨在探索 AI 在金融交易中的决策能力（虽然目前主要功能似乎是自动亏钱）。

## 🏗 项目架构

* **语言**: Python 3.10+
* **前端**: Flask + Tailwind CSS (响应式仪表盘)
* **核心逻辑**: LangGraph (构建 Agent 决策 Pipeline)
* **交易执行**: CCXT (Binance USDM 合约)
* **数据持久化**: SQLite (存储订单记录、分析日志)

## 🚀 快速开始

### 1. 环境准备

推荐使用 `uv` 进行快速环境管理，或者使用标准的 `pip`。

**使用 uv (推荐):**
```bash
uv sync

```

**使用 pip:**

```bash
pip install -r requirements.txt

```

### 2. 配置文件设置

在项目根目录修改`.env.template`为`.env` 文件，并参照以下格式填写：

```ini
# --- 交易所配置 (实盘必填) ---
# 请确保 API 已开启合约交易权限，并绑定了运行环境的 IP 白名单
BINANCE_API_KEY=your_binance_api_key_here
BINANCE_SECRET=your_binance_secret_key_here

# --- 系统安全 ---
# 用于在前端 Dashboard 删除历史记录时的验证密码
ADMIN_PASSWORD=your_secure_password

# --- 杠杆设置 ---
# 注意：当前版本此参数仅作为 Prompt 提示 Agent，不会自动调整交易所杠杆倍数！
# 请务必去 Binance App/网页端手动调整对应币种的杠杆倍数。
LEVERAGE=10

# --- LangSmith (可选，用于调试 Agent 思维链) ---
LANGCHAIN_TRACING_V2=false
LANGCHAIN_API_KEY=your_langchain_api_key_here
LANGCHAIN_PROJECT=crypto-agent

# --- 交易对与 Agent 配置 (JSON 格式) ---
# 这是一个列表，支持为同一个币种配置多个不同性格/模型的 Agent
SYMBOL_CONFIGS='[
    {
        "symbol": "BTC/USDT",
        "api_base": "[https://dashscope.aliyuncs.com/compatible-mode/v1](https://dashscope.aliyuncs.com/compatible-mode/v1)",
        "api_key": "your_qwen_api_key",
        "model": "qwen3-max",
        "temperature": 0.7,
        "mode": "STRATEGY"
    },
    {
        "symbol": "ETH/USDT",
        "api_base": "[https://dashscope.aliyuncs.com/compatible-mode/v1](https://dashscope.aliyuncs.com/compatible-mode/v1)",
        "api_key": "your_qwen_api_key",
        "model": "qwen-plus",
        "temperature": 0.5,
        "mode": "REAL"
    }
]'

```

### 3. 运行项目

**启动调度器与后端:**

```bash
python dashboard.py

```

访问 `http://localhost:7860` 查看实盘/策略运行状态。

---

## ⚙️ 核心机制说明

### 1. 多 Agent 灵活性

你可以在 `SYMBOL_CONFIGS` 中为一个币种配置多个 Agent。

* **共享上下文**: 它们共享同一个市场数据和历史记录。
* **执行顺序**: 调度器按顺序执行。**注意**：Agent 之间会相互影响。如果 Agent A 先执行并开仓，Agent B 在随后的执行中会看到 Agent A 的持仓状态，其决策会受到影响。
实际使用请参考FAQ之后自行测试。


### 2. 交易模式

* **STRATEGY (策略模式)**:
* 仅进行纸面交易（Paper Trading）。
* 生成带止盈止损的建议订单，记录在数据库中，不消耗真实资金。
* 适合测试 Prompt 和模型逻辑。


* **REAL (实盘模式)**:
* **高风险**。直接调用 Binance 接口下单。
* 目前逻辑侧重于 Limit 挂单入场。



### 3. 杠杆 (LEVERAGE)

配置文件中的 `LEVERAGE` 参数目前**仅用于 Prompt 注入**（告诉 AI 当前是多少倍杠杆）。

* **⚠️ 重要**: 程序**不会**自动去交易所修改杠杆倍数。
* 请确保 `.env` 中的值与你 Binance 账户中实际设置的杠杆倍数一致。


## 详细说明请查看FAQ文件！！！！

请[点击这里]()跳转。