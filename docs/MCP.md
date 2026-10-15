# MCP 外部助手接入

## 默认目录与 v3 顺序执行

重新 `tools/list` 获取按当前 Key 类型、scope 和授权 profile 生成的目录。live 可写目录为 7 项，只读为 6 项；paper 可写为 10 项，只读为 7 项。多个 profile 使用同一套工具名称，上下文分别列出各 profile 能力。目录不缓存其他用户的权限；每次调用仍重新检查身份与范围。

核心名称为 `get_trading_context`、`search_symbols`、`get_market`、`get_news`、`get_account_state`、`get_trade_records`、`execute_trade_actions`。paper 额外有 `paper_bank`、`configure_paper_account`、`get_paper_leaderboard`。只有 cancel 的 live Key 也可看到执行入口，但只能执行撤单；paper 撤单需要 read + trade。

先读 `get_trading_context` 获取 profile、模式、授权 symbol 和动作能力。`get_account_state(include=["positions"])` 仅返回仓位；需要余额、库存或挂单时显式选择相应 facet。`get_trade_records(record_type="orders"|"order"|"fills"|"history")` 区分订单、指定 ID、实际成交与历史。单订单使用 order_id；operation_id 仅标识原执行，不提供操作查询工具。

记录查询支持 limit/offset，默认投影订单、数量、费用、观察时间与管理事实；`detail=true` 返回原完整记录。live 订单列表沿用 open 范围，结束的订单可按 ID 单查；fills/history 使用交易所实际成交记录，保留原查询的 200 项观察窗口。到达窗口上限会返回 window_limit/window_exhausted，不能将该窗口视为全部账户历史。Paper history 返回该账户成交、借还款及账户调整事件，分页范围限于当前 namespace/user。

所有新交易使用 `contract_version=3` 与 1–20 项有类型的 actions。例：已知订单撤销后设置替代退出（一次调用、按请求顺序执行）：

```json
{"profile_id":"swap","operation_id":"replace-stop-v3-0001","contract_version":3,"actions":[{"action":"cancel","symbol":"BTC/USDT:USDT","order_id":"algo:123","reason":"撤旧止损"},{"action":"close","symbol":"BTC/USDT:USDT","side":"LONG","order_type":"stop_market","amount":0.1,"trigger_price":58000,"reason":"替代止损"}]}
```

整批先检查结构、字段、版本、权限和已知模式能力，再逐项核验余额、预留、目标和仓位。合法前置撤单可释放下一项需要的资源。默认 failed/pending/unknown 后停止；`execution_policy="independent"` 仅继续依赖满足且自身可准入的项，`depends_on` 是不重复的此前索引，前项必须 completed。限价入场 submitted 不等于成交，也不等待将来成交；没有已成交仓位时不能预挂未来退出。

REAL/STRATEGY 入场仍限价；现货 open/close 使用 LONG 买入/卖出；独立合约不接受 protect，使用 close 创建退出单。附带保护、现货原生 OCO 和 paper 原有保护使用其支持的 protect。`edit.remaining_amount` 表示基础币未成交数量，省略字段保留；all 不隐式变为 fixed。具体组合以当前上下文与 schema 为准。

可见且获交易授权的人工仓位/订单在有效管理动作中自动核验并认领，不要求先 adopt。管理者先到先得且跨空仓、暂停和重启保留；范围外、管理竞争、缺失执行证据或不支持原生目标仍明确拒绝。查询和工具发现不认领。

每个父请求保留稳定 operation_id 与子项映射。批次非原子，后项失败不回滚前项；相同 ID 重试只查询或核验已尝试项，绝不补发 not_executed 尾部。系统后台自动核验原身份。用户通过授权 HTTP/UI 按需读取原始/最新审计；操作查询工具及其旧适配器已移除。默认单项4096、整批32768 UTF-8字节以内，保留逐项真实状态和恢复身份，`include=["account"]` 最多返回一次末尾账户摘要。

下面旧工具名称与 v2/无版本示例为兼容客户端说明，不在默认模型目录公布。旧金融客户端可继续按原名称、参数和 operation_id 调用；操作查询名称不再可调用。原金融权限、指纹、数量和回放含义保留；新客户端使用本节目录。

## 可用性预算与数据完整性

MCP 查询、金融执行、OAuth 认证各自使用有限线程池，并共同受进程总准入、角色额度、队列和字节预算限制；独立线程池不能绕过总额度。存储另外限制为 2 个并发，等待最多 0.25 秒。超额请求返回 busy；已排队请求最多等待 2 秒。网页同步接口继续使用 Web 自己的线程池。认证及授权页面的数据库和密码校验在认证池执行，SQLite 锁等待默认 0.25 秒；建表只在启动迁移执行。

预算包含排队、市场初始化、交易所 HTTP 和返回处理。查询默认 20 秒、执行 40 秒、认证 3 秒；每次交易所 HTTP 最多取 8 秒与剩余预算中的较小值。耗尽预算后不会启动后续请求。同步网络请求不能被 HTTP 断开强行终止，正在运行的 worker 继续占用真实额度直至结束；不能以请求断开为理由重发交易。结果可能已送达交易所时保留原操作身份和 unknown/pending，由系统后台核验原身份；确认收到的 ACK 不会被后续审计失败覆盖。最终回执有短数据库落盘宽限，不允许在宽限期再启动网络操作。

市场目录缓存 5 分钟，同账户配置版本/交易所/市场仅刷新一次；每个请求有独立 CCXT 客户端并复制元数据，凭证变化使旧 key 失效。同进程同账号的客户端共享有权重的限速协调；多个 Web 进程仍受交易所账户级限速约束，应控制进程数量及实际 API 权重，不能通过加线程避开限速。缓存不包含可用来批准写入的私有仓位/订单证据。

公开 ticker 缓存 2 秒，K 线缓存 15 秒；`get_market` 保留 `symbol/timeframe/ticker/candles`，新增 `status`、`missing_sources`、每来源 `observed_at` 和 `sources`。ticker 成功而 K 线失败时 `status=partial`、`candles=null`，观察时间仍是原获取时间。仓位/订单/连接配置成功仍返回列表。普通或条件订单通道失败时返回明确的缺项工具错误，不把部分列表当作全部订单；单订单只取得本地记录时标明 `local_record/pending`，不是实时成交证明。

上述参数对应 `.env.template` 中 `MCP_*` 项。并发/队列额度在进程启动确定，修改后需按部署流程重启。2 核 2G 环境建议先以 read=2、execute=1、auth=2 验证，查询预算 20 秒、依赖 8 秒和排队 2 秒保持不变，再按实测调优。不要以增加代理或交易所超时替代故障定位。后台诊断显示当前 active/queued/capacity/queue_limit；`MCPResilience` 日志包含自动生成的 request_id、脱敏 scope_ref、阶段、结果和耗时，不记录调用参数、认证头或完整错误 URL。

账户状态投影中的只读独占范围发现有独立子预算 `MCP_MANAGEMENT_DISCOVERY_BUDGET_SECONDS`，默认 5 秒，并受外层请求剩余预算约束。发现失败保留实际持仓和明确的对账缺项，不把账户说成空仓。

金融范围同时受 SQLite 持久化 reservation 和共享数据卷 OS 文件锁保护。锁位于交易数据库父目录 `.management-locks`，Web 与 scheduler 必须共享该目录；Windows 使用非阻塞字节锁，Linux 使用非阻塞 flock。存活 worker 从准入至最终状态提交都持锁，后台不能因 ACK 已可读而提前释放；进程退出只释放 OS 锁，未知数据库记录仍须原操作证据恢复。运行中不要删除锁文件或清空该目录，也不要把锁目录迁到各容器独立的 `/tmp`。

## MCP 分层排障

按同一时段记录以下结果；模板和当前实测见 [MCP 故障记录](MCP_OUTAGE_2026-10-14.md)。先核验失败层，再执行相应修复。

| 层 | 检查 | 结论依据 |
| --- | --- | --- |
| DNS / TCP / TLS | 域名解析、连接时间、TLS 完成时间、证书与 SNI | TLS 未完成时没有 HTTP，更不能归因于交易所工具 |
| 公网路由 | 首页、`/health`、两条标准发现路径和两条兼容路径 | 首页成功而发现 404/HTML，优先核对代理路径及当前部署版本 |
| 代理 / 容器 | 同时段 access/error、容器状态/资源、映射端口与容器内 health | 本机成功、公网失败偏向入口；本机也失败再排查应用 |
| 认证 | 已授权 `get_trading_context`，issuer/resource 与公网地址一致 | 元数据可读不等于授权 MCP 调用成功 |
| 交易所 | 分别读取余额、单标的订单、ticker/K 线 | 单个依赖失败不能直接称作 Web 或整个 MCP 宕机 |

健康请求不带密钥。认证验证使用已有连接的只读工具，不把 Bearer 写进命令行、日志或故障报告。不得通过重放交易、改价、撤单或换 operation_id 测试连接。代理改动先保存原配置并运行 `nginx -t`；容器重建前记录原镜像 digest、端口、数据卷和密钥引用，不输出完整 `.env`。修复后重跑各层相同检查，记录时间、状态和失败层；缺服务器访问或认证验证时明确保留待验收项。

服务使用官方 Python MCP SDK，在现有 FastAPI 服务的 `/mcp` 提供 Streamable HTTP。后台 → MCP 管理连接配置、API Key、OAuth 授权和调用审计。外部助手共享消息摘要，可查询行情、余额、持仓和委托，并按授权调用现有交易工具；不会创建内部定时 Agent 或启动模型摘要任务。

## 1. 配置服务

1. 先在后台保存交易所账户，再在 MCP 页面建立连接配置，选择账户、现货/合约、标的范围、退出模式和额度。「指定标的」支持搜索交易所市场目录、多选和分页；「不限制标的」允许该账户所选市场中的有效交易对。旧配置保持指定标的范围，不会因升级自动放开。
2. 将“服务公开地址”设为实际域名的 origin，例如 `https://agent.example.com`，不包含 `/mcp` 或其他路径。初始默认值来自 `MCP_PUBLIC_URL`，未设置时为 `http://localhost:7860`。
3. 保存后重启后端，使 OAuth issuer、资源地址和 Host 校验使用新地址。启用状态、权限和连接配置无需重启。
4. 外部服务需要可访问的 HTTPS 地址。反向代理应保留公网 `Host` 和 `Authorization`，透传 `/mcp`、`/oauth/*`、`/.well-known/*`；不要把这些路径交给 SPA。`localhost` 只适用于同一台机器上的客户端。

| 用途 | 实际路径 |
| --- | --- |
| MCP URL | `/mcp` |
| 受保护资源发现 | `/.well-known/oauth-protected-resource/mcp` |
| 兼容资源发现（401 响应指向此地址） | `/oauth/resource-metadata` |
| OAuth 授权服务器发现 | `/.well-known/oauth-authorization-server/oauth` |
| 兼容 OAuth 发现 | `/oauth/.well-known/oauth-authorization-server` |
| OAuth issuer | `/oauth` |
| 动态客户端注册 | `/oauth/register` |
| 授权、令牌、撤销 | `/oauth/authorize`、`/oauth/token`、`/oauth/revoke` |
| 登录与授权选择页 | `/oauth/consent` |

## 2. WorkBuddy / Bearer API Key

在 MCP 页面创建 API Key，选择允许的连接配置及权限。复制页面生成的 JSON 到支持远程 HTTP MCP 的客户端。WorkBuddy 的连接器文档提供用户自填 Token 模式；选择该模式，将 API Key 作为 Bearer 凭证。具体配置入口随版本变化，以其 [官方连接器文档](https://open.workbuddy.cn/docs/connector) 为准。

```json
{
  "mcpServers": {
    "crypto-agent": {
      "url": "https://agent.example.com/mcp",
      "headers": {
        "Authorization": "Bearer <后台创建的 MCP_API_KEY>"
      }
    }
  }
}
```

这是支持 `url` / `headers` 的客户端配置示例。若客户端要求传输类型，选 Streamable HTTP；不是 stdio 或旧式 `/sse`。API Key 只能访问 MCP，不能作为后台登录凭证。密钥明文仅在已登录的后台显示，服务端持久化时使用配置主密钥加密，可随时撤销。

## 3. ChatGPT / OAuth

先在后台 MCP 页的「连接 ChatGPT」中点击「检查连接」。检查会区分未应用的公开域名、代理返回 HTML/404、旧 OAuth 缓存与正常授权入口。普通 URL 与带缓存绕过参数的 URL 都会检查，避免误把仍缓存 localhost 的地址判为正常。检查通过只代表公开发现接口可用，完整登录仍需在 ChatGPT 中完成。

按照 [OpenAI 的自定义 MCP 接入说明](https://developers.openai.com/api/docs/guides/custom-mcp-server)，在支持该功能的 ChatGPT 工作区添加自定义 MCP 服务，Server URL 填 `https://agent.example.com/mcp`，认证选择 OAuth。可用时选择动态客户端注册（DCR），随后在本站授权页面用后台登录密码验证，选择连接配置及所需权限。该页面允许取消交易和撤单权限，仅授予查询。完成后安装/启用该连接并在聊天中调用工具。

DCR 不可用时，使用后台「无法自动注册？生成自定义 OAuth 凭据」：

1. 在 ChatGPT 选择「自定义 OAuth 客户端」，复制该页面提供的完整回调 URL。不要自编客户端 ID，也不要把 API Key 当作 OAuth Client Secret。
2. 将回调 URL 粘贴到后台，选择可申请的 `read`、`trade`、`cancel` 权限，点击「生成并注册客户端」。默认只允许查询；如需要交易，请在此选择相应权限。
3. 将生成的 Client ID 和 Client Secret 填回 ChatGPT，Token 端点身份验证方式选 `client_secret_post`。密钥只在本次创建结果中显示。
4. 如需手填高级字段，展开后台的「需要手填其他 OAuth 字段？」逐项复制。基础 Scope 为 `read`，默认请求 Scope 使用已注册的权限范围；OIDC 关闭，不填写 `openid`。
5. 在本站登录授权页选择账户配置及权限并确认。创建客户端本身不会授予账户访问权限。重建 ChatGPT 连接导致回调 URL 改变时，需用新回调重新注册。

| ChatGPT 高级字段 | 示例（替换为自己的域名） |
| --- | --- |
| 授权网址 | `https://agent.example.com/oauth/authorize` |
| Token URL | `https://agent.example.com/oauth/token` |
| 注册地址 | `https://agent.example.com/oauth/register` |
| 授权服务器基础地址 | `https://agent.example.com/oauth` |
| 资源 | `https://agent.example.com/mcp` |

自定义客户端也需要正确的发现接口。若诊断显示 `/.well-known/` 返回 HTML 或 404，检查 Nginx/宝塔的证书验证和静态文件规则；OAuth 路径必须转发到同一个后端容器。可为以下两个路径添加精确匹配，`proxy_pass` 使用站点现有上游地址，保留公网 Host（示例端口需替换为实际映射端口）：

```nginx
location = /.well-known/oauth-protected-resource/mcp {
    proxy_pass http://127.0.0.1:7860;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_cache off;
}
location = /.well-known/oauth-authorization-server/oauth {
    proxy_pass http://127.0.0.1:7860;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_cache off;
}
```

这些精确路径不会覆盖 `/.well-known/acme-challenge/` 证书校验。OAuth 响应已设置 `Cache-Control: no-store`；若代理/CDN 已有旧响应，还需清除旧缓存。服务保留标准发现地址，并在未认证 `/mcp` 的 `WWW-Authenticate` 中提供 `/oauth/resource-metadata` 兼容入口，以绕开常见的根目录规则拦截。

ChatGPT 的此类连接使用用户 OAuth，不接收自定义 API Key。当前实现采用授权码、PKCE S256、精确 redirect URI 校验和资源地址校验；支持 HTTPS 或 loopback HTTP 回调。WorkBuddy 的私有协议回调未启用，使用上面的 Token 方式。客户端能力、工作区开放范围和界面入口请以 [OpenAI 认证文档](https://developers.openai.com/plugins/build/auth) 为准。

授权码有效期 2 分钟，访问令牌 1 小时，刷新令牌 30 天；刷新会轮换令牌，旧令牌失效。后台 OAuth 连接列表可撤销整次授权，客户端也可调用标准撤销端点。公开域名变更后旧域名令牌不能用于新资源，需要重新连接。

如果点击「授权连接」后停留原页，请先刷新授权页。浏览器可能会把 `form-action 'self'` 同时用于限制表单提交后的跨站跳转；本服务仅额外允许已经通过 SDK 精确校验的回调源，反向代理不要再注入更严格的 `form-action 'self'` 响应头。授权完成和授权码在同一 SQLite 事务提交；同一浏览器刷新或重复提交会在原授权码仍有效且未兑换时恢复同一个回调，不会新增授权码。已兑换的请求显示「授权已经处理」，请返回客户端检查连接状态。

授权页面有效期仍为 10 分钟，每次刷新不会延长。过期页面提供「重新打开授权页面」入口，保留原请求参数并重新经过 SDK 校验及密码验证；若客户端也超时，应从 ChatGPT 重新点击连接以生成新的请求。密码或 Cookie 校验失败会在页面给出可恢复操作；不同授权标签页使用独立 Cookie，避免相互覆盖。PKCE S256、精确回调地址、资源与权限校验保持不变。

OAuth 状态保存在 `trading_data.db`，重启和不同工作进程需要访问同一数据库并使用同一 `CONFIG_MASTER_KEY`。多副本各自持有独立数据库会丢失请求上下文，不能靠延长有效期修复。排障可查看 MCP 审计中的 `oauth_authorize`、`oauth_consent`：`completed`/`denied` 表示审批结果已落库，`callback_reissued` 表示恢复回调，`expired`/`missing`/`csrf_failed` 可区分真实过期、状态缺失和 Cookie 失败。审计仅记录事务摘要与客户端 ID，不记录管理员密码、授权码、完整回调查询串或令牌。

## 4. 权限与旧客户端兼容工具

| 权限 | 能力 |
| --- | --- |
| `read` | 查看共享消息、已授权配置、可用标的、行情、余额、持仓、委托、现货归属库存、交易参数定义及本调用者回执 |
| `trade` | 现货限价买入、归属库存卖出及原生止盈/止损/OCO；合约开仓、加仓、显式接管手动仓、减仓、平仓、改单和保护设置 |
| `cancel` | 撤销此 MCP 配置拥有的委托及现货退出单 |

所有令牌都包含 `read`。交易或撤单必须明确绑定至少一个 MCP 配置；没有配置授权的查询令牌只能读取共享消息等公共 MCP 信息。含交易及撤单的批次同时要求 `trade` 和 `cancel`。

查询工具为 `list_profiles`、`list_symbols`、`get_news`、`get_market`、`get_balance`、`get_positions`、`get_orders`、`get_spot_inventory` 和 `get_trading_tools`。`get_news` 读取全局缓存摘要及新鲜度，不触发抓取或模型计费。余额是账户余额，同一账户的多个策略不能相加；账户余额也不等于该 MCP 配置有权卖出的库存。

合约退出需要匹配该配置的持仓归属和退出周期。`pos_side=LONG` 表示平多，`SHORT` 表示平空，不能按委托的买卖方向选择。若返回周期缺失、已结束或待核验，请先核对配置、标的、持仓方向及原始开仓记录；不能换配置或新增开仓来绕过归属校验。批次中 `not_executed` 表示之前的操作失败或未知，未提交该项；已确认无仓的跳过项不阻止其后操作。详见[合约独立退出排查](WORKBENCH.md#合约独立退出与平仓排查)。

先用 `list_profiles` 取得获授权的 `profile_id`，再调用 `list_symbols` 搜索该账户、市场类型及配置范围内的实际可用标的。例如：

```json
{
  "profile_id": "spot-profile",
  "keyword": "ETH",
  "quote": "USDT",
  "limit": 20,
  "offset": 0
}
```

`keyword` 匹配标的或基础币，`quote` 精确筛选计价币，二者可省略。`limit` 默认 50、范围 1–200；`offset` 默认 0。响应包含 `symbols`、`total` 和 `has_more`，有下一页时将 `offset` 加上 `limit`。授权范围和市场类型先过滤，再计算分页总数。后续调用直接使用返回的 `symbol`，例如现货 `ETH/USDT`、线性永续 `ETH/USDT:USDT`；目录不返回反向合约、到期合约或非活跃市场。

交易优先使用以下扁平工具。每个写入工具都需要顶层 `profile_id`、`symbol`、`reason` 和调用者提供的 `operation_id`；下表列出其余参数，`?` 表示可选。`amount` 使用基础币数量，不是计价币金额或合约张数，网关不会猜测换算。

| 工具 | 其余参数与用途 |
| --- | --- |
| `buy_spot` | `amount`、`entry_price`：一笔现货限价买入，`amount × entry_price` 使用本次调用额度 |
| `create_spot_exit` | `amount`、`exit_type`、`price?`、`stop_loss?`、`take_profit?`、`stop_limit_price?`、`take_profit_limit_price?`：卖出归属库存或设置原生退出单，组合见下表 |
| `cancel_spot_exit` | `exit_id`：使用退出回执或库存返回的 ID，撤销该退出单或原生 OCO 组合 |
| `open_perpetual` | `side: LONG / SHORT`、`amount`、`entry_price`、`stop_loss?`、`take_profit?`：一笔线性永续限价入场；配置的退出模式可能要求 TP/SL |
| `adopt_perpetual_position` | `pos_side: LONG / SHORT`、`expected_amount`：在用户明确委托管理手动仓后，将已核验的当前整方向仓位接管到独立退出配置；`expected_amount` 必须大于零并匹配实时基础币数量。只登记管理权，不开仓、平仓或挂保护单 |
| `close_perpetual` | `pos_side: LONG / SHORT`、`amount`、`exit_type: market / take_profit_limit / stop_market`、`price?`、`trigger_price?`：按数量减仓；仅此工具允许 `amount: 0` 明确关闭整个归属方向。市价不带价格，限价退出需 `price`，止损市价需 `trigger_price`；条件退出要求独立退出模式 |
| `amend_perpetual_entry` | `order_id`、`entry_price?`、`amount?`、`pos_side?`：至少修改价格或数量之一；新 `amount` 是包含已成交部分的订单总数量 |
| `update_perpetual_protection` | `pos_side: LONG / SHORT`、`stop_loss?`、`take_profit?`：至少提供一项，未提供的保护值保留；不适用于独立退出模式 |
| `cancel_order` | `order_id`：撤销本配置拥有的普通委托；现货退出单和 OCO 使用 `cancel_spot_exit` |

例如，`buy_spot` 的完整参数放在同一层，不需要 `orders` 或 `arguments`：

```json
{
  "profile_id": "spot-profile",
  "symbol": "BTC/USDT",
  "amount": 0.001,
  "entry_price": 50000,
  "reason": "执行已确认的限价买入",
  "operation_id": "buy-btc-0001"
}
```

默认 v3 流程直接在授权范围内核验管理权并自动纳管，不要求先 adopt。旧显式接管入口仍供历史客户端使用，`expected_amount` 保持其原基础币数量含义。以下仅为旧客户端接管示例，不能作为新默认交易的前置步骤：

```json
{
  "profile_id": "perpetual-profile",
  "symbol": "ETH/USDT:USDT",
  "pos_side": "SHORT",
  "expected_amount": 0.37,
  "reason": "用户明确委托本配置管理其手动建立的 0.37 ETH 空仓",
  "operation_id": "adopt-eth-short-0001"
}
```

接管仅适用于实盘线性永续的独立退出模式，并要求 `trade` 权限。首次接管要求该账户与标的没有任何活跃管理周期，且相反方向没有非零仓位；不支持将双向持仓的一侧单独接管。服务重新核验实时数量、账户归属、普通与条件委托及成交记录；已有管理周期、冲突委托、未核验操作或接管期间仓位变化时会拒绝，不会自动撤单或抢占管理权。`completed` 接管回执只表示本地管理记录已建立，不表示交易成交或止损已经生效；需要平仓或设置止盈止损时，再以独立操作调用 `close_perpetual`。

接管基线与真实开仓成交分开保存，手动开仓不会被伪造成该配置的成交、费用或完整交易周期收益。之后按接管基线和本配置真实成交核验仓位；接管后的外部增减仓、未归属成交或无法核验的数量变化会阻止继续修改，即使外部加减后净数量恰好不变。遇到失败先核对回执；结果未知时使用原 `operation_id` 查询或重试原请求，不生成新 ID 重复接管。

现货卖出前调用 `get_spot_inventory(profile_id, symbol)`。核验成功后返回 `owned_quantity`、`reserved_quantity`、`available_quantity`、现有 `exits` 和交易所 `capabilities`。`get_balance` 看见的手工资产、其他配置买入的资产及无法完整证明归属的数量不能直接卖出；账户存在无法解释的余额、冻结或尚未核验的退出结果时，库存核验或卖出会拒绝。归属按同账户的基础币核验，换一个计价币交易对不会绕过已经预留的卖出数量。

`create_spot_exit` 当前支持 Binance 与 OKX 现货原生订单，具体类型还取决于该市场的能力和账户资格：

| `exit_type` | 价格参数 |
| --- | --- |
| `market` | 不传价格，立即市价卖出指定数量 |
| `limit` | 必须传 `price` |
| `stop_loss` | 必须传触发价 `stop_loss`；可显式传执行限价 `stop_limit_price` |
| `take_profit` | 必须传触发价 `take_profit`；可显式传执行限价 `take_profit_limit_price` |
| `oco` | 必须同时传 `stop_loss`、`take_profit`；两侧可分别显式传执行限价 |

卖出止损触发价低于当前价，止盈触发价高于当前价；执行限价不能高于对应触发价。如果 `capabilities.stop_loss_market` 或 `capabilities.take_profit_market` 为假，对应执行限价必须显式提供，系统不会代填。以下 OCO 示例显式给出两侧执行限价；示例价格仅展示参数结构，调用时须根据当前市场重新确定：

```json
{
  "profile_id": "spot-profile",
  "symbol": "BTC/USDT",
  "amount": 0.001,
  "exit_type": "oco",
  "stop_loss": 45000,
  "stop_limit_price": 44900,
  "take_profit": 60000,
  "take_profit_limit_price": 60000,
  "reason": "为已归属库存设置原生止盈止损",
  "operation_id": "btc-oco-0001"
}
```

OCO 使用 Binance 原生订单列表或 OKX `cash` 条件单接口，两条互斥腿只预留一份 `amount`，不会用两张互不关联的卖单模拟。交易所未支持的类型或价格组合直接拒绝。普通限价单、触发限价单均可能不成交；提交回执不代表成交。退出回执包含 `exit_id`、`status`、`amount`、`filled`、`exchange_id` 和 `order_ids`；失败或未知结果提供清理后的 `failure_reason` 和 `automatic_retry_allowed: false`。原生条件单可能不出现在普通 `get_orders` 结果中，应使用 `get_spot_inventory` 核验退出状态。

旧的 `execute_trade(profile_id, symbol, tool_name, arguments, operation_id)` 继续可用。`get_trading_tools` 返回当前模式允许的旧工具名称和完整业务参数定义，包括现货退出工具。`arguments` 可为 JSON 对象或 JSON 字符串，也兼容有限层数的 `args` / `arguments` 包装及字符串形式的订单数组；扁平工具只兼容额外的 `args` 包装。重复的 `symbol`、`profile_id`、`config_id`、`tool_name` 或 `operation_id` 只有与网关身份一致才接受，其中 `config_id` 必须对应 `mcp:<profile_id>`，`cycle_id` 始终由网关控制。冲突身份、不同值的重复 JSON 键、未知或错层级的资金参数都会报错，不会静默丢弃金额、价格、TP/SL，也不会覆盖外层身份。

外部助手的写入授权交互由客户端提供。服务验证 Token 权限和交易约束后直接执行，不进入站内聊天的审批流程。

### 独立退出规范工具 v2

`REAL/STRATEGY + independent_exits` 推荐 `open/close/edit/cancel`。外部 MCP 每次加上 `profile_id`、`symbol`、唯一 `operation_id`；Agent 工具由运行上下文注入这些身份。所有数量均为标的币数量，非保证金、百分比或合约张数；价格与数量必须是有限正数，布尔值不能代替数量。每项必须提供 reason。

| 工具 | 业务字段 | 语义 |
|---|---|---|
| open | side, amount, price, reason | 普通限价入场；可同轮多次调用，双向须 Hedge |
| close | side, order_type, amount 或 close_all=true, price?, trigger_price?, reason | 只退出已经核验成交的持仓，六类订单见[能力矩阵](TRADING_WORKFLOW.md#批量操作与退出方式) |
| edit | order_id, remaining_amount?, price?, trigger_price?, reason | 至少一项修改；remaining_amount 为未成交目标量，不能改方向/类型/fixed-all |
| cancel | order_id, reason | 撤销归属挂单，不能当作平仓 |

以下 JSON 为可校验的工具调用示例；示例 ID 须替换为实际查询或回执返回值。先核验入场成交，再提交退出例子，不能把示例顺序当作成交证明。

```json
{"tool":"execute_trade_actions","arguments":{"profile_id":"swap","symbol":"BTC/USDT:USDT","operation_id":"entry-stages-v2","contract_version":2,"actions":[{"action":"open","side":"LONG","amount":0.4,"price":60000,"reason":"第一档"},{"action":"open","side":"LONG","amount":0.6,"price":59500,"reason":"第二档"},{"action":"open","side":"SHORT","amount":0.2,"price":62000,"reason":"Hedge 独立空仓"}]}}
```

```json
{"tool":"execute_trade_actions","arguments":{"profile_id":"swap","symbol":"BTC/USDT:USDT","operation_id":"tp-stages-v2","contract_version":2,"actions":[{"action":"close","side":"LONG","order_type":"take_profit_market","amount":0.2,"trigger_price":64000,"reason":"第一档止盈"},{"action":"close","side":"LONG","order_type":"take_profit_limit","amount":0.3,"trigger_price":66000,"price":65900,"reason":"第二档止盈"},{"action":"close","side":"LONG","order_type":"take_profit_market","close_all":true,"trigger_price":68000,"reason":"最后退出全部剩余多仓"}]}}
```

```json
{"tool":"execute_trade_actions","arguments":{"profile_id":"swap","symbol":"BTC/USDT:USDT","operation_id":"sl-stages-v2","contract_version":2,"actions":[{"action":"close","side":"LONG","order_type":"stop_market","amount":0.2,"trigger_price":58000,"reason":"第一档止损"},{"action":"close","side":"LONG","order_type":"stop_limit","amount":0.3,"trigger_price":57000,"price":56800,"reason":"第二档止损"},{"action":"close","side":"LONG","order_type":"stop_market","close_all":true,"trigger_price":56000,"reason":"最后止损全部剩余多仓"}]}}
```

```json
{"tool":"edit","arguments":{"profile_id":"swap","symbol":"BTC/USDT:USDT","operation_id":"edit-entry-v2","order_id":"regular:123","remaining_amount":0.5,"price":60100,"reason":"调整未成交入场"}}
```

```json
{"tool":"cancel","arguments":{"profile_id":"swap","symbol":"BTC/USDT:USDT","operation_id":"cancel-exit-v2","order_id":"algo:456","reason":"撤销失效退出条件"}}
```

`status` 表示操作结果，`order_status` 表示订单状态。`submitted/waiting_trigger/triggered` 均不等于 filled。`quantity_mode=all` 的计划 `amount/remaining` 可空；`resolved_amount`（解析的委托量）、实际 `filled_amount/actual_remaining` 和 `coverage_estimate` 分开报告。独立模式的 `get_orders` 合并普通与条件单的只读状态及本配置覆盖/冲突事实，外部订单明确标记归属未知，读取不会触发补单。`regular:123` 与 `algo:123` 是不同身份；旧裸 ID 只有唯一命中时才接受。

普通 LIMIT 使用 `edit_method=native`；总量 1、已成交 0.2、remaining_amount=0.5 转为交易所总量 0.7，在途成交消耗剩余目标，不重新补量。条件单使用 `cancel_replace`：持久化意图、确认撤销、再提交替代单，回执提供旧新身份，不承诺原子性。已触发父单 edit 返回 `order_already_triggered` 和当前子单引用；对实际活跃 LIMIT 子单另调 edit，可改价/剩余量但不可改触发价。cancel 父单会在核验关联后撤销子单未成交部分，无法确认关联则待核验。

fixed 档不会自动重分配。`quantity_conflict` 不等于 unknown；Agent 可继续管理。条件 all 为账户该方向触发时原生全平，市价 all 为提交前快照；all 不支持任一种 LIMIT。挂单覆盖仅是估计，不是止损成交保证。

新旧映射：旧 open 的 `entry_price` 对应 price，旧 close 的 `pos_side/exit_type` 对应 side/order_type；旧 `take_profit_limit` 永远保持普通 LIMIT，旧 amount=0 保持首次仓位快照。旧 entry 修改 amount 是含已成交的总量，旧 exit 修改 amount 是剩余量；新 edit 统一使用 remaining_amount。无版本批次保留旧嵌套 order；v2 必须显式版本且平铺，不接受混搭。附带模式及现货仍用原工具语义，adopt 本次未扩展。

`execute_trade` 是兼容分发入口，不能绕过同样的模式、权限、归属及额度检查。`execute_trade_actions` 顺序执行，全部字段/模式/权限先校验；默认失败、pending、unknown 后停止。v2 可显式填写 `execution_policy: "independent"`，无依赖项继续逐笔准入；仅此策略支持 `depends_on: [前项索引]`，前项必须 completed，submitted 不满足依赖。省略策略不改变旧请求哈希，旧未执行项不重放。edit 需要 trade（含内部必要撤换），显式 cancel 需要 cancel；混合批次两种权限在首笔前检查。入场 edit 仍按已归属目标及修改后的实际敞口检查增仓额度。

独立模式规范工具自行取证，无需先查询或手工解锁。明确拒绝的旧单不再连带阻塞新单；真实相关未知只限制依赖它的动作。`get_trading_tools` 声明 admission_version=1、scoped_admission、query_required_before_action=false。后台证据中的 query.status 表示取证过程，status 仍表示原交易结果；每个 blocker 分别映射到调用者可访问的原身份，不输出操作查询指导。完整字段和可校验示例见[回执与恢复](TRADE_RECEIPTS_AND_RECOVERY.md)。

## 5. 执行与恢复规则

- 标的范围字段为 `symbol_scope: "selected" | "all"`，省略按 `selected` 处理。指定模式必须提供非空 `symbols`；不限制模式可保存 `symbols: []`。支持现货及线性永续合约，合约目录会排除反向合约。每笔请求仍必须传入有效的 `symbol`；账户权限、现货/合约类型、杠杆、订单归属和幂等检查保持生效。
- 现货每次调用最多涉及 10 个同计价币标的，`spot_allowance` 按该次调用的计价币计算；不限制模式可在不同调用中使用不同计价币，不会把不同币种的额度混算。从不限制改为指定标的时仍检查尚未结束的交易记录，不能排除仍需维护的标的。
- MCP 合约杠杆上限默认 5 倍，可在连接配置修改。开仓、加仓和入场改单先读取交易所实际杠杆及已有仓位杠杆；不可确认或超过上限时拒绝。它不会替你修改交易所杠杆，也不改变内部 Agent 的杠杆策略。
- 默认 `visible_first_claim` 自动管理授权可见且未被其他管理者占用的仓位与挂单，人工来源不要求模型先 adopt。已纳管仓位即使当前杠杆超过上限仍可合法减仓；新开仓、加仓和增加入场承诺量继续核验实际杠杆。真实证据缺项只限制依赖它的具体动作。旧显式 `strict_owned` 请求保留原归属/接管规则，旧请求 ID 与历史成交不会因管理者变化重算。
- 批次在首项影响前校验所有项的结构、模式能力、目标范围及权限；余额、数量、杠杆和库存承诺量在轮到该项时动态核验。后项动态失败不提前阻止合法前置 cancel。既有委托在配置停用后仍由后台维护，停用不会自动撤销交易所订单。
- 现货买入通过每次调用的组合共用额度周期核算，批次内所有标的共享 `spot_allowance`；同一请求重试不会新增额度。修改额度不会扩大已建立周期的上限。现货退出按已经核实的归属库存预留数量，不创建买入额度；未确认的提交或撤销仍占用预留，直到后台查证交易所终态。
- 所有扁平写入工具和旧 `execute_trade` 共用持久操作账本。`operation_id` 为 8–160 位字母、数字、点、下划线、冒号或连字符，必须由调用者提供。同一调用者对同一配置重试相同 ID/内容返回原回执；更换参数或调用者会被拒绝。等价的扁平调用与旧工具调用也共用回执。不同交易意图使用新 ID，客户端不应在超时后自动生成新 ID 重试。
- 超时或无法确认的结果保持 `unknown`，不会自动重放。系统后台核验原身份，用户通过授权 HTTP/UI 按需读取完整证据；`get_account_state` 与 `get_trade_records` 读取当前数量/订单/成交。网关 `submitted` 表示已受理，不能当作成交；退出回执内部 `open` 同样只是待执行。查询不重发、不撤改单、不续跑尾项。
- 持久管理权随任务/profile 保留，暂停与历史 DONE 不释放 owner。删除、缩小授权范围或换账户前须明确核验释放，或由接收任务接受交接；活跃资源和未决提交不能静默退休。已明确退休的管理范围可保留历史再退出配置，旧显式 strict 的历史保护约束继续兼容；普通名称与同值明文保存不受影响。

## 6. 导出、诊断与验证

全量配置导出包含 MCP 设置、连接配置和 API Key。包含密钥的导出在导入目标机器时重新加密；不包含密钥的导出不携带 MCP Key、后台密码、JWT 密钥和配置主密钥。导入的账户引用与 MCP 设置在同一个 SQLite 事务校验、提交，失败不会部分覆盖配置。OAuth 授权绑定原 issuer，不随配置包迁移，目标环境重新授权。配置包不包含交易审计和历史回执，完整历史使用数据库备份。

未认证访问 `/mcp` 返回 401 和资源发现地址；421 通常表示公开地址、反代 Host 不一致；403 或工具权限错误先检查 scope、配置授权和启用状态。后台显示交易拒绝原因、调用者、operation ID 和回执。交易所/HTTP 原始错误与结果会经过凭证清理，不向外部助手泄露账户密钥。

本地自动验证使用 FastAPI TestClient、官方 SDK 协议处理器和 fake exchange，覆盖初始化、工具清单、OAuth PKCE/一次性授权码/刷新轮换/撤销、权限隔离、持久幂等、实际杠杆、持仓归属和配置回滚。运行：

```bash
uv run pytest -q tests/test_mcp_gateway.py tests/test_mcp_tool_contracts.py tests/test_mcp_symbol_scope.py tests/test_mcp_spot_exits.py tests/test_position_protection.py tests/test_independent_exits.py tests/test_config_store.py
```

测试不调用真实交易所、不提交真实交易。实际公网域名和第三方工作区连接需要部署后由账户持有者完成授权联调。
# 原操作回执恢复

所有模式的后台核验与同 ID 重放使用原身份关联证据，不重复金融提交。
只读审计保留 principal/profile 隔离，不 create/edit/cancel。后台自动核验原回执；`get_orders` 在已显式授权的独占范围内可记录本地纳管事实，不会取消或修改交易所订单。
已成交 close 可同时带 `cleanup.status=pending`；具体 blocker 随摘要返回，后台自动推进；原始详情可在授权 HTTP/UI 展开。
旧批次 `not_executed` 尾部不会因查询、后台对账或重放而执行，后续交易需要明确的新请求。
首次回执及后续修订分别保存。完整字段和升级边界见 [交易回执与恢复](TRADE_RECEIPTS_AND_RECOVERY.md)。

## 人工订单的原工具接续管理

默认 visible_first_claim 在可交易范围内的有效金融动作中完成有界取证及自动纳管，无需有限手填清单或预先 adopt。旧单项入口仍保留兼容输入。查询行显示原身份、来源、管理者、支持动作和观察缺项；普通与条件身份始终分别核验。未支持类型、未知方向和缺失通道返回具体缺项，不把外部来源当作全局拒绝理由。

例如在 `ETH/USDT:USDT/LONG` 独占范围核验完成后，对人工未成交入场单改价：

```json
{"tool":"edit","arguments":{"profile_id":"swap","symbol":"ETH/USDT:USDT","order_id":"regular:123","price":2390,"reason":"关键支撑变化，更新入场价","operation_id":"manual-entry-edit-0001"}}
```

省略数量保持刚核验的原单剩余量；显式 `remaining_amount` 使用标的币数量，不是含已成交部分的总量。人工减仓不会自动重分配固定退出档位。固定减仓数量超过最新仓位时返回冲突；市价 `close_all` 使用提交前仓位，条件 `close_all` 保持交易所原生动态全平含义。

已纳管回执的数量以标的币计，`amount_units=base`。未纳管的原生列表保留合约数量并注明 `amount_units=contracts`，同时提供 `contract_size` 和 `base_amount/base_filled_amount/base_remaining_amount`。动态 all 的未解析委托数量为空，不能把交易所占位 0 当成零仓位。

人工撤单更新同一订单的终态。人工全平后由后台核验并清理旧周期挂单；全平后重新开仓进入新周期，旧 all 单列入旧单清理队列，不能算作新仓保护。发现和只读查询不执行清理写入。无法证明成交顺序的同毫秒边界保持待核验，当前已核验仓位仍可按减仓规则管理。

超时后系统按原操作 ID 自动核验证据，同 ID 回放不会再次提交。后台恢复原 Agent client ID 关联，交易所已受理但响应丢失的单不会作为第二笔人工订单登记。历史拒绝、批次未执行尾项及无关 unavailable 查询不要求 Agent 执行“解锁”；真实未知动作继续依原身份及依赖核验。独占授权不会增加 MCP scope，edit 仍需 trade，cancel 仍需 cancel，绑定聊天仍须原审批。

支持范围、启停与人工操作示例见 [独占仓位管理](exclusive-position-management.md)。


## 独占管理授权与配置生命周期

本节说明旧显式 `exclusive_scope` 配置的兼容映射；默认 `visible_first_claim` 使用前文的动态可见范围，不要求下面的额外清单。已绑定真实资源的旧 owner 会迁移并持续保留。

默认 `position_management_policy=visible_first_claim` 从已授权交易账户和标的产生管理资格，并在实际资源或有效交易意图上原子认领。all profile 不预占所有市场；只读查询不取得管理权。旧 strict/adopt 和有限 exclusive_scope 格式仅保留兼容解释，旧有效管理者优先保留，人工来源和缺少本地周期不单独成为拒绝原因。以下有限清单是旧显式策略的导入示例。

```json
{"position_management_policy":"exclusive_scope","management_scopes":[{"symbol":"ETH/USDT:USDT","side":"LONG"}]}
```

配置名、API Key principal 与管理者不同：同一 MCP profile 的授权调用共享 `mcp:<profile_id>` 管理者，任务使用 config_id。同一账户、品种及方向只能有一个管理者；Hedge 可分别管理方向，One-way 共用 BOTH。同凭证别名识别为同一范围；同一物理账户的多 Key 可在保存账户中明确确认相同 `physical_account_id`，身份同时包含规范化 venue，不能跨交易所误合并。不得通过别名、换 Key 或换调用 ID 绕过冲突。

开启后，启动已有仓位、普通/条件挂单及后续外部加减仓和订单变化由后台及执行层核验、记录和持续纳管。纳管只是本地授权与事实关联，不会自动撤销人工订单，也不伪造成 Agent 的入场成交。经核验且类型受支持的外部订单可由现有 edit/cancel 工具管理；仍遵守 profile/principal 授权、read/trade/cancel scopes、数量单位、真实 close-only 能力与原操作幂等。

停用 `enabled=false` 暂停决策，保留管理权与维护。已绑定资源的旧 scope 同样需要明确释放或接受交接，`running`/`unresolved` 不靠超时退休。仅从未绑定资源的旧配置纯预留保留旧显式退出规则。新策略已明确退休的范围可退出配置并保留原历史/actor/身份；旧 strict 的历史删除保护继续兼容。后台只读核验原未决提交，不重发或续跑尾项。

任务接口的旧客户端省略或传 null 的管理字段会保留原策略；MCP profile旧客户端省略管理字段时也保留原策略；显式 strict 并清空 scopes 才表示退出。配置保存、MCP profile 保存及完整导入使用同一事务提交授权和范围，冲突或校验失败不会部分清空订单/原提交或释放旧 owner。多个进程同时申请同一范围时，只有不冲突的配置能提交。导出包含管理字段，不包含密钥的导出不替换已有密钥；不含完整交易历史的配置包不能替代数据库备份。

## QQBot Paper 模拟交易

独立 Paper Key、按外部用户 ID 的账户、现货/合约模拟交易、可调杠杆与银行负债已支持。接入步骤、工具字段和后台 API 见 [QQBot Paper MCP](QQBOT_PAPER_MCP.md)。
