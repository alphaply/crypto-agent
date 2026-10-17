# Tasks: 丰富 MCP 行情与指标返回

## 1. Live 实盘模式的指标与情绪数据集成

- [x] 1.1 在 `backend/mcp/service.py` 的 `query(caller, 'market', ...)` 中集成 `MarketTool` 的多周期分析与衍生品数据拉取，支持默认计算 `['1h', '4h', '1d']` 并自动并入传入的 `timeframe`，返回 `analysis` 与 `sentiment`。验证离线 mock 测试通过。
- [x] 1.2 在 `backend/mcp/service.py` 中确保现货（Spot）标的 `sentiment` 为空字典，且当衍生品接口异常或超时时优雅降级（记入 `missing_sources` 并标记 `status='partial'`）。验证现货与容错逻辑测试通过。

## 2. Paper 模拟盘模式的指标与公开情绪数据集成

- [x] 2.1 在 `backend/mcp/paper_market.py` 中基于拉取的公网 K 线计算多周期技术指标（包含 1h/4h/1d 及自定义周期），填充 `analysis` 字段。验证 Paper 模式指标计算测试通过。
- [x] 2.2 在 `backend/mcp/paper_market.py` 中为 Binance 合约标的调用公网无需密钥的资金费率、持仓量及多空比接口，填充 `sentiment` 字段，并在现货标的上保持为空。验证 Paper 情绪数据拉取测试通过。

## 3. Canonical MCP 工具接口与参数裁剪

- [x] 3.1 在 `backend/mcp/canonical_tools.py` 中扩展 `get_market` 工具参数，新增可选的 `include`（支持 `ticker`, `candles`, `analysis`, `sentiment`），默认全量返回，并实现按 `include` 字段裁剪。验证 MCP 工具 inputSchema 与分发契约。
- [x] 3.2 确保 `get_market` 的核心工具名称保持在 7 个核心工具内，工具说明准确反映返回的指标与情绪数据。验证 `catalog_names` 与工具列表断言一致。

## 4. 全面测试与系统验证

- [x] 4.1 编写并执行针对 `get_market` 的单元测试与网关集成测试（覆盖默认全量返回、`include` 过滤、周期合并、现货空情绪、Live 与 Paper 模式一致性），验证 `pytest tests/test_mcp*.py` 相关测试通过。
- [x] 4.2 运行 `uv run backend/utils/test_agent_connection.py`（若环境支持）及前端构建 `npm run build --prefix frontend`，确保无回归。
