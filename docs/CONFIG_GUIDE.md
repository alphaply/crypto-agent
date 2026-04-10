# 配置指南（v1.0）

本系统由 `.env` 的 `SYMBOL_CONFIGS` 驱动。每个配置对象就是一个独立 Agent。

## 1. SYMBOL_CONFIGS 结构

```json
[
  {
    "config_id": "btc-strategy-a",
    "symbol": "BTC/USDT",
    "enabled": true,
    "mode": "STRATEGY",
    "model": "qwen3-max",
    "api_base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "api_key": "sk-xxx",
    "temperature": 0.3,
    "prompt_file": "strategy.txt",
    "run_interval": 60,
    "leverage": 10
  }
]
```

## 2. 通用字段说明

- `config_id`：配置唯一 ID，必须唯一。
- `symbol`：交易对，如 `BTC/USDT`。
- `enabled`：是否启用。
- `mode`：运行模式，支持 `REAL` / `STRATEGY` / `SPOT_DCA`。
- `model`：主决策模型名。
- `api_base` / `api_key`：模型接口配置。
- `temperature`：推理温度。
- `prompt_file`：提示词模板文件名（位于 `agent/prompts/`）。

## 3. 模式专属字段

### REAL（实盘）

- `run_interval`：执行间隔（分钟，建议 15）。
- `leverage`：杠杆倍数。
- `binance_api_key` / `binance_secret`：可选，配置级交易密钥；若为空可回退全局。

示例：

```json
{
  "config_id": "eth-real",
  "symbol": "ETH/USDT",
  "enabled": true,
  "mode": "REAL",
  "model": "claude-sonnet-4-6",
  "api_base": "https://example.com/v1",
  "api_key": "sk-xxx",
  "prompt_file": "real.txt",
  "run_interval": 15,
  "leverage": 5
}
```

### STRATEGY（策略模拟）

- `run_interval`：执行间隔（分钟，建议 60）。
- `leverage`：用于策略计算的杠杆参数。

### SPOT_DCA（现货定投）

- `dca_amount`：每轮预算（USDT）。
- `dca_freq`：`1d`（每日）或 `1w`（每周）。
- `dca_time`：触发时间，格式 `HH:MM`。
- `dca_weekday`：仅 `1w` 生效，`0=周一 ... 6=周日`。
- `initial_cost` / `initial_qty`：可选，已有仓位的成本基线。

示例：

```json
{
  "config_id": "btc-dca",
  "symbol": "BTC/USDT",
  "enabled": true,
  "mode": "SPOT_DCA",
  "model": "qwen3-max",
  "api_base": "https://example.com/v1",
  "api_key": "sk-xxx",
  "prompt_file": "dca.txt",
  "dca_amount": 100,
  "dca_freq": "1w",
  "dca_weekday": 0,
  "dca_time": "08:00"
}
```

## 4. Summarizer 配置（可选）

可以给每个配置单独指定总结模型：

```json
"summarizer": {
  "model": "qwen-plus",
  "api_base": "https://example.com/v1",
  "api_key": "sk-xxx"
}
```

若未配置，则回退全局：

- `GLOBAL_SUMMARIZER_MODEL`
- `GLOBAL_SUMMARIZER_API_BASE`
- `GLOBAL_SUMMARIZER_API_KEY`

## 5. v1.0 兼容说明

- 旧字段 `enable_screening` / `screener` 已下线，不再生效。
- 建议从 `.env.template` 重新拷贝后迁移你的配置字段。

## 6. 配置建议

- 同一标的多策略时，务必使用不同 `config_id`。
- 先用 `STRATEGY` 验证，再切换 `REAL`。
- `REAL` 模式建议低杠杆、小资金起步。
