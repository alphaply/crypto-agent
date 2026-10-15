# 生产部署与升级指南

## MCP 故障定位与恢复

持续资源证据、只读诊断、故障分类及宿主独立采集见 [资源诊断](RESOURCE_DIAGNOSTICS.md)。采集脚本不会重启容器；没有故障瞬间记录时不能仅凭断连确认 OOM。

同一诊断时段先比较公网和服务器本机健康，再查 OAuth/MCP/交易所。参考 [MCP 分层排障](MCP.md#mcp-分层排障) 与 [2026-10-14 实测记录](MCP_OUTAGE_2026-10-14.md)。以下只读命令按实际映射端口/容器名替换；不要输出完整环境配置或认证头。

```bash
date -Is
docker ps --format '{{.Names}} {{.Status}} {{.Ports}}'
docker stats --no-stream crypto-agent crypto-agent-scheduler
curl --connect-timeout 5 --max-time 10 -sS http://127.0.0.1:31421/health
docker exec crypto-agent python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:7860/health', timeout=5).status)"
curl --connect-timeout 5 --max-time 10 -sS -o /dev/null -w 'http=%{http_code} tls=%{time_appconnect} total=%{time_total}\n' https://YOUR_DOMAIN/health
curl --connect-timeout 5 --max-time 10 -sS https://YOUR_DOMAIN/.well-known/oauth-authorization-server/oauth
curl --connect-timeout 5 --max-time 10 -sS https://YOUR_DOMAIN/.well-known/oauth-protected-resource/mcp
curl --connect-timeout 5 --max-time 10 -sS https://YOUR_DOMAIN/oauth/.well-known/oauth-authorization-server
curl --connect-timeout 5 --max-time 10 -sS https://YOUR_DOMAIN/oauth/resource-metadata
```

只导出故障时段必要的容器和代理日志，先脱敏 OAuth 查询串、签名 URL、Cookie 与密码。TLS 失败先检查入口监听、防火墙、证书和 SNI；标准发现 404 而兼容路径正常时检查反代精确路径和运行镜像版本；本机 health 失败查容器/应用/数据库。不要直接提高代理超时。已有 connector 上依次只读验证 `list_profiles`、余额、订单、行情；各项分别记录，不用新交易验证。

修复前保存实际原配置与镜像 digest，代理语法检查 `nginx -t` 通过后才应用变更；重建保留原数据卷、`CONFIG_MASTER_KEY`、JWT 和 OAuth 密钥引用及未核验交易记录。回退使用原配置或原 digest，禁止拿旧数据库覆盖已发生的新成交。发布镜像与生产容器更新分别记录。恢复需同时有公网 TLS/Web/health/OAuth/已授权 MCP 验收，缺服务器访问时不能声称已定位根因或修复完成。

MCP 并发、预算、缓存和数据库短等待参数见 `.env.template` 与 [MCP 可用性说明](MCP.md#可用性预算与数据完整性)。线程池额度修改需重启，启动执行幂等迁移；任何客户端超时都不释放仍在运行的金融协调，也不重发未知操作。

独占金融范围的 OS 锁保存在交易数据库旁的 `.management-locks`；Docker Web 与 scheduler 共享 `/app/data` 后自然共享这些锁，不能为它们分别挂载锁目录或使用 `/tmp`。容器退出后 OS 自动释放锁，数据库中 running/unknown 仍保留，后台只按原成交/订单证据恢复。不要在服务运行时删除锁文件，删除再新建同名文件会破坏存活 worker 的互斥。正常升级保留数据卷即可，不需要手工“解锁”。

当前发布使用 Docker Hub 镜像 `alphaply712/crypto-agent:latest`。宝塔及已有 Compose 中的镜像名应保持一致；Web 和独立调度容器必须更新到同一镜像。

## 宝塔镜像更新

先拉取 `alphaply712/crypto-agent:latest`，再用新镜像重新创建原容器。保留原端口、数据卷、环境变量和 `CONFIG_MASTER_KEY`。推送或拉取镜像本身不会替换正在运行的容器。

```bash
# 先按目标主机通过的 capacity profile 设置六个变量，并校验双容器合计及主机余量。
# 本地 profile 的结果仅适用于隔离环境；变量未设置时不得启动。
: "${WEB_MEMORY_BYTES:?capacity profile required}" "${WEB_MEMORY_SWAP_TOTAL_BYTES:?capacity profile required}" "${WEB_CPUS:?capacity profile required}"
: "${SCHEDULER_MEMORY_BYTES:?capacity profile required}" "${SCHEDULER_MEMORY_SWAP_TOTAL_BYTES:?capacity profile required}" "${SCHEDULER_CPUS:?capacity profile required}"
docker pull alphaply712/crypto-agent:latest
docker image inspect alphaply712/crypto-agent:latest --format '{{json .RepoDigests}}'
```

发布时记录推送所得的 digest；需要精确回滚时使用之前记录的 `alphaply712/crypto-agent@sha256:...`，避免可变的 `latest` 标签指向新版本。

## 首次部署

```bash
mkdir -p crypto-agent
cd crypto-agent
curl -O https://raw.githubusercontent.com/alphaply/crypto-agent/beta/.env.template
cp .env.template .env
```

编辑 `.env`，至少设置：

```env
ADMIN_PASSWORD=your-strong-password
JWT_SECRET=your-long-random-jwt-secret
CONFIG_MASTER_KEY=your-long-stable-config-master-key
PORT=7860
RUN_SCHEDULER_IN_WEB=true
SCHEDULER_MAX_WORKERS=2
TIMEZONE=Asia/Shanghai
```

其中 `RUN_SCHEDULER_IN_WEB=true` 适用于单进程运行方式；下面分容器部署显式覆盖为 false，并单独启动 `crypto-agent-scheduler`。仓库不提供 Compose 文件，已有自维护 Compose 可沿用相同镜像、数据卷和配置。
短期动态记忆在每轮策略摘要成功保存后入队更新，合并旧 memory、近 4 小时摘要和实际执行证据。失败保留旧记忆，最多尝试 3 次，前两次失败后分别等待至少 60 秒、300 秒。日报和固定四小时批次已退役，不再注册调度或补跑。

启动服务：

```bash
# 先按目标主机通过的 capacity profile 设置六个变量，并校验双容器合计及主机余量。
# 本地 profile 的结果仅适用于隔离环境；变量未设置时不得启动。
: "${WEB_MEMORY_BYTES:?capacity profile required}" "${WEB_MEMORY_SWAP_TOTAL_BYTES:?capacity profile required}" "${WEB_CPUS:?capacity profile required}"
: "${SCHEDULER_MEMORY_BYTES:?capacity profile required}" "${SCHEDULER_MEMORY_SWAP_TOTAL_BYTES:?capacity profile required}" "${SCHEDULER_CPUS:?capacity profile required}"
docker pull alphaply712/crypto-agent:latest
docker run -d --name crypto-agent --restart unless-stopped \
  --memory "$WEB_MEMORY_BYTES" --memory-swap "$WEB_MEMORY_SWAP_TOTAL_BYTES" --cpus "$WEB_CPUS" \
  --env-file .env -e RUN_SCHEDULER_IN_WEB=false \
  -p 31421:7860 -v crypto_agent_data:/app/data \
  alphaply712/crypto-agent:latest
docker run -d --name crypto-agent-scheduler --restart unless-stopped --no-healthcheck \
  --memory "$SCHEDULER_MEMORY_BYTES" --memory-swap "$SCHEDULER_MEMORY_SWAP_TOTAL_BYTES" --cpus "$SCHEDULER_CPUS" \
  --env-file .env -e RUN_SCHEDULER_IN_WEB=false \
  -v crypto_agent_data:/app/data \
  alphaply712/crypto-agent:latest uv run --no-sync python -m backend.app.core.scheduler
```

以上命令启动两个容器，且只有一个调度器：

- `crypto-agent`：仅提供 FastAPI + 前端页面。
- `crypto-agent-scheduler`：仅执行定时 agent 任务。

这样可以避免 scheduler 在同一进程里执行大模型/市场分析时把 Web 请求拖慢。调度器不提供 HTTP 服务，所以关闭其继承的 Web 健康检查。对 2 核 2G 机器，建议把 `SCHEDULER_MAX_WORKERS` 保持在 `1` 或 `2`。

验证健康状态：

```bash
curl http://localhost:31421/health
```

访问地址：

- 公开看板：`http://your-server:31421/`
- 控制台：`http://your-server:31421/console/chat`
- 配置页：`http://your-server:31421/console/config`

## 数据与密钥

两个容器都将 named volume `crypto_agent_data` 挂载到 `/app/data`。SQLite 运行数据、聊天检查点和日志都保存在该数据卷中。

`CONFIG_MASTER_KEY` 用于解密 SQLite 中保存的敏感配置。恢复或升级同一个数据卷时必须继续使用原来的 `CONFIG_MASTER_KEY`，否则已保存的交易所密钥和模型密钥无法解密，`/health` 会显示 degraded。

## 升级

记录当前容器镜像 ID / 已保存 digest，并先拉取新镜像。停止写入后备份数据，再删除旧容器并按「首次部署」命令重建两个容器；删除容器不删除命名数据卷：

```bash
docker inspect crypto-agent --format '{{.Image}}'
# 先按目标主机通过的 capacity profile 设置六个变量，并校验双容器合计及主机余量。
# 本地 profile 的结果仅适用于隔离环境；变量未设置时不得启动。
: "${WEB_MEMORY_BYTES:?capacity profile required}" "${WEB_MEMORY_SWAP_TOTAL_BYTES:?capacity profile required}" "${WEB_CPUS:?capacity profile required}"
: "${SCHEDULER_MEMORY_BYTES:?capacity profile required}" "${SCHEDULER_MEMORY_SWAP_TOTAL_BYTES:?capacity profile required}" "${SCHEDULER_CPUS:?capacity profile required}"
docker pull alphaply712/crypto-agent:latest
docker stop crypto-agent crypto-agent-scheduler
docker cp crypto-agent:/app/data ./crypto_agent_data_backup
docker rm crypto-agent crypto-agent-scheduler
```

重建完成后检查两边日志和 Web 健康状态：

```bash
docker logs --tail 100 crypto-agent
docker logs --tail 100 crypto-agent-scheduler
curl http://localhost:31421/health
```

使用新的备份目录名保留每次快照；备份目录包含密钥和交易数据，不要提交或打包到镜像。升级不要删除 `crypto_agent_data`。已有自维护 Compose 也不要执行 `docker compose down -v`。

本次版本启动时自动创建交易规则/版本及操作记录等附加表，保留历史订单与记忆。旧 REAL 默认可选 TP/SL、旧 STRATEGY 默认必填；新任务默认必填，已有任务不会自动切换成独立退出。需要独立退出时，在账户持仓、挂单和待核验操作全部处理完后，到任务设置中选择。服务器 Web 与调度器都必须使用同一版本，避免新旧退出维护逻辑并行。

## 回滚

回滚时将「首次部署」中的镜像替换为已记录的旧 digest，并保持同一 `.env` 和数据卷。新版本独立退出周期未结束前，不应直接让旧版本接管；先处理活动仓位和委托，或继续运行能识别这些计划的维护进程。只有确需恢复数据且已处理交易所当前状态后，才考虑使用备份，不能用旧数据库掩盖已发生的成交。

```bash
docker pull alphaply712/crypto-agent@sha256:替换为已记录的旧版本摘要
```

单进程部署也可显式开启 Web 内调度器；不要同时运行独立调度器：

```bash
docker run -d --name crypto-agent \
  --memory "$WEB_MEMORY_BYTES" --memory-swap "$WEB_MEMORY_SWAP_TOTAL_BYTES" --cpus "$WEB_CPUS" \
  --restart unless-stopped \
  -p 31421:7860 \
  -v crypto_agent_data:/app/data \
  -e ADMIN_PASSWORD=your-strong-password \
  -e JWT_SECRET=your-long-random-jwt-secret \
  -e CONFIG_MASTER_KEY=your-long-stable-config-master-key \
  -e RUN_SCHEDULER_IN_WEB=true \
  alphaply712/crypto-agent:latest
```

## 生产安全检查

- 修改默认 `ADMIN_PASSWORD`。
- 使用强随机 `JWT_SECRET` 和 `CONFIG_MASTER_KEY`。
- 记录并安全保存当前 `CONFIG_MASTER_KEY`。
- 只向可信来源开放对外映射端口（示例为 `31421`）。
- 推荐通过 Nginx 或 Caddy 配置 HTTPS 反向代理。
- 定期备份 `/app/data` 或 Docker named volume。
- 关注 `/health` 状态及两个容器的 `docker logs -f` 输出。

## 本地构建验证

先完成后端与前端回归，再重新生成前端产物。推荐使用预编译前端的构建入口，不能复用旧 dist：

```bash
npm run build --prefix frontend
docker build -f Dockerfile.prebuilt -t alphaply712/crypto-agent:latest .
docker run --rm --memory 1g --memory-swap 1152m --cpus 1 -p 31421:7860 \
  -e ADMIN_PASSWORD=local-password \
  -e JWT_SECRET=local-jwt-secret \
  -e CONFIG_MASTER_KEY=local-config-master-key \
  -e RUN_SCHEDULER_IN_WEB=false \
  alphaply712/crypto-agent:latest
```

检查：

```bash
curl http://localhost:31421/health
```

本地烟测使用镜像内空数据目录，不挂载生产数据库，关闭调度器。也可用 `docker build -t alphaply712/crypto-agent:latest .` 执行多阶段完整构建；`NODE_IMAGE` / `PYTHON_IMAGE` 可调整基础镜像来源，`NPM_REGISTRY` 可调整前端依赖源。

验证后提交并推送 `beta`，再发布镜像并记录 Git commit 与镜像 digest：

```bash
docker push alphaply712/crypto-agent:latest
docker image inspect alphaply712/crypto-agent:latest --format '{{json .RepoDigests}}'
```

推送镜像不会自动重建服务器容器。`.env`、`pricing.json`、SQLite 数据库及 WAL 文件由 `.dockerignore` 排除，不能作为镜像发布内容。

## OpenSpec 归档后的自动发版

### v3 交易数据与兼容回滚

升级 v3 前停止旧 worker，保留数据库、WAL 与原交易证据；重复初始化只增量建表，不重算旧 fingerprint 或补发批次尾项。新请求停止后，当前运行时继续核验已尝试项。完整保留 `visible_management_*`、`management_*`（包括 retirement 与 recovery grants）、`unified_trade_batches`、`unified_trade_children`、`unified_native_intents`、`paper_batches`、`paper_batch_children`、`spot_management_intents`、`spot_budget_reservations` 及原交易/MCP/订单账本。

拟回滚时，对一致性备份运行 `uv run python scripts/check-trading-compatibility.py --database <backup.sqlite> --supported-contract-version 2`。只读预检发现持久 owner、活跃 v3 worker、未决原操作、原生客户端身份或接受的恢复责任时返回 `blocked` 和退出码 2；不能让旧运行时管理这些数据。当前支持 v3 的运行时传版本 3。预检不清理资源、不释放 owner、不下单；通过只表示已检查的新契约阻塞项不存在，还须遵守上文既有 v2/all 与条件单的回滚要求。不得覆盖代码或旧备份来消除未决证据。

本项目将 archive 请求视为完整交付：先同步主规范、核对任务与验收结果，再归档并发版。用户明确要求“仅归档”时按该次范围执行。持久规则记录在 `AGENTS.md`、`openspec/config.yaml` 和 `openspec/specs/archive-release-workflow/spec.md`；归档技能读取这些规则后执行下列流程。

1. 将选定变更的增量规范合并到主规范，保留未修改要求与场景，运行 `openspec validate --specs --strict` 和该变更的严格校验。
2. 将已完成变更移动到 `openspec/changes/archive/YYYY-MM-DD-<change-name>`，保留验收记录和元数据；已有目标不得覆盖。
3. 仅暂存本次实现、测试、文档、主规范和归档，在 `beta` 提交。无关工作（包括未跟踪的本地 `.agents/`）保留，不能直接用 `git add .` 混入。
4. 在仓库根目录运行发布入口，归档名替换为实际目录名：

   ```powershell
   powershell -NoProfile -File scripts/release-archived-change.ps1 -ArchiveName 2026-10-13-refine-decision-state-context
   ```

该入口检查已提交的归档和干净的镜像输入，重新安装锁定的前端依赖、构建前端，再通过 `Dockerfile.prebuilt` 构建带 Git revision 的镜像。烟测使用一次性空数据容器、关闭调度器、不挂载生产数据，检查 `/health` 与前端入口后自动清理。成功后推送 `origin/beta`，依次发布 `alphaply712/crypto-agent:git-<short-sha>` 和 `:latest`，输出 commit 与镜像 digest。

任一步失败都停止后续步骤，并报告已推送的阶段。修复后可对同一归档重新运行入口；不能将“目录已归档”当成“发布成功”。该流程由归档 Agent 按项目规则调用，直接运行裸 `openspec archive` 命令不会触发 Git 或 Docker 钩子。生产容器更新继续使用上面的部署流程，在用户请求部署时执行。

双容器 profile、预先声明的校准/acceptance、memory+swap 总量与安全回退见 [容量验证](CAPACITY_VERIFICATION.md)。目标主机需分别设置预期memory/swap/CPU环境并核验实际cgroup；本地 Docker Desktop 报告不代表生产验收。

## 用户指定的无容量门槛主机测试

当用户明确要求测试整台主机的实际承载能力时，Web 和 scheduler 同时设置 `RUNTIME_CAPACITY_ENABLED=false` 并重建。这会关闭应用资源准入门槛，保留真实资源采集和交易正确性校验；具体范围见 [运行容量](RUNTIME_CAPACITY.md#explicit-observation-mode)。原有数值预算可以保留作为参考，诊断会标记 `observe_only`，不能称它们仍在限额。

Docker Compose 同时移除这两个服务的 `mem_limit`、`memswap_limit`、`cpus` 和 `deploy.resources.limits`；清除旧的 `RESOURCE_EXPECTED_MEMORY_BYTES`、`RESOURCE_EXPECTED_MEMORY_SWAP_TOTAL_BYTES`、`RESOURCE_EXPECTED_CPU_CPUS` 及 `CAPACITY_PROFILE_SHA256` 环境声明。不使用巨大哨兵数值冒充无限额。保留原凭据、数据卷、端口、账户锁、请求超时和唯一 scheduler。通过 `docker inspect` 和进程内 cgroup 读取确认配额确实未设置。

这种测试不再以固定1GiB容器或RSS平台阈值作为部署门槛。旧的失败/中断验收报告原样保留；发布仍须完成回归、前端构建、隔离空数据烟测、Git提交推送及带revision的不可变镜像发布。生产验收只调用账户、订单及行情等只读工具，记录并发量、耗时、进程RSS峰值、主机可用内存、swap和OOM证据，结论限定于实际验证的负载。
