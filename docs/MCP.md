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
| OAuth 授权服务器发现 | `/.well-known/oauth-authorization-server/oauth` |
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

按照 [OpenAI 的自定义 MCP 接入说明](https://developers.openai.com/api/docs/guides/custom-mcp-server)，在支持该功能的 ChatGPT 工作区添加自定义 MCP 服务，Server URL 填 `https://agent.example.com/mcp`，认证选择 OAuth。服务支持动态注册，随后在本站授权页面用后台登录密码验证，选择连接配置及所需权限。该页面允许取消交易和撤单权限，仅授予查询。完成后安装/启用该连接并在聊天中调用工具。

ChatGPT 的此类连接使用用户 OAuth，不接收自定义 API Key。当前实现采用授权码、PKCE S256、精确 redirect URI 校验和资源地址校验；支持 HTTPS 或 loopback HTTP 回调。WorkBuddy 的私有协议回调未启用，使用上面的 Token 方式。客户端能力、工作区开放范围和界面入口请以 [OpenAI 认证文档](https://developers.openai.com/plugins/build/auth) 为准。

授权码有效期 2 分钟，访问令牌 1 小时，刷新令牌 30 天；刷新会轮换令牌，旧令牌失效。后台 OAuth 连接列表可撤销整次授权，客户端也可调用标准撤销端点。公开域名变更后旧域名令牌不能用于新资源，需要重新连接。

## 4. 权限与工具

| 权限 | 能力 |
| --- | --- |
| `read` | 查看共享消息、已授权配置、行情、余额、持仓、委托、交易参数定义及本调用者回执 |
| `trade` | 现货限价买入；合约开仓、加仓、减仓、平仓、改单和保护设置 |
| `cancel` | 撤销此 MCP 配置拥有的委托 |

所有令牌都包含 `read`。交易或撤单必须明确绑定至少一个 MCP 配置；没有配置授权的查询令牌只能读取共享消息等公共 MCP 信息。含交易及撤单的批次同时要求 `trade` 和 `cancel`。

查询工具为 `list_profiles`、`get_news`、`get_market`、`get_balance`、`get_positions`、`get_orders`、`get_trading_tools` 和 `get_operation`。`get_news` 读取全局缓存摘要及新鲜度，不触发抓取或模型计费。余额是账户余额，同一账户的多个策略不能相加。

写入统一通过 `execute_trade(profile_id, symbol, tool_name, arguments, operation_id)`。先调用 `get_trading_tools` 获取当前模式允许的工具及完整参数定义。现货只支持已有的买入/撤单能力；合约沿用现有开平仓、改单、保护和批量工具。网关注入交易身份，参数不能覆盖 `config_id`、顶层 `symbol`、`cycle_id` 或 `operation_id`。

外部助手的写入授权交互由客户端提供。服务验证 Token 权限和交易约束后直接执行，不进入站内聊天的审批流程。

## 5. 执行与恢复规则

- 标的范围字段为 `symbol_scope: "selected" | "all"`，省略按 `selected` 处理。指定模式必须提供非空 `symbols`；不限制模式可保存 `symbols: []`。支持现货及线性永续合约，合约目录会排除反向合约。每笔请求仍必须传入有效的 `symbol`；账户权限、现货/合约类型、杠杆、订单归属和幂等检查保持生效。
- 现货每次调用最多涉及 10 个同计价币标的，`spot_allowance` 按该次调用的计价币计算；不限制模式可在不同调用中使用不同计价币，不会把不同币种的额度混算。从不限制改为指定标的时仍检查尚未结束的交易记录，不能排除仍需维护的标的。
- MCP 合约杠杆上限默认 5 倍，可在连接配置修改。开仓、加仓和入场改单先读取交易所实际杠杆及已有仓位杠杆；不可确认或超过上限时拒绝。它不会替你修改交易所杠杆，也不改变内部 Agent 的杠杆策略。
- 已确认由本 MCP 配置独占的仓位，即使当前杠杆超过上限也允许减仓和平仓。存在其他策略、手工增仓或不能证明归属的持仓时拒绝修改。系统按当前持仓周期的已关联订单成交量核验；历史单页超过 1000 条或数据不完整时拒绝，需先核验账户状态。
- 撤单按订单归属校验；批次在执行任何保护调整之前检查其中所有增加风险的动作。既有委托在 MCP 配置停用后仍由后台维护，停用不会自动撤销交易所订单。
- 每次现货 `execute_trade` 建立一个组合共用额度周期，批次内所有标的共享 `spot_allowance`；同一请求重试不会新增额度。修改额度不会扩大已建立周期的上限。
- `operation_id` 为 8–160 位字母、数字、点、下划线、冒号或连字符。同一调用者对同一配置重试相同 ID/内容返回原回执；更换参数或调用者会被拒绝。不同交易意图使用新 ID。
- 超时结果保持 `unknown`，不会自动重放。通过 `get_operation`、`get_orders`、持仓和交易所记录核对；不要因为超时立即换 ID 重发。委托已提交不等于已成交。
- 配置已有交易历史后不能删除或改绑账户，可停用。未结束的持仓、挂单或待核验操作会阻止移除相关标的、切换退出模式或更换账户凭证；普通名称或同值明文保存不受影响。

## 6. 导出、诊断与验证

全量配置导出包含 MCP 设置、连接配置和 API Key。包含密钥的导出在导入目标机器时重新加密；不包含密钥的导出不携带 MCP Key、后台密码、JWT 密钥和配置主密钥。导入的账户引用与 MCP 设置在同一个 SQLite 事务校验、提交，失败不会部分覆盖配置。OAuth 授权绑定原 issuer，不随配置包迁移，目标环境重新授权。配置包不包含交易审计和历史回执，完整历史使用数据库备份。

未认证访问 `/mcp` 返回 401 和资源发现地址；421 通常表示公开地址、反代 Host 不一致；403 或工具权限错误先检查 scope、配置授权和启用状态。后台显示交易拒绝原因、调用者、operation ID 和回执。交易所/HTTP 原始错误与结果会经过凭证清理，不向外部助手泄露账户密钥。

本地自动验证使用 FastAPI TestClient、官方 SDK 协议处理器和 fake exchange，覆盖初始化、工具清单、OAuth PKCE/一次性授权码/刷新轮换/撤销、权限隔离、持久幂等、实际杠杆、持仓归属和配置回滚。运行：

```bash
uv run pytest -q tests/test_mcp_gateway.py tests/test_position_protection.py tests/test_independent_exits.py tests/test_config_store.py
```

测试不调用真实交易所、不提交真实交易。实际公网域名和第三方工作区连接需要部署后由账户持有者完成授权联调。
