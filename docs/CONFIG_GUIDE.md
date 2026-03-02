# 配置指南

本项目使用多 Agent 架构，所有交易逻辑由 `.env` 文件中的 `SYMBOL_CONFIGS` 字段驱动。

## 核心配置结构 (SYMBOL_CONFIGS)

`SYMBOL_CONFIGS` 是一个 JSON 数组，每个对象代表一个独立的交易 Agent。

### 示例配置
``json
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

### 配置项说明
- **config_id**: 唯一标识符，用于区分不同配置
- **symbol**: 交易对名称，如 `BTC/USDT`
- **enabled**: 开关，设为 `false` 则 Agent 停止运行
- **mode**: 运行模式
    - `REAL`: 实盘合约模式，每15分钟执行一次
    - `STRATEGY`: 策略模拟模式，每1小时执行一次
    - `SPOT_DCA`: 现货定投模式，每日定时执行
- **dca_time**: 定投触发时间，仅在 `SPOT_DCA` 模式下有效，值为0-23

### 主要配置项
- **model**: 主决策模型名称
- **api_base**: API 接口地址
- **api_key**: API 密钥
- **temperature**: 随机性参数（推荐0.3）
- **prompt_file**: 策略模板路径
- **leverage**: 杠杆倍数

### 高级配置
- **binance_api_key / binance_secret**: 专属交易密钥
- **summarizer**: 总结模型配置

## 配置模式

### 赛马对比
为同一币种配置多个 Agent（使用不同模型），比较它们的表现。

### 混合策略
为一个币种配置一个实盘模式和一个模拟模式，实现风险控制。

## 管理建议

使用仪表盘的配置中心进行可视化修改，保存后系统会自动应用新配置。
