# Spec Delta

## Purpose

使项目以 PostgreSQL 作为统一运行数据库，并通过现有配置导入入口在空白数据库上重新部署。保持配置、交易和聊天的既有使用语义，同时提供可验证的容器更新、持久化及备份恢复流程，避免遗漏独立 SQLite 路径或在数据库故障时静默切换存储。

## ADDED Requirements

### Requirement: Runtime persistence uses PostgreSQL explicitly

系统 SHALL 使用明确配置的 PostgreSQL 保存运行配置、业务记录、MCP/Paper 状态、记忆和聊天检查点。数据库连接失败 SHALL 按对应调用原有失败契约明确报告，不能读取旧 SQLite 或初始化替代空库来返回成功。运行日志 SHALL 不泄露数据库凭据。

#### Scenario: A new installation starts with an empty database
- **WHEN** 部署者为新版配置一个空白 PostgreSQL 数据库并执行初始化
- **THEN** 所有运行存储在 PostgreSQL 建立，应用可以进入配置导入流程，不读取旁边遗留的 SQLite 历史或创建新的 SQLite 运行库

#### Scenario: PostgreSQL becomes unavailable
- **WHEN** Web、MCP 或 scheduler 的数据库连接失败
- **THEN** 相关操作明确报告依赖不可用，既有执行状态不被改写为空记录或成功，连接信息中的密码不出现在响应或日志中

### Requirement: Schema upgrades are explicit and repeatable

系统 SHALL 提供有版本记录的初始化及升级入口。重复执行已完成版本 SHALL 不清空数据或重置业务状态；并发启动 SHALL 不竞争修改表结构。数据库结构不符合当前版本时 SHALL 在接收依赖该结构的工作前明确报错，不在普通请求中吞掉迁移错误继续运行。

#### Scenario: Two application processes start together
- **WHEN** Web 与 scheduler 同时连接到已初始化数据库
- **THEN** 双方使用相同结构版本，不重复执行互相冲突的建表或字段修改

#### Scenario: Migration fails
- **WHEN** 版本升级中的数据库步骤失败
- **THEN** 失败及已完成阶段可识别，依赖失败步骤的应用启动停止；重新执行不把失败版本伪装为完成

### Requirement: Existing configuration packages remain importable

系统 SHALL 保持现有 version=1 配置 JSON、配置导入路由及既有字段语义兼容，支持策略、模型供应商、交易所配置、全局设置、Prompt、计价和包内 MCP 设置。包含的密钥 SHALL 使用目标环境密钥重新加密；脱敏包不产生虚构密钥。配置验证失败时 SHALL 保持既有配置数据库、运行快照及文件不被替换。

#### Scenario: An old complete export is imported into PostgreSQL
- **WHEN** 用户向空白新版实例导入旧版完整配置包
- **THEN** 原配置身份、引用、参数及受支持设置被恢复，导出后可核对语义一致，密钥在目标环境解密可用且不以明文保存在配置表中

#### Scenario: The destination uses a different master key
- **WHEN** 配置包包含可导入密钥，目标环境使用不同 CONFIG_MASTER_KEY，且导入不改写目标环境变量
- **THEN** 导入使用目标环境当前密钥加密，不要求访问旧 SQLite 或旧环境加密密钥

#### Scenario: An invalid package is submitted
- **WHEN** 包版本不受支持或配置未通过既有校验
- **THEN** 接口沿用明确失败语义，原配置、运行快照和 Prompt 保持不变，不因换库绕过权限或业务校验

#### Scenario: Imported configuration survives application replacement
- **WHEN** 导入成功后重建 Web 和 scheduler 容器并保留其配置与持久卷
- **THEN** 配置、可解密密钥、Prompt 和计价仍可读取，Web 与 scheduler 使用同一份已导入配置

### Requirement: Fresh deployment does not imply historical recovery

本次升级 SHALL 从空白业务库开始，仅通过原配置包恢复其已有配置内容。系统 SHALL 不导入旧交易记录、记忆、聊天检查点或操作账本，不删除旧 SQLite 文件，不因配置身份相同而认定已有交易所仓位已归本地管理。配置中原有初始化参数的含义 SHALL 保持不变。

#### Scenario: Old database files remain on the server
- **WHEN** 新版启动时服务器仍保存旧 SQLite 文件
- **THEN** 这些文件不被自动导入、改写或删除，新业务历史从 PostgreSQL 开始

#### Scenario: The exchange still has positions or orders
- **WHEN** 空白实例导入了原交易所与策略配置，而交易所上仍有仓位或挂单
- **THEN** 导入过程不会生成虚构成交、恢复标记或管理归属，也不执行平仓、撤单或认领；后续交易仍沿用既有账户权限和管理规则

### Requirement: Storage replacement preserves business transaction semantics

系统 SHALL 保持原查询范围、排序、统计口径、幂等身份及事务一致性。并发配置更新、资金预留、操作受理和计划保存 SHALL 不因 SQLite 写锁消失而发生重复执行、丢失更新或超额预留。数据库事务失败 SHALL 不触发交易所写操作重放，也不把可能已提交的交易变为明确未执行。

#### Scenario: Concurrent workers receive the same operation
- **WHEN** Web 与 scheduler 的不同连接处理相同操作身份或同一资金/管理范围
- **THEN** 原唯一性和协调约束继续成立，不重复提交交易或重复扣减，已有结果与未决身份可继续核验

#### Scenario: Persistence fails after an exchange acknowledgement
- **WHEN** 交易所已返回受理响应，而随后数据库提交失败或连接中断
- **THEN** 处理保持既有已受理/未知事实及原请求身份，不自动换身份或重发交易，也不把数据库错误表述为交易所明确拒绝

#### Scenario: History is paged while unrelated writes occur
- **WHEN** 成交、审计或 Paper 历史通过稳定游标分批读取，其他范围同时写入
- **THEN** 分页身份在重启和更新后保持稳定，过滤与统计口径不变；读取不完整不会被表示为完整历史

### Requirement: Chat state persists independently of application containers

新版产生的聊天检查点 SHALL 存入 PostgreSQL，保持既有会话隔离、恢复、中断续接和删除行为。容器重建 SHALL 不丢失已保存状态；删除指定会话 SHALL 不删除其他会话或业务账本。

#### Scenario: A chat resumes after restart
- **WHEN** 一个新版本会话已保存消息或中断状态，随后应用容器重建
- **THEN** 相同会话可以恢复其已保存状态，不创建本地 SQLite 替代检查点

#### Scenario: A chat is deleted
- **WHEN** 用户删除某个授权会话
- **THEN** 该会话的检查点和关联会话记录按现有规则清理，其他会话及交易数据保留

### Requirement: Database maintenance operates on the selected runtime store

数据库诊断、导出和维护 SHALL 针对 PostgreSQL，保留既有权限、清理范围、预览一致性及先备份行为。备份 SHALL 可恢复且注明格式，不能把 PostgreSQL 备份伪装成 SQLite 文件。原 SQLite 上传副本分析 SHALL 保持只读，不合并至运行库；不适用的 SQLite 指标 SHALL 明确标识而非填零冒充实测。

#### Scenario: A database backup is restored in isolation
- **WHEN** 管理员导出 PostgreSQL 备份并在隔离的同版本环境恢复
- **THEN** 配置、业务数据和聊天检查点可核对一致，备份不依赖应用容器内部临时文件长期存在

#### Scenario: A cleanup preview becomes stale
- **WHEN** 用户提交清理前，预览选中的记录已经发生变化
- **THEN** 清理沿用过期预览拒绝行为，不扩大删除范围；备份失败也不继续删除

#### Scenario: An old SQLite copy is inspected
- **WHEN** 用户上传旧 SQLite 文件用于数据库页面分析
- **THEN** 仅分析上传副本，不恢复配置或业务记录，不更换当前 PostgreSQL 连接

### Requirement: Container deployment separates application updates from database data

系统 SHALL 提供同主机 Web、scheduler、PostgreSQL 的 Compose 部署方案，数据库使用独立持久卷与容器内部网络，默认不映射宿主机数据库端口。更新流程 SHALL 保留配置和数据，明确备份、结构升级、同版本应用重建及验证顺序；发布镜像 SHALL 不被描述为已更新服务器容器。

#### Scenario: An application image is updated
- **WHEN** 部署者按文档拉取新版应用镜像并完成备份及所需结构升级
- **THEN** Web 和 scheduler 使用相同目标镜像重建，数据库持久卷保留，不通过应用更新隐式升级 PostgreSQL 主版本

#### Scenario: The first configuration-only cutover is performed
- **WHEN** 部署者从旧 SQLite 实例切换到 PostgreSQL 空白实例
- **THEN** 文档指导先导出配置并停止旧执行者，再初始化新库、启动无调度的 Web、导入核对、最后启动唯一 scheduler，并说明配置导入不能恢复旧仓位管理责任

#### Scenario: A deployment step fails
- **WHEN** 备份、结构升级或健康检查失败
- **THEN** 更新流程停止依赖该步骤的后续动作，记录已完成和待完成阶段；回退不删除数据卷，也不以旧数据库覆盖新版已经发生的交易事实
