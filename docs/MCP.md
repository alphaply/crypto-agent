# MCP 外部助手接入

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

## 4. 权限与工具

| 权限 | 能力 |
| --- | --- |
| `read` | 查看共享消息、已授权配置、可用标的、行情、余额、持仓、委托、现货归属库存、交易参数定义及本调用者回执 |
| `trade` | 现货限价买入、归属库存卖出及原生止盈/止损/OCO；合约开仓、加仓、减仓、平仓、改单和保护设置 |
| `cancel` | 撤销此 MCP 配置拥有的委托及现货退出单 |

所有令牌都包含 `read`。交易或撤单必须明确绑定至少一个 MCP 配置；没有配置授权的查询令牌只能读取共享消息等公共 MCP 信息。含交易及撤单的批次同时要求 `trade` 和 `cancel`。

查询工具为 `list_profiles`、`list_symbols`、`get_news`、`get_market`、`get_balance`、`get_positions`、`get_orders`、`get_spot_inventory`、`get_trading_tools` 和 `get_operation`。`get_news` 读取全局缓存摘要及新鲜度，不触发抓取或模型计费。余额是账户余额，同一账户的多个策略不能相加；账户余额也不等于该 MCP 配置有权卖出的库存。

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

## 5. 执行与恢复规则

- 标的范围字段为 `symbol_scope: "selected" | "all"`，省略按 `selected` 处理。指定模式必须提供非空 `symbols`；不限制模式可保存 `symbols: []`。支持现货及线性永续合约，合约目录会排除反向合约。每笔请求仍必须传入有效的 `symbol`；账户权限、现货/合约类型、杠杆、订单归属和幂等检查保持生效。
- 现货每次调用最多涉及 10 个同计价币标的，`spot_allowance` 按该次调用的计价币计算；不限制模式可在不同调用中使用不同计价币，不会把不同币种的额度混算。从不限制改为指定标的时仍检查尚未结束的交易记录，不能排除仍需维护的标的。
- MCP 合约杠杆上限默认 5 倍，可在连接配置修改。开仓、加仓和入场改单先读取交易所实际杠杆及已有仓位杠杆；不可确认或超过上限时拒绝。它不会替你修改交易所杠杆，也不改变内部 Agent 的杠杆策略。
- 已确认由本 MCP 配置独占的仓位，即使当前杠杆超过上限也允许减仓和平仓。存在其他策略、手工增仓或不能证明归属的持仓时拒绝修改。系统按当前持仓周期的已关联订单成交量核验；历史单页超过 1000 条或数据不完整时拒绝，需先核验账户状态。
- 撤单按订单归属校验；批次在执行任何保护调整之前检查其中所有增加风险的动作。既有委托在 MCP 配置停用后仍由后台维护，停用不会自动撤销交易所订单。
- 现货买入通过每次调用的组合共用额度周期核算，批次内所有标的共享 `spot_allowance`；同一请求重试不会新增额度。修改额度不会扩大已建立周期的上限。现货退出按已经核实的归属库存预留数量，不创建买入额度；未确认的提交或撤销仍占用预留，直到后台查证交易所终态。
- 所有扁平写入工具和旧 `execute_trade` 共用持久操作账本。`operation_id` 为 8–160 位字母、数字、点、下划线、冒号或连字符，必须由调用者提供。同一调用者对同一配置重试相同 ID/内容返回原回执；更换参数或调用者会被拒绝。等价的扁平调用与旧工具调用也共用回执。不同交易意图使用新 ID，客户端不应在超时后自动生成新 ID 重试。
- 超时或无法确认的结果保持 `unknown`，不会自动重放。`get_operation(profile_id, operation_id)` 读取本调用者原始执行回执；再通过 `get_orders`、`get_spot_inventory`、持仓和交易所记录核对当前状态。网关 `submitted` 表示交易所已受理，不能当作成交；退出回执内部 `open` 同样只是待执行。委托成交或取消需后续核验。
- 配置已有交易历史后不能删除或改绑账户，可停用。未结束的持仓、挂单或待核验操作会阻止移除相关标的、切换退出模式或更换账户凭证；普通名称或同值明文保存不受影响。

## 6. 导出、诊断与验证

全量配置导出包含 MCP 设置、连接配置和 API Key。包含密钥的导出在导入目标机器时重新加密；不包含密钥的导出不携带 MCP Key、后台密码、JWT 密钥和配置主密钥。导入的账户引用与 MCP 设置在同一个 SQLite 事务校验、提交，失败不会部分覆盖配置。OAuth 授权绑定原 issuer，不随配置包迁移，目标环境重新授权。配置包不包含交易审计和历史回执，完整历史使用数据库备份。

未认证访问 `/mcp` 返回 401 和资源发现地址；421 通常表示公开地址、反代 Host 不一致；403 或工具权限错误先检查 scope、配置授权和启用状态。后台显示交易拒绝原因、调用者、operation ID 和回执。交易所/HTTP 原始错误与结果会经过凭证清理，不向外部助手泄露账户密钥。

本地自动验证使用 FastAPI TestClient、官方 SDK 协议处理器和 fake exchange，覆盖初始化、工具清单、OAuth PKCE/一次性授权码/刷新轮换/撤销、权限隔离、持久幂等、实际杠杆、持仓归属和配置回滚。运行：

```bash
uv run pytest -q tests/test_mcp_gateway.py tests/test_mcp_tool_contracts.py tests/test_mcp_symbol_scope.py tests/test_mcp_spot_exits.py tests/test_position_protection.py tests/test_independent_exits.py tests/test_config_store.py
```

测试不调用真实交易所、不提交真实交易。实际公网域名和第三方工作区连接需要部署后由账户持有者完成授权联调。
