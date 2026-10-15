# PostgreSQL 并发与执行恢复

本次切换使用新的 PostgreSQL 数据库，只通过原配置导入接口导入配置。不搬运 SQLite 交易、聊天或记忆历史。交易规则、原操作身份和未确认执行状态的含义不变。服务器更新步骤见 [部署指南](DEPLOYMENT_GUIDE.md)。

## 事务范围

`backend.storage.lock_scope` 将完整范围编码为 PostgreSQL 事务级 advisory lock。提交或回滚会释放锁；不同范围不共用一个数据库全局写锁。唯一约束和请求指纹仍是身份保护的一部分。

| 写入单元 | 锁范围 | 保护内容 |
| --- | --- | --- |
| 配置替换、配置导入、MCP 配置 | `runtime-config` | 同一份运行配置及引用一致提交 |
| 模拟账户 | `mock-account, config_id` | 同账户跨交易对的现金、保证金、仓位与收据 |
| Paper 账户及有序批次子动作 | `paper-account, namespace, user_id` | 账户、借款、订单、原子子收据和父进度 |
| Paper 收据修订 | `paper-receipt, namespace, user_id, operation_id` | 保留原收据并单调追加修订 |
| Paper 排名发布 | `paper-ranking, namespace` | 排名发布的一致性 |
| 实盘现金预留 | `spot-budget, config_id` | 同配置可用现金与预留 |
| 现货退出库存 | `spot-inventory, account_scope, base_asset` | 同账户、同基础资产的退出预留 |
| 通用交易受理及收据 | `trade-operation, config_id, operation_id` | 同 ID 一次受理、指纹校验、原身份恢复 |
| MCP 交易受理 | `mcp-operation, profile_id, operation_id` | 调用方隔离和外部操作 ID |
| OAuth 单次记录 | `mcp-record, kind, key` | 原子读取并消费记录 |
| 保护计划及证据 | `protection-account, account_scope, symbol` | 范围 generation 与计划 version 比较更新 |
| 原管理账户范围 | `management-account, account_scope, symbol` | 管理预约及发现结果 |
| 持续管理权 | `management-resource, physical_account, market_type, resource` | 跨方向资源冲突，包括 BOTH 与 LONG/SHORT 重叠 |
| 成交归属及历史发布 | `execution-source, account_scope, symbol` | 同来源的更新、关联与历史完整发布 |

需要同时处理多个账户时，先按 `(namespace, user_id)` 排序取得账户锁，再读账户或可变父批次；在账户锁内追加收据时，顺序为账户锁→收据锁。资金写入先取得对应账户/资金范围，再读取余额并写入结果。同一个事务里不可先取得收据锁后反向等待账户锁。保护计划读到的版本在写入时重新比较；锁本身不能代替版本校验。

数据库事务包围短暂的受理、预留和结果落库阶段。外部行情/交易请求与已有 worker 生命周期锁按原执行流程处理。数据库异常不会使金融回调自动重试；也不会用新操作 ID 再次提交。

## Worker 锁与数据目录

`management_lock.py` 的目录来自 `DATA_DIR/.management-locks`，不再从数据库文件名推导。Web 与 scheduler 必须共享同一个持久化 `DATA_DIR` 卷。OS 文件锁覆盖真实 worker 的生命周期：HTTP 调用者超时不代表后台 worker 已退出，不能因此释放预约。进程真正退出后文件锁由 OS 释放，账本中的 unknown/未完成状态仍留在 PostgreSQL。

管理预约先取得非阻塞的 worker 文件锁，再取得进程内账户锁，最后在短事务中取得数据库范围锁。竞争 worker 按原契约立即收到 running/busy，不能先阻塞在账户线程锁上并与调用者的等待形成死锁。

此部署仍按同一宿主机上的 Web、唯一 scheduler 和共享应用卷运行。文件锁不是跨宿主机分布式锁；不要据此增加多宿主机执行副本。PostgreSQL advisory lock 也不能证明一个已发出的交易所请求没有执行。

## 扫描与完整历史发布

每张表都有显式 `storage_id`。旧接口里名为 `rowid` 的游标字段只是 SQL 别名，不再引用 SQLite 隐式行号。业务 DTO 会剔除内部 `storage_id`。

`storage_id` 的分配顺序不是提交顺序，因此单纯保存“已经扫描到最大的 ID”会漏掉较早分配、较晚提交的记录。迁移中的触发器与来源行同事务更新这些 revision：

- `execution_source_revisions(account_scope, symbol)`：成交、订单归属及相关保护事件；覆盖插入、更新、删除及修改前后范围。
- `plan_source_revisions(symbol)`：原订单日志、保护计划和事件；覆盖历史计划更新和较小 ID 的迟提交。
- `paper_source_revisions(namespace, user_id)`：Paper 原子操作收据。
- `paper_raw_versions`：可变 Paper 审计内容的版本，用于拒绝跨版本续读。

原始 JSON TEXT 可能含 Python 可读取、JSONB 无法表示的转义字符。原文仍完整保留；无法读取辅助 JSONB 范围时，触发器保守地使已知候选范围失效：同一原始交易对（含冒号结算后缀别名），以及该 config_id 已关联的成交范围。原始查询和 Python 中的账户/交易对过滤不变；这种少见情况可能多做一次重建，不能发布漏掉更新的旧快照。正常 JSON 仍按 payload 中的精确账户和交易对失效。

订单归属索引发现来源 revision 改变时，清理该次派生结果并从头分批扫描。Paper 原始来源的 revision 改变时重新扫描，但保持同一指纹最初冻结的 horizon，不将 horizon 之后新成交混入原判定。循环式后台扫描在到达末尾后回到起点，后续循环可看到迟提交记录；扫描本身不重放交易动作。

保护活动投影由数据库触发器与原计划在同一事务更新，回滚原计划也回滚投影。损坏或超过既有字节预算的 JSON 保持 UNKNOWN，不能当作 DONE 排除。容量模式变化会重置分批重建进度。

历史重建在 REPEATABLE READ 快照下写会话临时表，使用服务器游标分批获取元数据、按字节预算读取 payload。只有完成整个扫描且对应来源 revision 未变才发布。发布时锁定范围并锁住 revision 行；触发器写入者与发布互斥。扫描不完整或相关来源改变时保留上一份完整历史；无关账户/交易对的变化不阻止发布。临时表在成功、未完成或异常退出后清理。归属索引的历史阶段只有在重建成功发布后才消费待处理账户；未发布或扫描未完成时保留重试进度。数据库断连直接报告存储失败，不能作为空结果发布。

## 字节读取与故障语义

历史模块名 `sqlite_blob.py` 暂保留以维持 Python 导入路径；实现仅使用 PostgreSQL `octet_length`、UTF-8 编码及数据库侧字节切片。读取先核对长度和预算，再取有限字节；切片返回的总长度与前次长度不同会要求重新读取。UTF-8 页边界使用增量解码，不能按字符数量冒充字节偏移。Paper 可变审计页同时核验原版本，避免跨页拼接不同版本。

真实 PostgreSQL 连接终止回归覆盖扫描期间断连及 ACK 后收据连接断开。交易所已 ACK、结果写库失败时，对外保留已知提交证据和 `persistence_pending`，持久受理身份保留为待核验/unknown。再次请求同一个 ID 查询原身份，不能再次执行回调。进程中止后也只读取原 client/order ID 的证据；有序批次的未执行尾项不会自动补发。

`DEPLOYMENT_MAINTENANCE=true` 是首次导入时的部署暂停开关。交易和账户变更入口、后台执行由部署层暂停，配置导入和只读核对仍可用；导入配置中的 enabled 值不被改写。完成配置核对并按部署步骤重启后才恢复执行。

## 回归与排查

仅在明确指定的隔离 PostgreSQL 数据库执行测试，例如 PowerShell：

```powershell
$env:TEST_POSTGRES_URL = 'postgresql://test_user:test_password@127.0.0.1:55439/crypto_test'
uv run pytest -q tests/test_postgres_financial.py tests/test_postgres_source_revisions.py tests/test_paper_ordered_batch.py tests/test_execution_plan_index.py tests/test_management_recovery.py tests/test_trade_evidence_concurrency.py
```

测试使用独立 schema、假交易所和假模型，不连接真实账户。新增金融测试覆盖跨进程同 ID 受理、同范围等待与不同范围推进、借款/现金预留并发、原子批次重放、ACK 后落库失败、较小 ID 迟提交、历史发布 revision、UTF-8 与原始审计页、投影回滚、部署暂停。原交易/管理/Paper 测试同时转到 PostgreSQL，保留资金、身份、权限与未执行尾项的断言。旧工具重放预期按已批准的简化执行契约更新：重放返回已保存结果，后台或授权审计读取负责恢复。

2026-10-15 在隔离 PostgreSQL 17.11 上，`test_postgres_financial.py`、`test_postgres_source_revisions.py` 与 `test_execution_plan_index.py` 共 30 项通过。双容器验收也已验证共享卷范围互斥、调用者超时不释放、worker 终止后释放，以及重启仍保留原 unknown 且不重发。完整后端回归结果由最终验收另行汇总。

遇到池等待、锁超时或断连时，先按原 operation_id 检查账本和后台状态，再检查数据库连接、锁等待和磁盘。不要清空 unknown 账本、生成新 ID 重试原金融请求，或用旧 SQLite 文件覆盖 PostgreSQL 新数据。
