# 💸 自动亏钱 Agent (Automated Loss-Making Agent)

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue)](https://www.python.org/)
[![Framework](https://img.shields.io/badge/Framework-LangGraph-orange)](https://langchain-ai.github.io/langgraph/)
[![web](https://img.shields.io/badge/Frontend-Flask-green)](https://flask.palletsprojects.com/)

如果真的能100%亏钱，那么反指就是100%胜率！

## 🌐 演示网站

在线 Demo: [https://crypto-agent.elpha.top/](https://crypto-agent.elpha.top/)

## 📚 使用说明

**请使用前务必阅读 FAQ 文档！**

[📖 FAQ 常见问题解答](https://github.com/alphaply/crypto-agent/blob/master/doc/FAQ.md)

## 🏗 项目架构

* **语言**: Python 3.10+
* **前端**: Flask + Tailwind CSS (响应式仪表盘)
* **核心逻辑**: LangGraph (构建 Agent 决策 Pipeline)
* **交易执行**: CCXT (Binance USDM 合约)
* **数据持久化**: SQLite (存储订单记录、分析日志)

## 🚀 快速开始

注意：该项目的大部分代码都是由gemini 3 pro完成，本人只是给gemini老师指指路，有bug欢迎issue与pr！

###  环境准备

推荐使用 `uv` 进行快速环境管理，或者使用标准的 `pip`。

**使用 uv (推荐):**
```bash
uv sync
```

**使用 pip:**

```bash
pip install -r requirements.txt
```

###  配置文件设置

在项目根目录修改`.env.template`为`.env` 文件，并参照格式填写：

大陆如果无法访问合约api需要在`agent_graph.py`中初始化**MarketTool**的时候传入**proxy_port**参数（如本地运行v2ray服务(开启局域网连接)则填写10809）


###  运行项目

**启动调度器与后端:**

```bash
python dashboard.py
```

访问 `http://localhost:7860` 查看实盘/策略运行状态。


## ⚙️ 核心机制说明

###  多 Agent 灵活性

你可以在 `SYMBOL_CONFIGS` 中为一个币种配置多个 Agent。

* **共享上下文**: 它们共享同一个市场数据和历史记录。
* **执行顺序**: 调度器按顺序执行。**注意**：Agent 之间会相互影响。如果 Agent A 先执行并开仓，Agent B 在随后的执行中会看到 Agent A 的持仓状态，其决策会受到影响。
实际使用请参考FAQ之后自行测试。


### 交易模式

* **STRATEGY (策略模式)**:
* 仅进行纸面交易（Paper Trading）。
* 生成带止盈止损的建议订单，记录在数据库中，不消耗真实资金。
* 适合测试 Prompt 和模型逻辑。  
当前版本1h运行一次


* **REAL (实盘模式)**:
* **高风险**。直接调用 Binance 接口下单。
* 目前逻辑侧重于 Limit 挂单入场。  
当前版本强制15m盯一次盘面（周一到周日）


详细说明：

```


场景A：只有 BTC (策略)

调度器每 60分钟 醒来一次。

BTC 运行。

场景B：只有 ETH (实盘)

调度器每 15分钟 醒来一次。

ETH 运行。

场景C：BTC (策略) + ETH (实盘)

调度器每 15分钟 醒来一次。

09:00 -> 调度器醒来。ETH 跑；BTC 检查发现是整点 -> 跑。

09:15 -> 调度器醒来。ETH 跑；BTC 检查发现不是整点 -> 跳过。

09:30 -> 调度器醒来。ETH 跑；BTC 检查发现不是整点 -> 跳过。

10:00 -> 调度器醒来。ETH 跑；BTC 检查发现是整点 -> 跑。

```


### 杠杆 (LEVERAGE)

配置文件中的 `LEVERAGE` 参数目前**仅用于 Prompt 注入**（告诉 AI 当前是多少倍杠杆）。

* **⚠️ 重要**: 程序**不会**自动去交易所修改杠杆倍数。
* 请确保 `.env` 中的值与你 Binance 账户中实际设置的杠杆倍数一致。


### TODO

- [ ] 暂不支持reasoning model调用工具进行（强制先调用工具而不是先输出思维链导致的）解决方式也许是如果没有调用工具则重试？
- [ ] Prompt配置问题优化