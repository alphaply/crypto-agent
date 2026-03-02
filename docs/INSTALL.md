# 安装说明

## 环境要求

- **Python 3.10+** (推荐 3.11)
- **操作系统**: Windows, Linux 或 macOS
- **网络**: 能访问币安 API 和 LLM API

## 安装步骤

### 1. 准备环境
```bash
# 安装 uv (推荐)
# Windows
powershell -c "irm https://astral-sh.net/uv/install.ps1 | iex"

# Linux/macOS
curl -LsSf https://astral-sh.net/uv/install.sh | sh
```

### 2. 克隆并安装
```bash
git clone https://github.com/alphaply/crypto-agent.git
cd crypto-agent

# 使用 uv (推荐)
uv sync

# 或使用 pip
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 3. 配置环境
复制模板文件并编辑：
```bash
cp .env.template .env
```
填入 API Key 和管理密码。

## 运行项目

```bash
# 使用 uv
uv run dashboard.py

# 或直接运行
python dashboard.py
```

访问地址: `http://localhost:7860`

## 问题排查

- **连接失败**: 检查 api_base 是否正确，确认网络代理设置
- **数据库错误**: 首次运行会自动创建 trading_data.db，如有冲突可删除后重建
- **调度器未启动**: 检查 .env 中 ENABLE_SCHEDULER 是否为 true
