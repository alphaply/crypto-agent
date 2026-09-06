# Polymarket 消息面监控

在控制台的 **配置 → 消息源** 中启用 Polymarket，粘贴事件链接，点击「测试连接」，确认返回的事件后加入监控列表。沿用配置页的自动保存，也可点击顶部保存按钮。最多配置 8 个事件，列表会公开显示在看板。

支持中文或英文的 `/event/{slug}` 链接，也支持直接输入 slug。例如：

- https://polymarket.com/zh/event/fed-decision-in-september-762
- https://polymarket.com/zh/event/bitcoin-above-on-september-6-2026

直接调用官方 Gamma REST API `GET https://gamma-api.polymarket.com/events/slug/{slug}`，没有引入交易 SDK，不需要 API key、钱包或签名。读取事件及其子市场的结果概率、买卖报价、最近成交价、24h 概率变化、成交量、流动性、截止日期和关闭状态。属于周期性盘口快照，不是 WebSocket 逐笔行情或完整深度订单簿。买卖报价对应 API 的第一个结果；未知数值显示 `—`，真实的零保留为零。不同价位的 BTC 阈值事件可能重叠，概率不会强制归一化。

看板每分钟请求一次本地接口，后端与 Agent 共用事件缓存，按配置的间隔（60–3600 秒，默认 300 秒）在收到请求时更新。没有看板或 Agent 请求时不独立轮询。事件链接固定对应其日期，不会自动换成次日事件；到期后可在后台替换。所有配置事件作为全局消息上下文提供给各币种的 Agent。

每个事件独立缓存及记录来源状态。采集失败时最多回退到 1 小时内的缓存，前端和 Agent 文本均带过期标记；无有效缓存时标记不可用，不伪造概率。快照加入消息存档、消息摘要和 Agent 提示词，概率作为市场预期而非已确认事实。原有 `NEWS_RISK_ENABLED=false` 会关闭 Agent 的整个消息上下文；公开看板的独立 Polymarket 面板仍由后台监控开关控制。

接口：

- `GET /api/public/polymarket`：公开、缓存的监控数据与来源健康状态。
- `POST /api/config/polymarket/test`：管理员认证后测试指定事件，JSON 为 `{"event": "事件 URL 或 slug"}`；测试不保存配置。
- `PUT /api/config`：通过 `globals.polymarket` 保存 `enabled`、`events`、`refresh_seconds`；包含在完整配置导出中。

新加坡被官方列为交易 close-only 地区，不能将此理解为所有机房均保证访问成功。只读数据无需交易认证，实际网络连通性通过部署后的「测试连接」确认；测试由后端服务器发起。

官方资料（核对于 2026-09-06）：[公开市场数据](https://docs.polymarket.com/market-data/overview)、[按 slug 获取事件](https://docs.polymarket.com/api-reference/events/get-event-by-slug)、[地区限制](https://docs.polymarket.com/api-reference/geoblock)。
