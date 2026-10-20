# Tasks

## 1. 配置与接入明确 Streamable HTTP 协议

- [x] 1.1 在 `backend/mcp/connection.py` 中为 `configuration` 配置模板显式添加 `'transport': 'streamable_http'`。
- [x] 1.2 在 `docs/QQBOT_PAPER_MCP.md` 中为接入 JSON 添加 `"transport": "streamable_http"` 说明。

## 2. 单元测试与协议验证

- [x] 2.1 在 `tests/test_mcp_paper.py` 中更新 `test_public_market_uses_only_anonymous_methods_and_rejects_stale_quotes` 的 session 关闭计数。
- [x] 2.2 运行完整 MCP 测试套件（`test_mcp_connection.py`、`test_mcp_canonical_catalog.py`、`test_mcp_paper.py`），全部通过。
- [x] 2.3 运行前端构建 `npm run build --prefix frontend`，通过。

## 3. 归档与 Docker 发布

- [x] 3.1 归档变更到 `openspec/changes/archive/2026-10-20-fix-mcp-streamable-transport-and-session-docs`。
- [x] 3.2 提交到 `beta` 分支。
- [x] 3.3 运行 `scripts/release-archived-change.ps1` 发布 Docker 镜像并推送到远程仓库。
