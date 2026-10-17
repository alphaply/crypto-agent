# Technical Design: 丰富 MCP 行情与指标返回

## Context

在现有架构中：
- MCP 核心工具由 `backend/mcp/canonical_tools.py` 注册，`get_market` 为唯一的行情查询工具。
- Live 实盘模式通过 `backend/mcp/service.py` 的 `query(caller, 'market', ...)` 处理，内部拥有 `MarketTool` 实例。`MarketTool` 在 `backend/utils/market_data.py` 中已经具备了完整的多周期指标计算（`get_market_analysis` / `process_timeframe`）与衍生品数据拉取（`_fetch_market_derivatives`，包含资金费率、持仓量、多空比等）。但此前 `service.py` 仅拉取了 `ticker` 与 `candles`。
- Paper 模拟盘模式通过 `backend/mcp/paper_market.py` 的 `resolve_quote` / `quote` 处理，使用 Binance 公开客户端获取价格与 K 线，此前未集成指标计算与衍生品数据。

## Goals / Non-Goals

**Goals:**
- 在 `get_market` 中同时支持返回 `ticker`（价格盘口）、`candles`（K线）、`analysis`（多周期技术指标）与 `sentiment`（衍生品多空与费率数据）。
- 默认一次性返回全量数据，满足外部客户端深度决策需求；同时提供 `include` 可选参数供按需裁剪。
- 指标周期默认与 Agent 一致计算 `['1h', '4h', '1d']`；若调用方指定了单周期（如 `15m`），自动并入计算列表（如 `['15m', '1h', '4h', '1d']`）。
- Live 实盘与 Paper 模拟盘统一支持上述字段（币安合约的资金费率、持仓量、多空比均有免密公有接口）。
- 衍生品数据失败时优雅降级（标记 `missing_sources` 和 `status='partial'`），不影响基础行情与指标返回。

**Non-Goals:**
- 不增加额外独立工具名称（保持 MCP 7 个核心工具的精简目录契约）。
- 不对现货（Spot）虚构合约专有的资金费率或多空比（现货返回空字典 `{}`）。
- 不改变 Agent 内部决策图与其它 MCP 交易工具的现有逻辑。

## Decisions

### 1. 扩展现有 `get_market` 而非新增工具
- **选择**: 在 `canonical_tools.py` 的 `get_market` 工具中直接增加返回结构，并新增可选入参 `include: list[Literal['ticker', 'candles', 'analysis', 'sentiment']] | None = None`。
- **理由**: 外部大模型或 MCP 客户端无需在下单前先调行情工具再调分析工具，减少网络往返与上下文复杂度，同时维持 `model-tool-catalog` 规定的核心工具集合（7个核心工具）不变。
- **替代方案**: 新增 `get_market_analysis` 工具。缺点是增加工具数量，客户端需要做两次查询，且需修改既有工具注册上限。

### 2. 默认全量返回与按需 include 裁剪
- **选择**: `include=None` 时默认返回 `['ticker', 'candles', 'analysis', 'sentiment']`。当调用方显式传递 `include` 时，仅保留包含在列表中的字段。
- **理由**: 满足用户明确要求（默认丰富输出），同时为对响应体积敏感的场景保留灵活裁剪能力。

### 3. 多周期与调用周期联动机制
- **选择**: 基准多周期集合为 `['1h', '4h', '1d']`。若调用方传入的 `timeframe` 不在该基准中（例如 `15m`），则自动扩展为 `[timeframe, '1h', '4h', '1d']` 进行批量指标计算。
- **理由**: 保证无论调用方关注何种细节周期，宏观多级别趋势指标始终完整，且完全复用 Agent 成熟的 `get_market_analysis` 计算逻辑。

### 4. Live 与 Paper 统一使用公网能力获取情绪与指标
- **选择**:
  - Live 模式直接调用 `MarketTool.get_market_analysis` 与 `_fetch_market_derivatives`。
  - Paper 模式在 Binance USDM 合约上，利用公网开放接口（公开的 funding rate 与 FAPI 大户/全网多空比接口免鉴权）获取真实衍生品情绪数据，并基于拉取的真实 K 线利用 `process_timeframe` 计算指标。
- **理由**: 模拟盘能拥有与实盘完全一致的丰富技术面与情绪面决策上下文。

## Risks / Trade-offs

- **[Risk] 多周期 K 线与衍生品情绪拉取增加接口耗时**
  - *Mitigation*: 衍生品情绪接口与 K 线数据在 `service.py` 与 `exchange_runtime` 中已有短期缓存机制；`include` 参数允许调用方在简单查询时排除耗时项；多周期计算在本地内存 DataFrame 完成，耗时通常在 200~500ms 内。
- **[Risk] 交易所衍生品接口（如币安情绪接口）偶尔网络抖动或超时**
  - *Mitigation*: 在衍生品获取失败时捕获异常，将 `sentiment` 记录错误信息，将 `'sentiment'` 追加至 `missing_sources` 并设置 `status='partial'`，确保基础价格和已有指标照常返回。
