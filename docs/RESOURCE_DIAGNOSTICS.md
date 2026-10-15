# 资源证据与故障归因

Web 和 scheduler 各自采集进程 RSS、可见 cgroup 的内存当前/峰值/上限、swap、OOM 事件、CPU quota/affinity/节流，以及进程总准入、角色队列、字节承诺、公开目录与 MCP 协调器统计。`GET /api/resources/snapshot` 使用现有控制台 JWT，只读，不注册为模型工具。平台不支持或文件不可读的计数返回 `null`/`unavailable`；真实零、显式 unlimited 和缺项分别表示。

每次启动使用新的 startup ID；记录容器 hostname/ID、镜像 `IMAGE_REVISION`、固定资源配置指纹、UTC 观察时间和固定阶段。Docker 构建须传 `--build-arg IMAGE_REVISION=<commit>`。操作 ID 只用于有限阶段关联，不作为累计指标标签。这里的 bytes 是保守对象/解析承诺，RSS 和 cgroup bytes 是实际观测，二者不相加冒充物理内存。

默认每 3 秒落盘至 `DATA_DIR/resource_evidence/<role>.jsonl`，每文件最多 1 MiB，保留 4 个备份加当前文件；Web/scheduler 分别最多 5 MiB。阶段环形缓冲最多 64 项，溢出以 `stage_records_dropped` 记录；单条完整 JSON 也受文件上限约束，省略阶段增加计数，不截断 JSON。采样在独立 daemon 中，金融 worker 仅向内存缓冲追加固定阶段。采集写入失败标为 unavailable，累计 `persistence_failures`，下一周期重试；不把异常正文或凭据写入证据。进程被强杀时最后一小段缓冲可能丢失，不能据日志缺口断言零占用。

容器内采集器不能在容器死亡后继续工作。因此部署持续证据时还需要独立宿主采集器，读取此前容器身份、镜像、状态和内核日志尾部。它不输出 Docker `Config.Env`，不修改容器、不重启服务、不调用交易接口：

```bash
python scripts/collect-resource-evidence.py \
  --container crypto-agent --container crypto-agent-scheduler \
  --kernel-log /var/log/kern.log --interval 5 \
  --output /var/log/crypto-agent-evidence/host.jsonl
```

宿主服务应按其常规 supervisor/systemd 配置独立于两个应用进程运行，授予读取指定内核日志和 Docker inspect 的权限；不要把 Docker socket 挂进应用容器。上述采集只读，所需 Docker 权限应由操作员按现有运维边界配置。`--once` 可生成一次现场记录，但不能代替持续序列。默认宿主证据同样为 1 MiB 当前文件加 4 个备份。每次 inspect 最多 5 秒，只观察显式指定的 1–8 个容器；每轮只读内核日志末尾 256 KiB，最多保留 64 条相关事件。已删除容器的旧映射保存在之前的轮转记录中，不据当前同名容器追认旧身份。

所有采集时间为 UTC；容器 Created/StartedAt/FinishedAt 保留 Docker 原始 UTC。传统 kern.log 的原时间文本完整保留，不自动猜年份和时区，人工关联时需核对宿主时区、日志日期、旧容器 ID/进程 PID 与请求时段。已有 request/operation ID 用于关联，不能把账户作用域当作 config_id。

报告明确区分四种证据：内核 `global_oom` 证明记录时段发生全局 OOM；同启动 cgroup `oom_kill` 增长证明该 cgroup 有 OOM kill；资源序列只证明所观察的压力；缺少事发记录则为证据不足。被杀进程不等于内存增长根因。重启后 `OOMKilled=false` 只代表当前容器状态，不能否定旧事故，也不能确认后来断连与旧事故同因。宿主 `/proc/vmstat` OOM 总计变化不区分 global/cgroup，容量验收只用它判断有没有新增 kill，不将它单独当作全局 OOM 归因。

可见 cgroup 限额与 Docker inspect 的配置意图区分保存。v2 `memory.swap.max` 是 swap-only；v1 memsw 和 Docker `--memory-swap` 是 memory+swap 总量。CPU 使用的 quota/period 结合 affinity，不能以宿主核数推断应用额度。祖先 cgroup、宿主压力或内核日志不可读时注明观测范围；缺项不视为安全通过。Windows 可读取 WorkingSetSize，但缺少 Linux cgroup/宿主指标时保持缺项。

离线验证见 `tests/test_resource_diagnostics.py`、`tests/test_host_resource_evidence.py`：v1/v2、不可读、无限额、真实零、节流、OOM delta、固定字段无秘密、写入失败重试与轮转边界，以及原 worker 强退后仍能读取前序证据和不同重启身份。双容器容量验收另由 `scripts/verify-runtime-capacity.py` 完成，发布镜像不应用生产限额。

容量验证器和发布烟测按各自角色传入 `RESOURCE_EXPECTED_MEMORY_BYTES`、`RESOURCE_EXPECTED_MEMORY_SWAP_TOTAL_BYTES`、`RESOURCE_EXPECTED_CPU_CPUS`。快照逐项报告 `matched / mismatched / unavailable / undeclared` 和声明值、实际值；生产部署同样须从目标 profile 设置，不能把环境变量本身当作生效证据。进程总字节额度达到或超过实际有限 cgroup 内存上限时，普通准入明确进入压力限制，保留关键恢复额度。
