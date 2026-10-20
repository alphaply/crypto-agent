# Design

## Context

FastMCP 采用官方 MCP SDK 的 Streamable HTTP 协议 (`/mcp`) 运行。
AstrBot 作为一个 MCP Client，其客户端实现位于 `astrbot/core/agent/mcp_client.py`：
1. 当配置缺少 `transport` 或 `type` 时，AstrBot 会报错 `MCP connection config missing transport or type field`；若配置未声明 `streamable_http`，AstrBot 会尝试使用 SSE 方式连接。
2. 当连接断开时，AstrBot 在 `_reconnect()` 中先将内部 `self.session` 置为 `None`，再调用 `connect_to_server()`。若重连过程中服务正在重启，重连抛出异常，`self.session` 将保持 `None`。
3. AstrBot 的 `@retry` 仅捕获 `anyio.ClosedResourceError`；当 `self.session` 为 `None` 时，调用函数工具直接抛出本地异常 `ValueError("MCP session is not available for MCP function tools.")`，不再重试或触发重连，导致必须重启 AstrBot。
4. 本次为确保外部对接毫无歧义，在 MCP 配置与接入说明中显式添加 `"transport": "streamable_http"`，修正测试中的 session 计数，并完成 Docker 镜像发布。

## Implementation Details

1. `backend/mcp/connection.py`：在 `connection_details` 输出的示例 `configuration` 中追加 `'transport': 'streamable_http'`。
2. `docs/QQBOT_PAPER_MCP.md`：更新接入示例 JSON 为：
   ```json
   {"mcpServers":{"crypto-agent-paper":{"url":"https://你的域名/mcp","transport":"streamable_http","headers":{"Authorization":"Bearer <Paper Key>"}}}}
   ```
3. `tests/test_mcp_paper.py`：更新 `assert seen.count('closed') == 7`。
