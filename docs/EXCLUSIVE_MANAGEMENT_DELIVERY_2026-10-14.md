# 独占管理、MCP 与记忆收益交付记录

变更：`add-exclusive-position-management`，2026-10-14。交易验收采用本地离线交易所；线上修复 OAuth 发现代理路径并做已授权只读复验，另在隔离临时目录验证两个 Linux 容器的共享文件锁。实施验收阶段未发布应用；后续镜像推送见下文。没有重建生产容器或执行真实交易。

## 交付行为

- 管理员可在任务及 MCP 配置页显式选择 Binance/OKX、REAL、swap、独立退出下的有限交易对及方向。默认 strict 归属规则保持；暂停决策保留管理范围和后台维护。
- 启动前仓位作为真实观察基线，人工新增、修改、撤销订单及后续成交纳入管理。原 edit/cancel/close 调用取证后继续，不要求额外 adopt。母单、子单及不同通道的相同数字 ID 保持独立关联。
- 后台处理周期对账、恢复及旧单清理。完整全平/重开建立后继周期，旧 all 不算作新仓保护；残仓仍可管理。未知提交保留原身份，只核验，不重发或仅按租期释放。
- 记忆只发布策略判断、关键价位和有效教训，旧原文和账本保留在详情。收益默认只有权益变化、盈亏、快照回撤；归属不足时不显示假零值。

## 场景与证据

| 验收场景 | 检查与结果 |
| --- | --- |
| 启动已有 ETH 0.02 多仓及 TP2565、SL2458、入场2385 | 两适配器离线测试：无造假入场/成交，无发现交易所写入，原单可管理 |
| 人工加仓、减仓、先加后减 | stable trade ID 去重；基线加净成交与实际仓位核对；后续真实减仓可执行 |
| 新增/改量/改价/改触发价/撤单 | 保留原交易所身份、类型、数量和来源，人工参数不会恢复为旧值 |
| 普通/algo 同数字、OKX 多 book、条件母子切换 | 完整渠道观察、命名空间分离；母单先恢复关联再分类子单，不重复登记 |
| 人工全平、重开、清理时晚到成交 | 旧周期及操作证据保留，后继订单不误撤，残仓/待核验旧单可见 |
| 同毫秒基线/顺序 | 已计入基线的成交按 ID 排除；可验证数字成交序列正确排序，无法证明顺序时保留边界待核验，不虚构周期 |
| 超过单页、1001 条密集成交、失败后重启、晚到成交 | 按完整叶区间保存覆盖及待扫描分支；20-call 上限耗尽后接续，不假报完整 |
| 双 owner、同凭证别名、Hedge/One-way | 配置事务校验；已知 Hedge 才隔离方向，One-way 共享净仓互斥；CAS 防旧观察覆盖 |
| 暂停/删除/缩范围/凭证轮换/导入 | 原子检查活跃计划及 running/unresolved 操作，失败回滚，不提前删除归属证据 |
| ACK 丢失、批次子 ID、MCP 原 ID 恢复 | 原请求/回执持续可查；原 client ID 关联，回放不调用金融 callback |
| read、cancel scope、聊天审批 | 只读发现无 create/edit/cancel，scope/owner/绑定聊天审批保持 |
| MCP 慢依赖、数据库锁及两进程竞争 | 有限线程池、队列及预算；真实 worker 结束才回收额度；DB-backed Web health 在 2 秒内返回；scheduler reservation 不能由 Web 绕过 |
| 用户给出的嘈杂记忆、null/toolcall/unavailable 引用 | 新纯策略正文保留四小时/CPI 条件；历史失败不形成解锁步骤，实际当前未知仍保留依赖 |
| 591.12→589.00 与不完整账本 | 权益 -2.12（-0.36%）、回撤指标可读；归属盈亏不可完整统计，不将零周期写成零交易 |
| 默认卡片、详情、中英文和手机 | 实际 React 页面及 API mock 浏览器验证 1440/390，中英文；原文及账本按需、按权限展开 |

主要测试文件：`test_exclusive_position_management.py`、`test_exclusive_config_lifecycle.py`、`test_management_observation_boundaries.py`、`test_management_recovery.py`、`test_management_scheduler.py`、`test_execution_ledger_progress.py`、`test_mcp_resilience.py`、`test_strategy_memory_performance.py`。旧独立退出、归属、交易回执、记忆及收益测试一并回归。

## 验证记录

受影响的 MCP、接管、独立退出、账本、scheduler、配置、记忆、投影及收益回归批次：**1042 passed、1 skipped、7 subtests passed**。跳过项为已有跨进程 reservation 的冗余平台用例；独立 spawned-process 综合测试实际执行通过。最终修订后的发现/投影场景 **106 passed、1 skipped**，MCP 契约及配套场景批次 **142 passed、1 skipped**，协调/恢复/锁及配置批次 **66 passed**；批次有重叠，不累加为独立测试数。

Windows 的非阻塞文件锁、存活 ACK、进程崩溃及原回执恢复均实测通过。Linux 补充验证使用本次 helper 源码及假 DB 路径，在 Web/scheduler 原容器的共享 `/app/data` 隔离目录中运行独立 Python 测试进程：存活 Web 测试持锁时 scheduler 测试返回 BUSY；只终止该测试进程后 scheduler 可取得锁。退出码 0，隔离目录已验证路径后清理；没有加载交易配置或修改业务数据库。该检查证明共享 inode 的 flock 行为，不冒充新应用已上线。

```powershell
$affectedTests = rg --files tests | Where-Object { $_ -match '\\test_(mcp_|position_adoption|position_protection|independent_|execution_ledger|scheduler_resilience|management_|exclusive_|memory_|context_projections|decision_|performance_context|strategy_memory_performance|trade_context_format|turn_memory|retired_memory|config_store|config_write_atomicity)' }
uv run python -m pytest @affectedTests -q --tb=short
npm run build --prefix frontend
node --test frontend/tests/positionManagement.test.js frontend/tests/mcpSymbols.test.js
openspec validate add-exclusive-position-management --strict
git diff --check
```

前端构建、管理范围/标的 Node 测试、相关 ESLint 及浏览器检查通过。截图见 `docs/screenshots/exclusive-management*`；人工状态及不完整订单截图来自明确标记的离线数据，不冒充生产订单。

## 线上修复与应用上线边界

已确认并修复宝塔 Nginx 的标准 OAuth 发现路径静态目录拦截。精确转发配置通过语法检查并平滑 reload；公开首页、health、标准 OAuth issuer/resource、认证配置/余额/订单/行情均成功。详见 [带时间的故障与回退记录](MCP_OUTAGE_2026-10-14.md)。历史 TLS 超时未复现，资源采样不足以推断历史根因。

生产仍运行原镜像，本次独占管理、记忆收益及并发预算代码尚未 rollout。后续用户请求的 Docker 构建与推送已经完成，标签及 digest 见 [镜像推送记录](DOCKER_PUBLISH_2026-10-14.md)。Web 与独立 scheduler 需共同升级后再开启新策略；完整历史保留在数据库，不用旧数据库回滚掩盖实际成交。归档及生产 rollout 按仓库流程另行执行。

## 明确边界

第一版使用统一保存的交易所账户引用；相同 API Key 别名可识别，不同 Key 指向同一物理账户不能靠 Key 哈希证明互斥。未知方向、不支持原生订单语义、成交缺口及同毫秒溢出继续明确标记缺项，不能猜测或补造交易。MCP 总预算有界；同步调用已开始后仍占用实际额度直到 worker 结束。scheduler 复用有限维护 worker、单次网络超时及范围退避，不能原子锁住交易所的人类操作。

实施保留了会话开始前的 provider-model-discovery、Jev 及其他本地修改，未重置、提交或归档它们。所有新增审计证据均增量保留。
