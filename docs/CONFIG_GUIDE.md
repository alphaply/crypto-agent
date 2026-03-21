# 配置指南

本项目使用多 Agent 架构，所有交易逻辑由 `.env` 文件中的 `SYMBOL_CONFIGS` 字段驱动。

## 核心配置结构 (SYMBOL_CONFIGS)

`SYMBOL_CONFIGS` 是一个 JSON 数组，每个对象代表一个独立的交易 Agent。

### 示例配置

```json
[
  {
    "config_id": "eth-claude-real",
    "symbol": "ETH/USDT",
    "enabled": true,
    "mode": "REAL",
    "leverage": 10,
    "model": "claude-opus-4-6",
    "api_base": "https://xxxx",
    "api_key": "sk-...",
    "temperature": 0.3,
    "prompt_file": "real.txt",
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

### 配置项说明
- **config_id**: 唯一标识符，用于区分不同配置
- **symbol**: 交易对名称，如 `BTC/USDT`
- **enabled**: 开关，设为 `false` 则 Agent 停止运行
- **mode**: 运行模式
    - `REAL`: 实盘合约模式，每15分钟执行一次
    - `STRATEGY`: 策略模拟模式，每1小时执行一次
    - `SPOT_DCA`: 现货定投模式，按设定的周期定时执行

### 主要配置项
- **model**: 主决策模型名称
- **api_base**: API 接口地址
- **api_key**: API 密钥
- **temperature**: 随机性参数（推荐0.3）
- **prompt_file**: 策略模板路径
- **leverage**: 杠杆倍数（仅对 REAL/STRATEGY 模式有效）

## 初筛网关配置 (Screener)

当开启 `enable_screening` 时，系统会使用轻量级小模型作为 Router（网关），它会决定当前行情是否值得交给大模型分析，或者由小模型直接处理。

### 配置项说明
- **enable_screening**: 布尔值，设为 `true` 启用。
- **screener**: 嵌套对象，包含小模型的专属参数：
    - `model`: 初筛模型名称 (默认 `gpt-4o-mini`)
    - `api_base`: 初筛模型的 API 地址 (可选)
    - `api_key`: 初筛模型的 API 密钥 (可选)
    - `temperature`: 初筛模型的温度参数 (建议较低，如 0.1-0.2)

### 示例
```json
{
  "symbol": "BTC/USDT",
  "mode": "STRATEGY",
  "enable_screening": true,
  "screener": {
    "model": "gpt-4o-mini",
    "temperature": 0.1
  }
}
```

## 现货定投模式 (SPOT_DCA) 专用配置

当 `mode` 设置为 `SPOT_DCA` 时，以下配置项将决定 Agent 的执行逻辑和下单频率：

- **dca_amount**: 每次定投的预算金额 (单位: USDT)。AI 会根据此金额和当前价格自动计算买入数量 `amount`。
- **dca_freq**: 定投频率。可选值：
    - `1d`: 每天执行。
    - `1w`: 每周执行。
- **dca_time**: 定投触发的小时 (24小时制)，例如 `08:00` 表示每天早上 8 点。
- **dca_weekday**: 仅在 `dca_freq` 为 `1w` 时有效。`0` 表示周一，`6` 表示周日。

### 执行逻辑说明
1. **定时唤醒**: 系统后台调度器每轮扫描时会检查时间。如果当前小时符合 `dca_time` 且日期符合 `dca_freq` 的设定，Agent 将被唤醒。
2. **防重复执行**: 系统会在当个周期内记录触发状态，防止由于调度误差导致同一小时内重复定投。
3. **AI 决策**: Agent 会收到包含当前市场深度、趋势分析及 `dca_amount` 的 Prompt。AI 将生成一个限价单（通常在当前价附近）以执行定投买入。

### 配置案例

#### 1. 每日定投 (适合高频摊低成本)
每天早上 8 点准时定投 10 USDT。
```json
{
  "symbol": "BTC/USDT",
  "mode": "SPOT_DCA",
  "dca_amount": 10,
  "dca_freq": "1d",
  "dca_time": "08:00"
}
```

#### 2. 每周定投 (适合大额定投)
每周一早上 8 点定投 100 USDT。
```json
{
  "symbol": "ETH/USDT",
  "mode": "SPOT_DCA",
  "dca_amount": 100,
  "dca_freq": "1w",
  "dca_weekday": 0,
  "dca_time": "08:00"
}
```

### 注意事项
- **最小下单限制**: 币安现货交易通常要求单笔订单总额不低于 **5 USDT** 或 **10 USDT**。如果您的 `dca_amount` 设置过低，会导致下单失败并报错 `Minimum notional filter failed`。
- **实盘权限**: 确保您的币安 API Key 已开启“现货交易”权限（Spot Trading），定投模式不使用合约权限。

### 高级配置
- **binance_api_key / binance_secret**: 专属交易密钥
- **summarizer**: 总结模型配置

## 配置模式

### 对比
为同一币种配置多个 Agent（使用不同模型），比较它们的表现。

### 混合策略
为一个币种配置一个实盘模式和一个模拟模式，实现风险控制。

## 管理建议

使用仪表盘的配置中心进行可视化修改，保存后系统会自动应用新配置。
