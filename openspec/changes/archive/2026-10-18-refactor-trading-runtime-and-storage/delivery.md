# 重构交付记录

日期：2026-10-18。用户已确认实施范围，并明确只做小测试，直接构建、推送 Docker；完整验收由用户负责。

## 已完成实现

- PostgreSQL 持久队列：普通计划、新闻和定投独立保存；同策略串行，跨策略按最早可运行顺序使用名额。没有等待过期、忙碌跳过或跨触发合并。
- 配置与恢复：未开始任务使用最新配置；暂停、删除、交易范围变化取消旧排队项。重启保留未开始项，已开始中断不整轮重新调用模型。
- 人工与 Agent 共管：旧管理策略统一升级；旧工具入口复用服务并减少重复校验；每个实际动作仍核验最新事实。
- 独立退出：人工变化更新现状及历史，不自动撤残单、补仓或补平；仅明确 Agent 全平或已保存清理意图授权清理。
- 强制止盈止损：按系统最新保存目标维护，外部改价/撤单不会替代目标。Agent 与已登录前端保留调整能力。
- 数据库：统计 Store 批量聚合；现货订单/累计成交同事务；启动检查只读；删除/维护保留未完成原操作和关联证据，避免删除并发写回的新事实。
- 看板：分开展示当前运行和等待数量/下一等待项，抽取相关组件；现货同步和统计计算从看板组装服务拆出；新闻设置说明同步更新。

保留原统计来源优先级、查询范围、金额、时间、未知盈亏及原始操作身份。新增迁移为 `0010_durable_agent_queue.sql`、`0011_shared_policy_normalization.sql`，没有清空数据库或重写既有迁移。

## 实际检查

以下均为本次相关的小范围检查，未运行全仓库测试或真实交易验收。

| 范围 | 结果 |
| --- | --- |
| 持久队列及相关 scheduler/news 行为 | 29 个不同针对性用例通过 |
| 共同管理、独立退出、强制保护、每动作校验 | `tests/test_runtime_trading_refactor.py`：20 passed |
| 数据库统计、事务、启动、清理及原有恢复边界 | 29 个不同针对性用例通过 |
| 现货统计兼容 | `tests/test_spot_portfolio_stats.py`：10 passed |
| 看板同时运行/排队 | `tests/test_dashboard_runtime.py`：1 passed |
| 时间显示兼容 | `test_dashboard_scheduler_timestamp_includes_china_timezone`：1 passed |
| 前端运行状态及原看板逻辑 | `node --test frontend/tests/agentRuntime.test.js frontend/tests/dashboard.test.js`：17 passed |
| 前端生产构建 | `npm run build --prefix frontend` 通过；存在原有 500 kB 分块体积提示 |
| OpenSpec 变更结构 | `openspec validate refactor-trading-runtime-and-storage --strict` 通过 |
| Python 语法 | 34 个新增/改动 Python 文件通过 AST 语法检查 |
| 差异及构建输入 | `git diff --check` 无错误；281 个构建输入文件的内容指纹见 `build-inputs.json`；未包含运行密钥或数据库 |

数据库小测试使用独立 PostgreSQL 测试容器及每例独立 schema，不连接生产数据库或真实交易所。测试中暴露的 fixture/旧业务断言问题已修正后重跑对应小测试；未把第一次失败当作通过。

## 构建与推送

状态：构建、两个标签推送及远端 digest 核对均成功。

- 仓库：`alphaply712/crypto-agent`
- 版本标签：`refactor-20261018-174e887349e6`
- 滚动标签：`latest`
- 两个远端标签共同 digest：`sha256:45960392d082aa892d375c37e2a6be0f6c1446999136960d7050be196f6fd8e2`
- 平台：`linux/amd64`（另含构建证明 manifest）
- 内容指纹：`174e887349e6b3288c67c9d774a2155c84911568d784f3080f470c14743e3f14`
- 镜像 `IMAGE_REVISION` 及 OCI revision 标签：`source-174e887349e6b3288c67c9d774a2155c84911568d784f3080f470c14743e3f14`
- 构建命令：`docker build -f Dockerfile.prebuilt --build-arg IMAGE_REVISION=source-174e887349e6b3288c67c9d774a2155c84911568d784f3080f470c14743e3f14 --label org.opencontainers.image.revision=source-174e887349e6b3288c67c9d774a2155c84911568d784f3080f470c14743e3f14 -t alphaply712/crypto-agent:refactor-20261018-174e887349e6 -t alphaply712/crypto-agent:latest .`

先推送版本标签，成功后推送 `latest`。分别通过 `docker buildx imagetools inspect` 从远端确认相同 digest；构建后复查 281 个输入指纹未改变。测试容器 `crypto-refactor-test-20261018` 已删除，未操作其他容器。

采用 `Dockerfile.prebuilt` 与本次前端产物；构建输入包含本次尚未提交的代码，因此用内容指纹标签识别，不冒用当前 Git commit 作为已提交版本。Git 基线为 `6e09c7c494307bc0ba836f7d362a30f2911862b6`。

## 初次镜像交付边界

- 没有运行完整回归、长时间并发压测或真实交易所验收。
- 没有生产重启、数据清空、Git 推送或 OpenSpec 归档。
- 未完成交易的原始证据保留；原账户授权已删除或不可用时，保持未核验结果，不借用新账户核验或换身份重发。
- 未增加停机时槽回填或多机器调度；状态切换期间看板各次查询可能出现一次刷新内的瞬时差异。
- 生产升级继续使用已有备份及迁移流程；镜像推送成功不等于生产已更新。

运行规则详见 `docs/REFACTOR_RUNTIME_20261018.md` 和 `docs/DURABLE_AGENT_QUEUE.md`。

## 后续归档请求

用户于 2026-10-18 随后要求归档。本目录归档到 `2026-10-18-refactor-trading-runtime-and-storage`，与四项能力的主规范一起提交并发布。归档前主规范严格校验 21/21 通过，本变更严格校验通过，任务 10/10 完成。同步修正了旧文档关于独占管理、外部全平清理及忙碌任务合并的冲突措辞，没有增加新交易行为。

沿用用户不跑完整测试的要求，发布命令为 `powershell -NoProfile -File scripts/release-archived-change.ps1 -ArchiveName 2026-10-18-refactor-trading-runtime-and-storage -SkipCapacityAcceptance`。该开关只跳过长时间容量验收，仍执行隔离迁移及启动烟测。此归档里的前述源码指纹镜像是首次交付；后续 `git-<short-sha>`、`latest` 和 Git 发布的最终成功状态、commit 与 digest 以本次归档回复及发布脚本实际输出为准。
