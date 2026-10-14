# QQBot Paper V2 开发与镜像发布（2026-10-14）

开发、验证、构建及版本标签 / latest 推送完成。生产容器尚未重启，QQBot 安装人格、skill 和配置定时投递是集成侧操作。

## 交付与验收证据

| 需求 | 实现与验证 |
| --- | --- |
| 真实行情不能被当成模拟报价 | 行情返回 live_public、market_data_is_simulated=false、production 与实际交易所；只有 execution_mode 为 paper。实际验证 Binance MUB 现货、OKX BTC 永续和 Bitget MU / TENCENT 永续报价。 |
| 查询 MUB、MU 和腾讯 | search_paper_symbols 查真实目录、别名及四个场所。MUB→MU 对应关系经官方披露与实时 Binance 目录确认。Tencent 实时 Bitget 目录及报价确认。 |
| 跨场所漏查修复 | 未指定场所时并行查询 Binance、OKX、Bitget、Bybit，失败作为 missing_sources 返回；只读查价可解析到已确认的其他场所。写入不会静默切换场所，持仓和委托固定保存源。 |
| Bot 自有账户及 PK | Paper Key 命名空间自动补建 bot:xiaojin-mengzhizi，升级为旧 keys 补建。get_paper_bot_account / ensure_paper_bot_account 支持自有账户交易。测试证明 bot 与用户资金隔离且重启不重置。 |
| 排行榜 | get_paper_leaderboard 返回公开参赛成绩与 self/bot 比较。贷款、后台充值及债务修改不会制造交易收益，允许隐藏排行；其他用户的私人持仓、现金和债务不返回。 |
| 持仓及平仓适配 | get_paper_positions、paper_open_position、paper_close_position 支持明确方向、基础币数量、名义金额、保证金金额及原子全平。 |
| 限价委托 | paper_place_order、get_paper_orders、paper_cancel_order、paper_edit_order；预留资金或现货数量，自动撮合后保存真实观察的成交价，待成交不冒充成交。 |
| 止盈止损 | paper_set_protection 与开仓附加保护；OCO 互斥、部分/本周期全量、改单/撤单。旧保护不能平掉重开仓位。爆仓先清理旧退出单，全量触发退出会释放冲突退出委托。 |
| 成交/爆仓提醒接口 | scan_paper_events 和 GET /api/paper/events 提供命名空间隔离、接收者、delivery_key、next_cursor 与 has_more。真实 HTTP 验证自动成交、爆仓及游标续读；仅提供扫描接口，不发送 QQ 消息。 |
| 后台数据 | MCP 页面新增持仓、委托/TP/SL、用户与 bot 排行、成交/爆仓事件和汇总；GET /api/mcp/paper/dashboard 仅管理员 JWT 可读，停用 paper 时仍可查看。 |
| 完整人格与 skill | UTF-8 人格文件、SKILL.md、ZIP 包已交付；skill 校验通过，包内容与独立文件逐字节一致。 |

腾讯排查中，本机 Binance 永续 exchangeInfo 返回 HTTP 451（地区可用性限制），不是 InvalidSymbol，也不能证明腾讯合约不存在。截图中的 `TENCENT/USDT:USDT` 是正确格式；实际 Bitget 还存在 `TENCENTHKD/USDT:USDT`，两者应分别按返回的标的和来源查询。

## 验证

- Paper 与 MCP 回归 **140 项通过**，含真实 MCP SDK 限价提交、修改、撤销和保护更新、并发撮合、OCO、账户与场所隔离、贷款及充值对排行的影响、HTTP 扫描权限及后台数据。
- 前端构建和三个修改组件的定向 ESLint 通过。
- 实际浏览器：Paper Key 创建、现金/债务修改、设置保存及四个后台数据标签，在中文/英文桌面及中文手机通过；页面无横向溢出和 JavaScript 错误。
- `--network none`、空临时数据、scheduler 关闭的镜像烟测：health、SPA、HTTP MCP、bot 账户、借款回执、管理员调整、权限隔离、现货/多空、爆仓、限价预留与成交、OCO、排行榜、真实 HTTP 事件游标、后台数据与重复迁移全部通过。
- 镜像内 **224 个源码/前端文件**与冻结清单 SHA256 逐一匹配；冻结上下文 **225 文件**在推送前与工作区一致。未复制本地 `.env`、数据库、pricing.json 或日志。

截图：[手机委托](screenshots/paper-v2-orders-390-zh.png)、[桌面排行榜](screenshots/paper-v2-ranking-1440-zh.png)、[桌面持仓](screenshots/paper-v2-positions-1440-zh.png)、[桌面事件](screenshots/paper-v2-events-1440-zh.png)。

## 文件与接入

- [完整人格 prompt](qqbot/人格prompt.txt)
- [完整 skill](qqbot/qqbot-paper-trading/SKILL.md)
- [两份文件打包](qqbot/qqbot-paper-package.zip)
- [工具与接口说明](QQBOT_PAPER_MCP.md)

更新服务容器后，QQBot 重新连接并刷新 MCP 工具清单，替换旧版只读人格和旧 skill。旧 Paper Key 可继续使用，不需要创建真实交易所账户。

定时任务用 Paper Bearer Key 调用 `GET /api/paper/events?after_id=<已保存游标>&limit=100`；按 delivery_key 去重，在成功投递或可靠入队后推进游标。失败或无可信投递路由时保留事件。bot 自身事件路由到配置的比赛渠道，不能把内部 bot ID 当作真实 QQ 号。

## 镜像

| 项目 | 值 |
| --- | --- |
| 版本标签 | `alphaply712/crypto-agent:paper-v2-20261014-bb1ffd26eefb` |
| 常用标签 | `alphaply712/crypto-agent:latest` |
| 两标签 digest | `sha256:25b74fd10efc9686e558f726acc6bb04455104c60f42ae7c4067e5d8ffd997f0` |
| Linux AMD64 manifest | `sha256:0d0014f64f93db17b16500958186d10dfa45765c10ad56b34eae4e01f932037a` |
| 源码 SHA256 | `bb1ffd26eefb5fdf8c0ece75671929b34db1b5920203fd4c4298ea40e1dfffaa` |
| 基础 Git HEAD | `8594060d3e6513560423f5ff81b2a42fedd467fd` |
| 发布前 latest digest | `sha256:94d320626199ca3180e5d410bd7bbd836faa89ab9c3fba3dfe50dcd25943c76d` |

```bash
docker pull alphaply712/crypto-agent:paper-v2-20261014-bb1ffd26eefb
# 固定本次版本：
docker pull alphaply712/crypto-agent@sha256:25b74fd10efc9686e558f726acc6bb04455104c60f42ae7c4067e5d8ffd997f0
```

工作区原有未提交改动和 `.agents/` 内容全部保留，镜像包含当前工作区代码及新增功能，标签按冻结源码摘要命名。本次未提交/推送 Git，也未进行 OpenSpec 归档或生产 rollout。更新容器时保留原数据卷及密钥。

本机冻结上下文及来源清单：

- `C:\Users\ASUS\AppData\Local\Temp\crypto-agent-paper-v2-final-jeawuytr`
- `C:\Users\ASUS\AppData\Local\Temp\crypto-agent-paper-v2-final-jeawuytr-source.json`

## 边界与来源

撮合为离散公开行情观察，不回放每笔成交或模拟完整深度；触发价不是保证成交价。新成交开仓的附加保护可在后续扫描生效。已有逐仓杠杆保持固定，paper 保证金规则使用后台设置，不冒充交易所原生股票合约风控档位。暂不模拟资金费。

角色核心性格参照 [作品官方角色资料](https://fxkurumi-info.com/)。粉爪银行和 PK 是同人追加设定；人格可以沉浸式表达，被问及资金/成交性质时仍如实说明。

资产与接口资料：[Binance 官方 MUB/MU 披露](https://www.binance.com/en/square/post/340313977073537)、[Bitget 腾讯股票永续公告](https://www.bitget.com/zh-CN/support/articles/12560603887840)、[Binance 公开行情域名说明](https://developers.binance.com/en/docs/products/spot/rest-api)。静态映射仅用于发现，当前上市与报价由实时目录和 API 确认。

最终 schema 核对修正了兼容 paper_trade 中 stop_loss/take_profit 的类型说明：它们是价格而非基础币数量。修正后 Paper 回归40项和最终镜像完整烟测再次通过，以上镜像标签和摘要均指最终修正版。临时烟测容器与本轮 Vite 进程已清理。
