# PostgreSQL 服务器部署记录

2026-10-15，目标服务器为用户指定的 Ubuntu 22.04、2 vCPU、约 7.25 GiB 实际内存主机。用户明确要求跳过隔离测试直接部署，并使用应用 `latest` 标签方便后续更新。

## 当前部署

- 访问地址：`https://crypto-agent.elpha.top/console/config`。公网 HTTPS 健康接口确认 `success=true`、`database.backend=postgresql`，配置页面 HTTP 200。本地外网直连 IP 的 31421 端口检查未成功，不把该入口标记为已验证。
- 部署目录：服务器 `/opt/crypto-agent-pg`；Compose 项目名 `crypto-agent-pg`。
- 配置文件：服务器 `/opt/crypto-agent-pg/.env`，权限 600，沿用原后台密码、JWT 与配置加密密钥，不在仓库保存实际凭据。
- 应用：`alphaply712/crypto-agent:latest`；同一镜像另保留 `postgres-20261015-8ba6e035` 标签。
- 已发布 manifest digest：`sha256:8ba6e035d98b0860b0119007847b0bd1d0c28e2b8a4e7000b878d12cdc64bddb`。
- 服务器 Docker 使用的 image config ID：`sha256:bf8c49994438d0c50ac6e3878e35625040b4e041f5a7454b9ce6c7fdc81d71e3`。本机 containerd 镜像存储与服务器经典存储的 image ID 表示不同；已通过实际拉取的 RepoDigest 和服务器 config ID 核对，不能简单比较两个存储后端显示的 ID。
- PostgreSQL 固定 17 及现有 digest，不使用跨主版本浮动的 `postgres:latest`。
- Web、scheduler、DB 分别设置 2 GiB / 1 CPU、2 GiB / 0.5 CPU、1 GiB / 0.5 CPU，容器不使用 swap。沿用旧部署 `RUNTIME_CAPACITY_ENABLED=false`，未重新启用其关闭的应用容量门槛。
- 数据卷：`crypto_agent_pg17_data`、`crypto_agent_pg_app_data`。数据库不映射宿主端口；Web 保留原 31421 端口及现有域名反代。

## 已执行

1. 私密导出 version=1 配置包，保存旧 Compose 和容器元数据。保留原镜像及独立回退标签。
2. 停止旧 Web/scheduler，备份旧应用数据卷并核对 tar 可读。备份目录为服务器 `/opt/crypto-agent-pg/legacy-backup/20261015T094246Z`，数据归档 200,565,967 字节，有 `COMPLETE` 标记；旧卷本身也保留。
3. 拉取已发布 `latest`，启动新 PostgreSQL、执行业务和聊天 schema 迁移，以维护模式启动 Web。
4. 导入原配置，`write_env=false`。核对 3 个任务、11 个模型供应商、3 个交易所配置的数量和 ID、导出密钥内容及 5 份 Prompt 均一致；重启后再次核对。
5. 关闭维护模式，重建 Web，健康检查成功后启动唯一的新 scheduler。新三容器检查时均运行、0 restart、无 OOM；Web/DB healthy。
6. 旧 `crypto-agent`、`crypto-agent-scheduler` 保持停止，并禁用其 Docker 自动重启。旧 Compose 不应再启动，以免两套调度器同时运行。

本次按用户选择只导入配置，不迁移旧交易、记忆、聊天、Paper 余额或 OAuth 授权。OAuth 客户端需重新连接。没有人工调用真实交易接口进行验证。

## 后续更新

在服务器执行：

```bash
sudo /opt/crypto-agent-pg/update.sh
```

该入口使用 `latest`，按现有脚本完成 pull、停止应用、数据库和应用文件备份、迁移、Web 健康检查及 scheduler 启动。更新时不需要再次编辑镜像标签，也不删除数据库卷。

查看状态或日志：

```bash
sudo docker compose --project-directory /opt/crypto-agent-pg ps
sudo docker compose --project-directory /opt/crypto-agent-pg logs --tail 80 web scheduler
```

不要运行 `down -v`。不要直接恢复旧 SQLite 备份覆盖新库；生产产生新业务记录后，回退还须核对期间交易责任。

## 验证边界

此前本机的 Python 原生崩溃未定位或修复，纯 Python 驱动对照也未稳定通过；生产使用原 3.11/binary 候选，没有采用失败的 3.12 或纯驱动候选。服务器初次隔离验收在读取不存在的 `memory.peak` 时提前失败，未进入负载阶段；随后用户明确要求跳过隔离测试直接部署，没有降低阈值或将缺失数据补成通过。

本次只确认部署、导入、进程及公网基本可用，不代表完整容量或长期稳定性验收通过。OpenSpec 仍保持 34/37，未归档、未创建 Git 提交或 Git push。Docker 镜像发布和服务器部署已实际完成。服务器私密证据位于部署目录的 `rollout-state.json`、`import-verification.json` 和 `deployment-report.json`。
