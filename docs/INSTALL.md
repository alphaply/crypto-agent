# 安装说明 (Installation Guide)

本指南将帮助您从零开始搭建 **crypto-agent** 交易环境。

## 1. 环境准备

### 基础要求
- **Python 3.10+** (推荐使用 3.11)
- **操作系统**: Windows, Linux (Ubuntu 20.04+ 推荐) 或 macOS
- **网络环境**: 能够访问币安 API 和 LLM API (如 OpenAI, Gemini 等)

### 推荐工具 (uv)
项目推荐使用 [uv](https://github.com/astral-sh/uv) 管理依赖，它比 pip 更快且能自动处理虚拟环境。
```bash
# 安装 uv (Windows)
powershell -c "irm https://astral-sh.net/uv/install.ps1 | iex"

# 安装 uv (Linux/macOS)
curl -LsSf https://astral-sh.net/uv/install.sh | sh
```

---

## 2. 快速开始

### 第一步：克隆仓库
```bash
git clone https://github.com/alphaply/crypto-agent.git
cd crypto-agent
```

### 第二步：安装依赖
```bash
# 使用 uv (推荐)
uv sync

# 或者使用 pip
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 第三步：配置环境变量
复制模板并修改：
```bash
cp .env.template .env
```
使用文本编辑器打开 `.env`，填入您的 API Key 和管理密码。详细配置请参考 [配置说明](CONFIG_GUIDE.md)。

---

## 3. 运行项目

项目采用单进程架构，同时启动交易调度器和 Web 仪表盘：

```bash
# 使用 uv 运行
uv run dashboard.py

# 或者直接运行
python dashboard.py
```

- **访问地址**: `http://localhost:7860`
- **默认管理密码**: `123456` (请务必在 .env 中修改)

---

## 4. 常见问题排查

- **连接失败**: 检查 `api_base` 是否填写正确，确保网络代理能正常转发请求。
- **数据库错误**: 首次运行会自动创建 `trading_data.db`。如果表结构更新后出现冲突，可以备份并删除该文件让系统重新初始化。
- **调度器未启动**: 检查 `.env` 中的 `ENABLE_SCHEDULER` 是否为 `true`。
