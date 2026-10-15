# 隔离容量验证

本 change 已通过本机 Docker Desktop 的 v12 全时长隔离容量验收。生产主机尚未验收；历史 v10 的一次 SIGSEGV 仍未定因，不能据本轮通过宣称它已修复。镜像发布不重建生产容器，也不能证明线上故障已经消除。

最低 Python 版本为 3.11：使用 SQLite `blobopen` 检查账本长度并分块读取，避免 `length(CAST(payload AS BLOB))` 先加载整个值。Docker 使用 Python 3.11。

## 预先声明与执行

`scripts/capacity/*.json` 固定两个进程各自的 memory、memory+swap、CPU、目录样本规模、并发、轮换 key 数量、七个阶段时长、应用总额度、Web SLO 和宿主余量。校准报告只能得到 `calibrated`；最终文件冻结后另跑 `acceptance`。报告保存完整 profile 指纹、镜像 ID、revision、实际 inspect/cgroup 限额、两进程峰值及失败原因。失败报告保留，不覆盖为通过。

```powershell
npm run build --prefix frontend
docker build -f Dockerfile.prebuilt --build-arg IMAGE_REVISION=working-calibration --label org.opencontainers.image.revision=working-calibration -t crypto-agent-capacity:calibration .
uv run python scripts/verify-runtime-capacity.py --image crypto-agent-capacity:calibration --profile scripts/capacity/local-calibration-v11.json --mode calibration --output data/capacity-reports
uv run python scripts/verify-runtime-capacity.py --image crypto-agent-capacity:calibration --profile scripts/capacity/local-verified-v12.json --mode acceptance --output data/capacity-reports
```

验证器先用 64 MiB、无网络的探针读取 Docker 宿主可用内存和 CPU，校验 Web + scheduler + 其他服务预留 + 主机余量。之后只创建带随机名称的两个受限容器，使用独立临时目录、空配置、假 Binance 目录和模拟订单。所有外部 socket 连接被拒绝；不读取仓库 `.env`、生产数据库或交易密钥。只终止本轮创建的受限 worker，不制造宿主 OOM。scheduler 容器执行真实调度循环，配置为空。

每个 catalogue 阶段必须实际成功加载样本，不能把 busy 拒绝算作完成；缓存保留字节必须非零。采样缓冲最多 3600 项。退休阶段必须覆盖声明的全部 key，最终目录与消费者引用归零；同时核验绝对峰值、整轮 RSS 增长和后半轮 RSS 增长。后半轮容许 16 MiB 分配器波动，整轮上限由独立校准决定。失败后应重新校准并生成新版本，不修改已经执行的 profile。

重启测试保存原父子身份：原已完成子项、原 unknown 子项和未尝试尾项继续存在，重新读取和同 ID 重试不得触发金融回调。每个容器核验 OOM 增量，宿主 OOM 计数独立核验。报告在 `data/capacity-reports/<run-id>/`；该目录不提交真实运行数据。

## 2026-10-15 本机校准与失败记录

[运行索引](capacity/2026-10-15-run-index.json) 收录目前 18 份正式目录及短阶段 debug 报告，保存每份原始 SHA-256、verdict、失败项、profile 指纹和镜像身份。早期 harness 曾出现目录实际未加载、把 busy 算作完成、退休 key 被容量拒绝后跳过等问题；其原有 `calibrated` 或 `failed` 裁决均保留，不能用作现版本验收。debug 报告仅验证 harness 修正。

v7 校准 `20261015T011737Z-08f3757b` 的原裁决为 `calibrated`。两个容器分别限制 memory=1 GiB、memory+swap=1152 MiB、CPU=1；进程内总字节额度为 128 MiB，目录保留额度 16 MiB，单次刷新预留 64 MiB。样本含各 4096 个 Binance 现货/线性永续标的，刷新阶段 24 个并发请求，退休阶段要求 32 个唯一 key 全部成功加载。宿主其他服务预留 8 GiB，余量要求 2 GiB。这些是本机 Docker Desktop 的校准预算，不能直接作为生产主机配置。

| v7 实测 | Web | Scheduler |
| --- | ---: | ---: |
| 全阶段最大 cgroup memory peak | 374763520 字节（357.4 MiB） | 373547008 字节（356.2 MiB） |
| 全阶段最大进程 RSS | 392478720 字节（374.3 MiB） | 390586368 字节（372.5 MiB） |
| 成功覆盖的唯一退休 key | 32 | 32 |
| 退休后半轮 RSS 增长 | 0 字节 | 0 字节 |
| 容器 OOM kill 增量 | 0 | 0 |

该轮 Web 679 次延迟样本、0 次错误，p95=112.3 ms、最大 194.3 ms；宿主 OOM kill 增量为 0。它使用的 worker 后来发现观察计数竞态，因此这些峰值保留为历史测量，不能替代修正后 worker 的完整验收。

- **v8 完整验收 `20261015T012153Z-670cd1b1`：failed。** 唯一失败为 `scheduler:slow_dependencies:no_critical_receipt_progress`。旧观察器只在自己调用的 `reconcile` 返回 `processed>0` 时读取并计数；真实 scheduler 若先完成同一原始身份，下一次 helper 返回 0 就会漏计。该路径已用真实 SQLite 回执、真实恢复 helper 和 AST 隔离加载测试复现并修正：当前按已 seed 的 rowid/config/operation ID 读取持久状态，仅对同身份 completed 去重计数，集合上限等于声明阶段数。v8 的失败不改写，也不据此推定它已满足验收。
- **v9 短校准 `20261015T012903Z-2550137c`：calibrated。** 采用修正观察器，退休阶段 45 秒，其余阶段各 2 秒；两个容器均覆盖 32 key，刷新和慢依赖阶段各观察到 1 次原身份回执更新，后半轮 RSS 增长分别为 872448/462848 字节。Web 208 次样本、0 次错误，p95=99.5 ms、最大 159.4 ms。短校准不能替代完整 acceptance。
- **v10 完整验收 `20261015T013044Z-82dbc1c6`：failed，原始根因未确定。** Web 在 cold 阶段退出；保留的 `web-docker-state.json` 显示 `ExitCode=139`、`OOMKilled=false`，`web-container.log` 明确记录 `Fatal Python error: Segmentation fault`。当前线程在自动 GC 期间执行 `copy._deepcopy_list` / `public_catalogue.py`，另有 weakref/deepcopy 线程及处于 `database.py:44`、`sqlite3.connect` 的恢复线程。该时点不在退休阶段的显式 `gc.collect()` 中。栈位置尚不能确定哪个组件损坏内存，不能把此轮归为普通 HTTP 超时或 OOM，也不能因一次短校准通过而继续发布。

失败时验证器会在移除临时容器前保存 Docker.State；存活进程可请求 SIGUSR1 全线程栈，并保留有界日志、worker 指纹和资源时间序列。v10 的原始崩溃日志与 State 文件指纹也记录在运行索引中。

### 有限原生诊断与诊断器修复

[原生诊断摘要](capacity/2026-10-15-native-diagnostics.json) 保存各原报告、日志及状态文件路径和 SHA-256，未复制运行数据库或长日志，也未改写 v10 裁决。

| 诊断组 | 实际覆盖 | 结果与限制 |
| --- | --- | --- |
| 5 个 GDB 进程 | 两轮覆盖 cold/typical/refresh/retirement，另有 ASLR 与 GC 阈值对照 | 未复现原故障，未取得 v10 原生故障 C 栈；GDB/allocator/GC 对照改变了诊断条件 |
| 8 个带故障暂停捕获器的正常进程 | 四对 Web/scheduler，各完成原 cold 10 秒，均观察到真实原身份回执更新 | 未复现；另一个合成崩溃只证明暂停/附加 GDB/保留原栈的链路有效 |
| 4 个 stdlib 对照 | 普通 SQLite、blob、Windows bind 数据盘、closed Blob 延迟跨连接关闭，各 20 秒 | 均正常退出；不能排除原缺陷 |
| 3 个公共目录差分 | 真实目录模块、增加 app import、再增加真实 DB/reconcile，各 30 秒 | 均未复现；最后一轮实际执行 33 轮恢复检查，原回执由 unknown 更新为 completed |
| 1 个 Valgrind 进程 | 停留在导入阶段，health 未就绪，cold/成功目录调用/真实恢复均为 0 | 不能作为业务负载的内存检查通过；捕获了独立的主动诊断信号故障 |

Valgrind 过程中，第二次主动 SIGUSR1 在原生 OpenBLAS 线程上触发 `_Py_DumpTracebackThreads` 无效读取并崩溃；这发生在 imports 阶段，尚未进入 v10 的 cold 工作负载。CPython 官方问题 [#116008](https://github.com/python/cpython/issues/116008) 记录了多线程异步 `faulthandler` 转储时的同类风险，并给出通过 Python 信号回调持有 GIL 后转储的规避方式。此独立诊断故障不能被写成已经确认的 v10 根因。

当前 worker 的主动 SIGUSR1 改由 Python `signal.signal` 回调调用 `faulthandler.dump_traceback`；阶段 watchdog 改为 daemon `threading.Timer` 的 Python 回调，阶段正常或异常结束均取消并有界等待结束，避免旧 watchdog 继续转储下一阶段。相关隔离测试在回调内实际检查 GIL，覆盖信号、计时器、取消和异常退出。致命错误自动 `faulthandler` 捕获继续保留；主动诊断栈的时点是 Python 回调执行时，并非信号到达瞬间。

以上共 21 个诊断进程及一个独立合成链路验证，均不属于 capacity acceptance。阴性诊断、诊断器修复及一次校准成功，都不能证明 v10 已修复，也不能证明生产宕机原因。v10 保持 `failed`、根因未确认。诊断修复后已另跑 v11 校准、冻结 v12 并完成下述全时长隔离验收；提交镜像仍须由发布脚本再次执行相同全时长验收，成功后才允许推送。

### 当前 v11 校准与 v12 完整验收

[v11 校准报告](capacity/2026-10-15-local-calibration-v11.json) 为 `calibrated`，202 次 Web 样本全部成功，p95=102.7 ms。[v12 完整报告](capacity/2026-10-15-local-verified-v12.json) 为 `accepted`、失败项为空；[冻结 manifest](capacity/2026-10-15-local-verified-v12-manifest.json) 保存执行前的 profile、worker 与镜像指纹。修复后的 worker SHA-256 为 `151e9800482fe022c93777d178febe1de9c2548771a4593532246a2e67aec19f`。

v12 沿用 v10 的全部额度和阈值，阶段时长仍为 cold/idle/typical/refresh/slow/retirement/restart 各 10/20/30/45/20/120/15 秒。两个容器分别实际执行 memory=1073741824、memory+swap=1207959552、CPU=1，Docker inspect 与 cgroup 相符。

| v12 实测 | Web | Scheduler |
| --- | ---: | ---: |
| 全阶段最大 cgroup memory peak | 380604416 字节（363.0 MiB） | 377806848 字节（360.3 MiB） |
| 全阶段最大进程 RSS | 384892928 字节（367.1 MiB） | 382361600 字节（364.6 MiB） |
| 成功覆盖的唯一退休 key | 32 | 32 |
| 退休后半轮 RSS 增长 | 0 字节 | 368640 字节 |
| 容器 OOM kill 增量 | 0 | 0 |

896 次 Web 样本全部成功，p95=112.0 ms、最大 204.7 ms，满足 500/2000 ms 门槛；宿主 OOM kill 增量为 0，可用内存最低 30897860608 字节。两容器的池、队列及字节峰值均在声明额度内，32 key 全覆盖后目录和消费者引用归零；并发及慢依赖阶段真实原身份核验均推进，终止/重启后原 unknown 身份保留，金融重发和未尝试尾项续跑均为 0。

发布、profile 与诊断相关组合回归为 30 passed、1 个 POSIX 用例在 Windows 跳过；受限 Linux 上真实 SIGUSR1/Timer 用例另有 7 passed。实际发布预检还发现 Windows PowerShell 5.1 在参数默认表达式中无法取得脚本根目录，现已将默认 profile 解析移到脚本体；包含省略 profile 参数的新用例，发布回归另跑 13 passed。该轮容量验收仍使用开发快照镜像 `working-1d41f20-v7`，不能冒充已提交镜像验收；发布脚本默认采用同一 v12 文件，重新构建提交镜像并验收。

## 部署与回退

`--memory-swap` 表示 **内存与 swap 合计**。例如 memory=1 GiB、memory-swap=1152 MiB，swap 上限为 128 MiB。CPU=1 是容器配额，宿主还需给 Web、scheduler 以外的服务留余量。两个 1 GiB 容器的承诺是 2 GiB，不能套用到仅约 3.4 GiB 且已有其他服务的主机而跳过检查。

部署指南中的六个 shell 变量须来自目标主机验收过的 profile。先运行只读主机预检，再按对应 profile 的 application 字段配置环境。Windows 无 cgroup 指标时报告 unavailable；本机 Windows 进程的成功不替代 Linux 容器验收。

归档发布脚本要求已提交的 archive 和冻结的最终 profile，按该文件为烟测容器设置 memory/swap/CPU，并对提交镜像执行完整双容器 acceptance。任何步骤失败都停止后续 push。发布保存不可变 `git-<sha>` 标签和 `latest`，生产 rollout 仍需单独执行。

回退应保留 BN-only 策略、来源证据、未知操作和已验证的资源限额。旧镜像如果不能识别该策略，只允许关闭金融写入及 scheduler 的诊断模式；不能让它重新撮合被过滤历史或重放 unknown 操作。

Windows host contract smoke (pure Python pytest dependencies are mounted read-only; network disabled):

```powershell
powershell -NoProfile -File scripts/verify-container-contracts.ps1 -Image crypto-agent-capacity:calibration-v7 -CapacityProfile scripts/capacity/local-verified-v12.json
```

## 2026-10-15 生产机元数据修复验收与采样校正

应用修复镜像为 `git-8790a1a`，digest 为 `sha256:efc9ebdf5da7f5ff42f617077cc56de0f4c8591e491bec691cba40a4e57dfc88`。本机 `metadata-fix-v13` 完整验收 `20261015T025936Z-82c61278` 为 accepted：736 次 Web 请求、0 错误，p95=256.4ms、最大525.3ms，宿主 OOM 增量0。两个容器分别限制1GiB内存、1152MiB内存与swap合计、CPU=1。真实 Binance 解压响应与目录内存测量见 [运行容量](RUNTIME_CAPACITY.md#binance-spot-metadata-response-sizing)。

生产机 `production-metadata-fix-v1` 首次完整验收 `20261015T030720Z-f6343d58` 保留为 failed，唯一失败项为 `scheduler:retirement_plateau_or_key_coverage`：后半段RSS差值28,274,688字节超过16MiB。32个退休key全部成功覆盖，资源承诺、目录及消费者最终为0，795次Web请求全部成功，OOM增量0。不能将该轮改写为通过。

已确认验证器时点错配：报告的结束RSS=378,691,584字节来自03:11:47–49UTC的缓存采样，当时有两个目录用户，解析承诺268,435,456字节或尚存64,375,552字节目录与客户端承诺；报告中的目录/消费者0却在之后实时读取。03:11:58UTC的采样RSS已经回落到344,879,104字节且承诺为0。该错配会把刷新中的峰值误用于回收后的平台比较。

当前worker改为在批次完成、目录退休与GC后实时读取中点RSS，在线程池关闭、GC及退休完成后实时读取结束RSS，并记录测量来源、时间戳和生命周期。后台采样仍参与峰值统计，不缩小负载、阶段时长、覆盖要求或阈值。相关组合回归60passed、1个POSIX用例在Windows跳过。修正后的实测结果须另行记录；此次诊断本身不替代重新验收。
