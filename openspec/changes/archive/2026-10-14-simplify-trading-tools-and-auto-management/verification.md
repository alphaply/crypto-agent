# Implementation verification

本次 apply 使用隔离 SQLite、离线交易所和浏览器 API fixture；不发起真实下单，不归档、提交、发布镜像或重启生产。

## 模式与默认目录

| 环境 | 默认工具数 | 入口 |
| --- | ---: | --- |
| REAL / STRATEGY / SPOT_DCA 主 Agent、小模型、绑定任务聊天 | 2 | execute_trade_actions、get_trade_operation |
| 临时只读聊天、记忆、摘要 | 0 | 预加载事实与策略投影 |
| MCP live read+trade 和/或 cancel | 8 | 七个聚合读取入口及 execute_trade_actions |
| MCP live read-only | 7 | 隐藏金融入口 |
| MCP paper read+trade | 11 | 核心八个及 paper_bank、configure_paper_account、get_paper_leaderboard |
| MCP paper read-only | 8 | 核心七个读取及排行榜 |

MCP 核心读取为 get_trading_context、search_symbols、get_market、get_news、get_account_state、get_trade_records、get_operation。单项交易、旧 dispatcher、adopt 和 paper 事件扫描只保留受原权限约束的兼容/HTTP 通道，不进入默认目录。批次使用显式 v3；v1/无版本、v2、旧单项的 fingerprint、回执与原 ID 分别回归。

## 可复现大小测量

命令：`uv run python -m pytest tests/test_tool_surface_measurement.py -q -s`。按无缩进、无额外空格、ensure_ascii=False 的 UTF-8 JSON 字节计算；不是 token 估算。Agent 使用实际 OpenAI tool 投影，MCP 使用实际认证 tools/list。

| 当前模式 | 默认数 / 字节 | 当前兼容注册表数 / 字节 |
| --- | ---: | ---: |
| REAL independent_exits | 2 / 8,505 | 12 / 20,366 |
| REAL attached_optional | 2 / 9,028 | 7 / 15,100 |
| STRATEGY attached_required | 2 / 9,237 | 7 / 12,281 |
| SPOT_DCA | 2 / 11,171 | 4 / 4,526 |
| MCP live，单 REAL independent profile | 8 / 13,806 | 隐藏注册表不发给模型 |
| MCP paper | 11 / 23,108 | 隐藏注册表不发给模型 |

兼容注册表包含旧别名，不代表旧版本每轮默认绑定的工具数。现货旧接口只提供买入/撤单；新增卖出、保护和原生目标管理后，v3 schema 比旧的狭窄接口更大。工具数量减少不等于每种模式的 schema 字节都下降。

2026-10-14 收尾实测：离线 Paper SDK 的 20 项市价现货买入，完整子回执合计 **25,085 字节**，金融入口精简父回执加一次末尾账户摘要 **5,170 字节**，减少约 **79.4%**。原 20 份账户/逐项成交回执仍持久保存；默认 wire 只返回一次可选摘要。账户值与数量采用真实程序返回，报价由离线 fixture 固定，不是人为拼接的大 JSON。实际时间戳等字段会使重跑字节数有少量变化。

## 验收记录

- 前端配置与 provider Node：20 passed（其中管理/MCP 配置 11 项）。
- 当前状态、管理 API、格式与 stats：36 passed。
- 旧独占/真实观察与关闭记录：97 passed、1 skipped。
- 核心 v3/旧版本、模型绑定与审批：194 passed（实现者聚焦集合）。
- MCP/Paper 聚焦集合：236 passed；旧范围断言调整后继续总体验收。
- 扩大交易相关第一次回归：848 passed、20 failed、1 skipped。失败已按目录/查询的新契约、旧 owner 投影与离线 mock 分组处理；此记录保留首次结果，最终结果另记。
- `npm run build --prefix frontend`：通过；Vite 原有大 chunk 提示不影响构建。
- `uv run --with playwright python tests/browser_visible_management.py`：1440/390 像素中英文全部通过，保存默认、释放冲突、交接/受限恢复共 12 张截图。
- `uv run --with playwright python tests/browser_exclusive_management.py`：更新旧配置验收为新默认后，1440/390 像素中英文全部通过，兼顾保存失败保留草稿和部分读取警示。
- 回滚/重复初始化：`tests/test_trading_runtime_compatibility.py` 2 passed，保留原 unknown 与子 ID、不续跑尾部，拟降到 v2 时阻止接管活跃 v3 数据。
- 最终扩大交易相关回归：**1123 passed、1 skipped，291.45 秒**。范围由 `rg --files tests` 选择交易、MCP、Paper、管理、现货、状态、目录、绑定聊天和恢复相关测试；跳过项明确注明相同 SQLite reservation 语义已由 Binance scope 场景覆盖。
- 其后补齐配置删除边界：新 v3 意图不得在缺配置时回落到模拟交易；已保存原父意图严格匹配后只查询；逐项 adapter 不使用已删除的配置快照提交。3 个新增反例通过，包含核心/原身份/管理/SDK 的定向集合 **160 passed，35.82 秒**。该结果是最终补丁后的验收，不将两轮重叠测试相加。
- 最终实际目录与回执测量：2 passed，见上表。
- `openspec validate simplify-trading-tools-and-auto-management --strict`：通过；下述前置 requirement 尚未同步的 INFO 保留。
- `git diff --check`：通过；仅工作区既有 LF/CRLF 提示。

[逐场景证据索引](scenario-verification.md) 已核对七份 delta 的 **231 个场景**：191 个由直接测试断言覆盖，40 个由局部测试与生产分支组合审查支持。归档前同步旧独占管理规范时，补回一项既有外部单撤单场景，避免 MODIFIED 丢失旧契约；未新增交易行为。索引明确列出组合场景未由单个端到端测试证明的条件，不将总测试数作为逐场景证明。

## 规范同步依赖

本次只 apply。以后 sync/archive 前须先同步 `add-exclusive-position-management`；本 delta 的 `Verified external changes do not disable independent exits` 是对该未同步前置 requirement 的修改。保留当前工作区其他改动与 `.agents/`。

实现及离线验收已完成。本次 apply 保留工作区现状；后续同步、归档和发布按上述依赖顺序另行执行。

## 2026-10-14 归档发版前收尾

用户随后授权归档全部未归档变更并执行 Git 与 Docker 发布。按顺序同步并归档模型发现（15/15）与独占管理（51/51），再同步本变更（62/62）；主规范严格校验 **13/13 通过**，原前置 requirement INFO 已消除。主规范保留旧显式策略并明确其兼容范围，没有删除旧场景；本变更的 73 条要求和 231 个场景均逐项比对同步结果。

发布前发现并补齐接受交接后的删除边界：原任务删除不能清掉接收方核验所需的原父子意图和执行证据。`tests/test_management_recovery_deletion.py` 通过实际 transfer、delete API、持久化清理及重新初始化链路验证父/子两种恢复引用、原 actor/hash/ID 和相关订单/成交/审计保留；无关私有历史继续清理，查询权限不扩大。删除、配置回滚、管理恢复和现货预算的定向集合 **105 passed，37.95 秒**。

模型发现的 URL 凭据回显边界也已补齐，其六文件离线验收 **115 passed、7 subtests passed**，证据随模型发现归档保留。当前工作区发布输入均属于以上三个变更及统一 Paper 入口所需基础实现；`.agents/` 不暂存，旧镜像交付记录仅作历史证据。

三个旧 DailySummary 夹具调整为使用实际已发布策略视图，16 项及关联回归 **44 passed**，没有恢复默认读取旧原文。随后完整执行 `uv run python -m pytest tests -q --tb=short -rs`：**2209 passed、1 failed、1 skipped、26 subtests passed，497.07 秒**。唯一失败为旧摘要→记忆链路仍要求诊断正文进入记忆，并暴露具体英文 order/submission 核验描述的过滤缺口。

收尾窄修补齐英文 unverified/unconfirmed、verify/reconcile order/submission 的诊断过滤，新增反例验证 CPI、四小时收盘、价格条件仍保留；流水线仍核对调用顺序、unknown 原始决策/回执、后台状态和原子发布。三个定向文件 **58 passed**；最后运行所有 memory/context/summary/prompt/decision/format/performance 等受影响的 **40 文件：462 passed、3 subtests passed，83.82 秒**。完整回归中的唯一失败在该集合中已通过；不将重叠测试累加，也不声称最后补丁后又重跑了整个全套。

主规范严格校验 **13/13 通过**；三份归档保留 **128/128** 已完成任务。归档证据链接已按新目录修正，并核对 495 个测试函数链接的当前源码行号。暂存仅包含三个已完成变更及其必要依赖，凭据模式扫描及暂存差异检查通过；本地 `.agents/` 保留未暂存。
