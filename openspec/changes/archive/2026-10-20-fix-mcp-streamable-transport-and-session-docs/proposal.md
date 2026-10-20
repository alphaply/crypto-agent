# Proposal

## Why

外部接入客户端（如 AstrBot / QQBot）在容器重启更新或长时间连接断开后，内部会因为连接断开进入重连失败并清空 session。当 AstrBot 自身的 session 变为 None 时，其内部函数工具调用会直接抛出本地异常 `ValueError: MCP session is not available for MCP function tools`，导致每小时定时巡检无法获取余额与订单信息，进而触发模型人格提示“缺少可核验的订单身份时保留未确认状态，不用其他订单或当前余额猜测原操作结果”并汇报“MU @893 那张单状态未知，我不猜。八小时了，等是不会好的。要么重启一次 AstrBot，要么看下交易 MCP 的连接配置”。

同时，AstrBot 在配置缺少 transport 字段时要求显式指定 transport 类型，否则会报错缺少 transport 或回退到不兼容的 SSE。我们需要在连接诊断配置及文档中显式声明 `"transport": "streamable_http"`，修正测试中的 session 计数断言，验证 MCP 接口与工具调用的完整可用性，并发布包含更新的 Docker 镜像。

## What Changes

- **显式声明 streamable_http 传输类型**：在 `backend/mcp/connection.py` 的 connection_details 配置示例及 `docs/QQBOT_PAPER_MCP.md` 中，显式添加 `"transport": "streamable_http"`，确保外部客户端准确使用 Streamable HTTP 协议接入。
- **校准单元测试断言**：在 `tests/test_mcp_paper.py` 中更新多重行情指标与情绪查询后的 session 关闭计数，确保所有 MCP 单元测试 100% 通过。
- **构建与发布**：完成前端构建、完整单元测试验证后归档变更，推送到 `beta` 分支并执行 Docker 发布脚本。

## Capabilities

### New Capabilities

无。本次为连接配置文档与测试校准。

### Modified Capabilities

无。MCP 协议契约与工具保持兼容。

## Impact

- 后端与文档：`backend/mcp/connection.py`、`docs/QQBOT_PAPER_MCP.md`、`tests/test_mcp_paper.py`。
- 用户影响：客户端配置明确传输模式，重启 AstrBot 后即可顺畅重连并恢复交易巡检与下单。
