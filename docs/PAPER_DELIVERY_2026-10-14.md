# QQBot Paper 开发与 Docker 发布（2026-10-14）

开发、验证、Docker 构建与两个标签推送已完成。生产容器尚未重启。

## 已交付

- 独立 `key_type: paper`，不绑定真实交易所账户；禁止访问真实交易配置与真实下单。
- 按命名空间 + 字符串用户 ID 持久化模拟账户，同一命名空间支持 key 轮换和多用户。
- USDT 现货买卖、线性永续多空与部分平仓、用户调整开仓杠杆（后台上限最多 125×）。
- 公开行情和 K 线、共享评分情报、手续费、逐仓保证金与后台爆仓维护。
- 模拟银行借款、还款与持久化负债；后台调整现金、负债、额度、市场与启用状态。
- 操作回执与余额变动使用 SQLite 原子事务，稳定 ID 重试不会重复扣款或借款。
- 完整配置迁移保留 paper key 类型、命名空间与全局设置。

使用方法和工具示例见 [QQBot Paper MCP](QQBOT_PAPER_MCP.md)。模拟成交为市价成交，不包含挂单、TP/SL 或资金费。

## 验证

- Paper + MCP 回归 **120 项通过**。
- 前端重新构建与两个修改组件的定向 ESLint 通过。
- 真实后台 UI：创建 Paper Key、调整账户现金/负债、保存设置；桌面中文、英文和手机中文全部通过。
- 实际无密钥读取 OKX BTC/USDT 现货和 BTC/USDT:USDT 合约 ticker / candles 成功。
- `--network none`、空数据目录、scheduler 关闭的临时容器：health、SPA、管理员登录、真实 HTTP MCP、账户创建、借款重试、后台调整、权限隔离，以及镜像内现货/多空/爆仓账本与重复迁移均通过。
- 镜像内 **219 个源码/前端文件**逐一与冻结 SHA256 清单核对通过。镜像不包含本地 `.env`、交易数据库或 `pricing.json`。
- 构建前后工作区与 **220 文件**冻结构建上下文完全匹配。

截图：[中文桌面](screenshots/paper-trading-1440-zh.png)、[英文桌面](screenshots/paper-trading-1440-en.png)、[手机](screenshots/paper-trading-390-zh.png)。

## 发布结果

| 项目 | 值 |
| --- | --- |
| 版本标签 | `alphaply712/crypto-agent:paper-20261014-1e5e5b459ba6` |
| 常用标签 | `alphaply712/crypto-agent:latest` |
| 两标签 digest | `sha256:94d320626199ca3180e5d410bd7bbd836faa89ab9c3fba3dfe50dcd25943c76d` |
| Linux AMD64 manifest | `sha256:c8b541c30cb1d7f36384fc3b065229165f90578d2c4912d9712bf68ccf601ea6` |
| 构建源码 SHA256 | `1e5e5b459ba6acace7c34108fb6df18cdfcdbba4a9eaaae8dea9f5bcfa1ac136` |
| 基础 Git HEAD | `8594060d3e6513560423f5ff81b2a42fedd467fd` |
| 发布前 latest digest | `sha256:381a744bc2aa989a9f3fa01861a95ac3a1b1a9274738825a87dd529a30383942` |

工作区原有大量未提交改动，保留了原文件与 `.agents/` 内容。本次构建包含当前工作区已有功能与新增 paper 功能，使用源码摘要版本标签；未将工作区改动标记为旧 commit 的构建，也未提交或推送 Git、执行 OpenSpec 归档或生产 rollout。构建采用冻结最小上下文与 `Dockerfile.prebuilt`，不挂载生产数据。

```bash
docker pull alphaply712/crypto-agent:paper-20261014-1e5e5b459ba6
# 固定本次发布：
docker pull alphaply712/crypto-agent@sha256:94d320626199ca3180e5d410bd7bbd836faa89ab9c3fba3dfe50dcd25943c76d
```

更新生产容器时保留原数据卷和密钥，后台 MCP 页面点击「创建 Paper Key」后给 QQBot 配置 `/mcp` URL 和 `Authorization: Bearer <key>`。不要把 bot key 分发给 QQ 用户；bot 从可信消息事件传入发送者用户 ID。

本机构建与来源清单：

- `C:\Users\ASUS\AppData\Local\Temp\crypto-agent-paper-image-o_66wpzk`
- `C:\Users\ASUS\AppData\Local\Temp\crypto-agent-paper-image-o_66wpzk-source.json`

临时烟测容器和本轮 Vite 进程已清理。以上临时构建上下文保留用于复核。
