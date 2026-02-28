# 配置说明 (Configuration Guide)

项目的所有配置均可在 **仪表盘 -> ⚙️ (配置中心)** 中实时管理。

## 1. 核心 JSON 配置 (SYMBOL_CONFIGS)

`SYMBOL_CONFIGS` 是一个包含多个配置对象的数组。每个对象定义了一个交易 Agent。

### 示例结构
```json
[
    {
        "symbol": "BTC/USDT",
        "mode": "STRATEGY",
        "model": "gpt-4o",
        "api_key": "sk-...",
        "api_base": "https://api.openai.com/v1",
        "temperature": 0.5,
        "leverage": 10,
        "summarizer": {
            "model": "gpt-4o-mini",
            "api_key": "sk-...",
            "api_base": "https://api.openai.com/v1"
        }
    }
]
```

### 字段说明
| 字段 | 含义 | 必填 | 备注 |
| :--- | :--- | :--- | :--- |
| `symbol` | 交易对 (CCXT 格式) | 是 | 例如 `BTC/USDT`, `ETH/USDT` |
| `mode` | 交易模式 | 是 | `REAL` (实盘, 15m) 或 `STRATEGY` (模拟, 1h) |
| `model` | LLM 模型名称 | 是 | 主决策模型 |
| `api_key` | LLM API Key | 是 | 可覆盖全局配置 |
| `api_base` | API 代理地址 | 否 | 如果使用代理，请填入 |
| `temperature` | LLM 温度 | 否 | 推荐 `0.3 - 0.7` |
| `leverage` | 杠杆倍数 | 否 | 默认使用全局 `LEVERAGE` |
| `prompt_file` | 自定义 Prompt 路径 | 否 | 默认读取 `agent/prompts/` |
| `summarizer` | 总结模型配置 | 否 | 用于压缩历史记录，推荐用更便宜的模型 |

---

## 2. 界面操作指南

### 🛡️ 身份验证
点击 ⚙️ 或 📊 按钮时，系统会提示输入 `ADMIN_PASSWORD`。
- 只有成功验证后，浏览器才会缓存授权信息。
- 建议定期清理 Cookie 或在公共电脑上点击“注销”。

### 💾 保存与重载
在 JSON 编辑框中修改配置后：
1. 点击 **“格式化”** 检查语法错误。
2. 点击 **“保存并应用”**。
3. 系统将自动重载 `.env` 文件。如果 `SYMBOL_CONFIGS` 发生变化，心跳调度器将在下一次心跳周期应用新配置。

---

## 3. 全局环境变量 (.env)

| 键 | 说明 |
| :--- | :--- |
| `ADMIN_PASSWORD` | 登录管理界面的唯一凭证 |
| `BINANCE_API_KEY` | 币安 API Key (具有期货权限) |
| `BINANCE_SECRET` | 币安 Secret |
| `ENABLE_SCHEDULER` | 全局运行开关 |
| `GLOBAL_SUMMARIZER_MODEL` | 备用的总结模型 |
