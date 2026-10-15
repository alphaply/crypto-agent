# 2026-10-15 工具调用链路调整发布记录

## 发布结果

- 固定镜像：`alphaply712/crypto-agent:toolchain-20261015-3eefebbd6878`
- `alphaply712/crypto-agent:latest` 已通过 `docker buildx imagetools create --prefer-index=false` 指向同一镜像索引，命令成功退出。
- 发布 digest：`sha256:6d70d2ce19b9b6da04771622831c2d35739f1ed9f779cc6f56badba26ff7a6f7`
- 工作区源文件指纹：`3eefebbd68784b6f90d97ea761232aaadc27d977f9312641c14d1e026b498611`
- 没有创建 Git 提交，没有操作生产容器。

首次推送和部分标签上传重试遇到 Docker Hub TLS 握手超时；固定标签推送最终成功，随后使用仓库 manifest 复制发布 latest，保留相同 digest。仓库 GET 二次核对经重试成功：固定标签与 latest 的响应正文 SHA-256 和 Docker-Content-Digest 均与发布 digest 一致。

## 范围

移除工具链路的内存/缓存准入门槛、CPU 对 scheduler 线程数的进一步压缩、归属索引单轮时间/总条数门槛、后台恢复共享预算、MCP 工作线程累计预算、管理发现与 Paper 维护整轮预算、成交与资金流水总页数预算，以及回执展示硬字节预算。

并发、单次请求/响应超时、重试、现货定投资金预算、权限、真实订单归属和幂等保留。真实网络错误、无效数据、分页不前进仍报告失败，不伪造已同步。前一版的回执结构化展示及观察更新不重置归属索引修复也包含在此镜像中。

## 验证

- 恢复、归属索引跨页、完整大记录、市场目录、真实 CCXT 精度与缓存隔离、并发与工作线程生命周期、Paper 与统一交易回归已验证。
- 新增用例验证响应超时后原工作线程仍能读取下一页、内存压力不拦截但并发仍受控、大回执不因字节预算失败、资金流水超过 50 页可完成且重复页面报错。
- 旧测试中要求“超限就拒绝”的断言已按确认后的行为调整，失败项定点复测通过。原始测试日志保留于本地 `data/limits-*.txt`。
- Windows 中一次组合测试卡在连接池线程启动，停止该测试进程后隔离重跑监控文件，12 项通过；其余金融回归单独执行。
- 前端构建、Python 编译检查、diff 空白检查、Compose 配置检查通过。
- 固定运行时依赖与当前 `pyproject.toml` / `uv.lock` 哈希一致，镜像内源码与构建清单逐文件一致。
- 隔离临时 PostgreSQL 从上一版回执镜像升级，只新增 `0006_unrestricted_evidence_projection.sql`；schema_version=6 检查通过。
- 隔离 Web `/health` 与前端入口通过，调度器关闭。临时容器与网络已清理。未用真实下单做验证。

## 部署

已使用 PostgreSQL 的实例运行原更新脚本以完成拉取、备份、迁移及重建应用。不要仅拉镜像而跳过迁移。

本次仓库 Compose 删除 `cpus`、`mem_limit`、`memswap_limit`。服务器需同步这三类配置调整，保留自身端口、卷名、环境和密钥；应用更新脚本重建 Web/scheduler 后生效。脚本不重建 DB，数据库容器旧配额需另行更新运行参数或安排重建。仅换镜像不会删除已有容器配额。

详见 [部署指南](DEPLOYMENT_GUIDE.md)、[运行说明](RUNTIME_CAPACITY.md) 和 [已确认限制范围](LIMITS_REVIEW_20261015.md)。

构建、推送和隔离验证原始记录：本地 `data/image-release/tool-chain-20261015/`（忽略目录，不提交）。
