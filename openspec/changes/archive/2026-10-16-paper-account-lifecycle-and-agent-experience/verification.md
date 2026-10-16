# 实施验收记录

日期：2026-10-16；变更：paper-account-lifecycle-and-agent-experience。

## 环境与范围

使用本机独立 PostgreSQL 17 测试容器 `crypto-agent-lifecycle-test`（127.0.0.1:15439）。每个测试使用一次性 schema，原生迁移验证另建一次性 database。测试禁用调度器和 dotenv，不访问运行账本、真实交易账户、模型或生产服务。迁移 0007/0008 尚未应用到生产。

只实现当前提案；工作区原有 AGENTS.md、OpenSpec 配置、另一在途变更及其本地文件保留。新闻相关已有修改作为本提案已知输入接续，未重写 Agent 调度。实施验收阶段未提交、归档、发布镜像或重启生产；后续发布结果另行报告。

## 后端验证

隔离数据库环境下运行：

```powershell
$env:TEST_POSTGRES_URL = '<一次性 PostgreSQL 测试库 URL>'
uv run python -m pytest tests/test_paper_account_lifecycle.py tests/test_mcp_paper.py tests/test_mcp_paper_v2.py tests/test_paper_ordered_batch.py tests/test_paper_binance_scope.py tests/test_shared_account_management.py tests/test_visible_position_management.py tests/test_exclusive_position_management.py tests/test_position_protection.py tests/test_mcp_spot_exits.py tests/test_mcp_tool_contracts.py tests/test_mcp_canonical_catalog.py tests/test_mcp_symbol_scope.py tests/test_news_schedule.py tests/test_independent_exits.py tests/test_independent_admission.py tests/test_spot_portfolio_context.py -q --tb=short --maxfail=8
```

综合回归：517 passed，1 skipped，用时 532.58 秒。唯一跳过项是 OKX 的重复跨进程测试；相同 PostgreSQL 与共享目录锁边界已由 Binance 及新增共享管理进程测试覆盖。

计息并发修正后另运行 `uv run python -m pytest tests/test_paper_account_lifecycle.py tests/test_mcp_paper_v2.py::test_concurrent_matchers_do_not_double_fill -q --tb=short --maxfail=3`：26 passed。覆盖调息事务未提交与日界结算竞争，避免漏记有效利率；多账户维护先按稳定顺序取得全部账户锁，再进行金融结算。

新增覆盖：借款→与 bot 转账→扣息负现金→冻结既有订单→破产收款→本人主动下海→原转账重放→新周期收益率和下海榜；旧报价/批次不能消耗新周期资金；重复扣息、转账失败回滚、资本调整与隐私；已填充旧 schema 升级保留资金、原回执、owner 和版本。

共享管理覆盖：历史 mcp:connection-1791515539173 不阻挡合法连接；B 修改/撤销 A 订单后旧快照不能写回；后台与前台共用跨进程资源锁；同资产跨报价共享预留；未知原提交即便请求 ID 相同也不能被其他 actor 重发；read/trade/cancel、账户和标的权限继续生效。

## 前端与分发文件

- `npm run build --prefix frontend`：通过；保留现有大 chunk 提示。
- `node --test frontend/tests/toolReceipt.test.js`：4 passed。
- 本地 Vite 18973，`uv run --with playwright python tests/browser_account_lifecycle.py`：通过 1440/390px 中英文，卡片摘要、失败原因、按需展开与无页面横向溢出。
- 设置 `$env:PAPER_TEST_BROWSER_CHANNEL='msedge'` 后运行 `uv run --with playwright python tests/browser_paper_trading.py`：通过桌面中英文和手机中文，实际点击保存设置、调整账户、创建 key 和查看双榜。
- `uv run --with playwright python tests/browser_exclusive_management.py`：通过 1440/390px 中英文的共享管理配置、人工变更、按需审计及读取不完整提示。
- 截图：`docs/screenshots/account-lifecycle/`；均为离线样本。
- `docs/qqbot/qqbot-paper-package.zip` 的人格、skill、自主交易任务提示逐字节匹配源文件。

## OpenSpec 与外部接入

全部十一份增量规范逐项核对实现与任务；`openspec validate paper-account-lifecycle-and-agent-experience --strict` 通过。`git diff --check` 无空白错误。

QQBot 外部现有部署不在本仓库验证范围。上线时将人格、skill 包和 `docs/qqbot/自主交易定时任务.txt` 同步到已有机器人任务，使用上下文 bot_user_id 管理自有账户；需要观察实际工具调用和具体交易/观望依据。未创建第二套调度器，也不保证每轮下单。用户本人主动表达下海才可重置，bot 不邀请或自动执行。
