# Binance Paper 与单次交易调用验证

验证日期：2026-10-15。范围为本地实现及隔离镜像，不代表生产已更新；未连接真实交易账户，也未修改生产容器。

## 功能与数据

Paper 仅允许 Binance USDT 现货与线性永续。非 BN、来源缺失/冲突、曾被默认值回填的历史资源从默认投影过滤，原账本、资金、债务、保证金、全部预留及原操作指纹保留。同名同方向的新 BN 资源使用独立身份和周期。混合估值返回缺项，不能以零收益参与排名。

模型默认交易入口仅 `execute_trade_actions`；Agent 可写 1 项、只读 0 项，live MCP 7/6 项，Paper MCP 10/7 项。查询操作工具从默认注册和缓存调用映射移除。前置准入后保存原意图，金融 ACK 一次返回；条件单受理与成交分开。后台按原已尝试身份更新父子回执，不补发未尝试替代或批次尾项。

`tests/fixtures/legacy_conditional_ack_*.json` 固定用户案例的脱敏基线：SHORT 条件 close、请求 0.03、已确认成交 0、旧仓位观察 0.06、11 条重复规则及旧可信上下文冲突。原始证据与 hash 保持不变，摘要去重且单项/20 项分别受 4096/32768 UTF-8 字节预算约束。

## 回归记录

- 初次扩大回归：2242 passed、101 failed、24 errors、1 skipped；失败记录保留于本地 `data/full-regression-verbose.txt`。其中包含旧查询工具/ACK 后读取断言、离线目录 fixture、Windows SQLite 连接清理和跨文件工厂污染；修复后重新检查失败模块与全部相关实现。
- 最终跨模块集成：**1287 passed，1 skipped，7 subtests passed**。文件清单和原输出分别为本地 `data/final-regression-manifest.json`、`data/final-targeted-regression.txt`。覆盖 Paper、MCP、ordered batch、目录、总准入、scheduler/management、权限、先到先得、各退出模式、账本/恢复、上下文和回执。
- 受限 Linux 镜像合同验证：**71 passed**，memory=1 GiB、memory+swap=1152 MiB、CPU=1、network=none，挂载只读测试及纯 Python 测试依赖，数据目录由 fixture 独立创建。命令见 `scripts/verify-container-contracts.ps1`。另实际读取 cgroup 验证限额，`/health` 与生成的前端 HTML 均通过。
- `npm run build --prefix frontend` 通过；现有打包体积提示仍为 warning。
- 两套真实 React 浏览器 fixture：中文/英文 × 1440/390，每套 4 场景通过。断言缺失估值为“—”、过滤资金承诺、namespace/账户/订单/事件游标、默认零审计请求、展开一页、固定版本续页、无页面横向溢出及无页面错误。
- 七份 delta 已同步主规范：28 个 ADDED、25 个 MODIFIED 完整匹配，1 个 REMOVED 已删除；既有标题、Purpose、63 个未涉及块及其顺序保留。`openspec validate --specs --strict` 与 change strict validation 通过。v12 全时长隔离容量验收已通过，实测与历史失败记录见 [容量验证](CAPACITY_VERIFICATION.md)，不以单元测试代替实际容器峰值。

截图只含合成账户和脱敏回执：

- [中文手机默认回执](screenshots/bn-capacity-single-call/single-call-default-zh-390.png)
- [英文桌面按需审计](screenshots/bn-capacity-single-call/single-call-en-1440.png)
- [中文混合账户与估值缺项](screenshots/bn-capacity-single-call/paper-scope-zh-1440.png)
- [英文手机原账本审计](screenshots/bn-capacity-single-call/paper-audit-en-390.png)
