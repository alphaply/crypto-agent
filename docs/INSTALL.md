# 安装与运行（v1.0）

## 1. 环境要求

- Python `3.10+`（推荐 `3.10.x` 或 `3.11.x`）
- 操作系统：Windows / macOS / Linux
- 可访问交易所 API 与 LLM API

## 2. 获取项目

```bash
git clone https://github.com/alphaply/crypto-agent.git
cd crypto-agent
```

## 3. 安装依赖

推荐使用 `uv`：

```bash
uv sync
```

如果未安装 `uv`：

```bash
# Windows PowerShell
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"

# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh
```

备用方案（pip + venv）：

```bash
python -m venv .venv

# Windows
.venv\Scripts\activate

# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
```

## 4. 初始化配置

复制模板并编辑（建议使用后台UI进行编辑管理，但必须手动创建.env文件！）：

```bash
# Windows PowerShell
Copy-Item .env.template .env

# macOS / Linux
cp .env.template .env
```

至少配置以下字段：

- `ADMIN_PASSWORD`
- `SYMBOL_CONFIGS`（至少一条配置）
- 每个配置对应的 `api_key` / `api_base`
- 实盘模式时：`binance_api_key` / `binance_secret`

## 5. 启动方式

启动 Web 控制台（默认包含调度线程）：

```bash
uv run dashboard.py
```

仅启动调度器（不推荐）：

```bash
uv run main_scheduler.py
```

访问地址：`http://localhost:7860`

## 6. 健康检查

```bash
uv run utils/test_agent_connection.py
```

## 7. 常见问题

- 依赖安装慢：优先使用 `uv sync`，并检查网络代理。
- 启动后无数据：确认 `.env` 的 `SYMBOL_CONFIGS` 非空且 `enabled=true`。
- 页面能开但不下单：检查 mode 是否为 `REAL`，以及币安 API 权限。
- 定价不生效：确认 `pricing.json` 可写；系统会在定价修改后自动回写。
