# Proposal: 丰富 MCP 行情与指标返回（支持技术指标、资金费率与多空比）

## Why

目前内置 Agent 在分析行情与做出决策时，具备丰富的多周期技术指标（EMA 均线、MACD、RSI、布林带、ATR、VWAP 等）以及衍生品情绪数据（资金费率、全网持仓量 OI、大户与全网多空人数/持仓比、主动买卖比）。然而，MCP 客户端调用的 `get_market` 工具当前仅返回实时的 ticker 报价与裸 K 线（candles），缺失了上述深度指标与情绪数据。这导致外部通过 MCP 对接的大模型或客户端无法获取完整的技术面与情绪面信息，决策依据不足。

本次改动在 MCP 的 `get_market` 工具中直接补齐技术指标与衍生品情绪数据，统一实盘（Live）与模拟盘（Paper）的行情与指标分析能力，使用户与外部客户端在不增加额外工具调用次数的前提下，一次性获取完整、丰富的市场分析数据。

## What Changes

- **扩展 MCP `get_market` 返回契约**：
  - 默认同时返回基础行情（`ticker`、`candles`）、多周期技术指标（`analysis`）及衍生品情绪数据（`sentiment`）。
  - 支持可选参数 `include: list[Literal['ticker', 'candles', 'analysis', 'sentiment']] | None`，允许调用方按需裁剪返回字段。
- **技术指标多周期对齐与协调**：
  - `analysis` 默认计算与 Agent 一致的标准周期 `['1h', '4h', '1d']` 的技术指标（包含 EMA、RSI、MACD、布林带、ATR、VWAP 等）。
  - 当调用方显式传入特定 `timeframe`（例如 `15m`）时，系统自动将该周期并入 `analysis` 的指标计算中，兼顾局部微观分析与宏观趋势。
- **衍生品情绪数据统一覆盖**：
  - 合约市场（Swap）统一返回资金费率（`funding_rate`）、全网持仓量（`open_interest`）、24h 成交额（`24h_quote_vol`）以及多空比指标（`ls_ratio`、`ls_accounts`、`top_ls_accounts`、`taker_buy_sell_ratio` 等）。
  - 现货市场（Spot）在情绪字段保持规范，不返回虚假或占位费率（现货无资金费率与多空比）。
- **实盘（Live）与模拟盘（Paper）能力统一**：
  - 实盘与模拟盘在 MCP 行情获取上保持一致标准，均提供完整技术指标与公开衍生品情绪数据。
- **容错与部分降级弹性**：
  - 当衍生品情绪接口偶发超时或异常时，将其计入 `missing_sources` 并标记 `status='partial'`，不阻断基础行情与技术指标的正常返回。

## Capabilities

### Modified Capabilities
- `model-tool-catalog`: 扩展 `get_market` 工具的数据返回结构，增加 `analysis`（多周期技术指标）与 `sentiment`（资金费率、持仓量、多空比）字段，支持可选的 `include` 裁剪参数，并在 Live 与 Paper 模式下保持统一的行情分析能力。

## Impact

- **Affected Code**:
  - `backend/mcp/canonical_tools.py`: 更新 `get_market` 函数签名、参数及文档，注入 `analysis` 和 `sentiment` 的处理逻辑与 `include` 过滤。
  - `backend/mcp/service.py`: 在 `service.query(caller, 'market', ...)` 中集成 `MarketTool` 的指标计算与衍生品情绪拉取。
  - `backend/mcp/paper_market.py`: 在 Paper 模式中复用公开行情源完成指标计算与公开情绪数据拉取。
- **Affected Tests**:
  - `tests/test_mcp_canonical_catalog.py`
  - `tests/test_mcp_gateway.py`
  - 新增/更新针对 `get_market` 返回指标及资金费率/多空比的契约单元测试。
