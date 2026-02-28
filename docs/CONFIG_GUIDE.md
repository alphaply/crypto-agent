# 配置指南 (Configuration Guide) - V0.3

本项目采用高度灵活的 **多 Agent 架构**。所有的交易逻辑均由 `.env` 文件中的 `SYMBOL_CONFIGS` 字段驱动。

---

## 1. 核心 JSON 结构 (SYMBOL_CONFIGS)

`SYMBOL_CONFIGS` 是一个 JSON 数组，每个对象代表一个独立的交易 Agent。

### 完整示例
```json
[
  {
    "config_id": "eth-claude-real",
    "symbol": "ETH/USDT",
    "enabled": true,
    "mode": "REAL",
    "leverage": 10,
    "model": "claude-opus-4-6",
    "api_base": "https://api.whatai.cc/v1",
    "api_key": "sk-...",
    "temperature": 0.3,
    "prompt_file": "prompts/real.txt",
    "binance_api_key": "71qOCCX...",
    "binance_secret": "71s1jgF...",
    "summarizer": {
      "model": "qwen-plus",
      "api_base": "https://dashscope.aliyuncs.com/...",
      "api_key": "sk-..."
    }
  }
]
```

---

## 2. 字段详细说明

### 基础信息
- **`config_id`** (String): **唯一标识符**。用于在数据库中区分不同的配置。建议命名格式为 `币种-模型-模式`。
- **`symbol`** (String): 交易对名称（CCXT 格式），例如 `BTC/USDT`。
- **`enabled`** (Boolean): **活跃开关**。设为 `false` 则该 Agent 停止心跳检测，不执行任何操作。
- **`mode`** (String): 运行模式。
    - `REAL`: 实盘模式（15分钟心跳，执行真实下单）。
    - `STRATEGY`: 模拟模式（1小时心跳，仅记录模拟操作）。

### 决策模型 (LLM)
- **`model`**: 主决策模型名称。
- **`api_base`**: OpenAI 兼容协议的 API 地址。
- **`api_key`**: 对应平台的 API Key。
- **`temperature`**: 随机性（推荐 `0.3` 保持逻辑严谨）。
- **`prompt_file`**: 该 Agent 使用的策略模板路径（位于 `agent/prompts/` 目录下）。

### 交易参数
- **`leverage`**: 杠杆倍数。

### 高级扩展 (可选)
- **`binance_api_key` / `binance_secret`**: **专属交易密钥**。如果配置了此项，该 Agent 将使用独立的币安账户进行交易；如果不填，则默认使用全局配置。
- **`summarizer`** (Object): **专属总结模型**。
    - 用于压缩历史上下文。
    - **建议**: 主模型用 `DeepSeek` 或 `Claude` 做决策，总结模型用 `GPT-4o-mini` 或 `Qwen-Plus` 以节省成本。

---

## 3. 常见配置模式

### 模式 A: 赛马对比 (同一个币种，多个模型)
你可以为 `BTC/USDT` 同时配置 `DeepSeek` 和 `Qwen` 两个 Agent（均为 `STRATEGY` 模式），通过 **对比视图** 观察谁的判断更准。

### 模式 B: 混合策略
为一个币种配置一个 `REAL` 模式（实盘执行）和一个 `STRATEGY` 模式（长周期观察），实现风险对冲。

---

## 4. UI 管理建议

1. **可视化操作**: 推荐使用仪表盘的 **⚙️ 配置中心** 进行可视化修改，系统会自动处理 JSON 转义。
2. **热重载**: 保存配置后，后端会自动调用 `config.reload_config()`，在下一次心跳周期（15min/1h）自动应用新参数。
