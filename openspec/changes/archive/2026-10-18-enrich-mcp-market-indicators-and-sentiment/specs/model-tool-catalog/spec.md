# Spec Delta: model-tool-catalog

## MODIFIED Requirements

### Requirement: Symbol and market queries preserve venue-specific capabilities
search_symbols 与 get_market SHALL 根据可信执行上下文使用相应标的发现和行情能力。Paper SHALL 仅使用 Binance 已核验且符合既有市场能力的公开行情，不跨场所搜索或 fallback；别名 SHALL 不代替上市核验。live SHALL 保留 profile 标的授权。发现失败或部分结果 SHALL 不被表达为标的必然不存在，返回的交易所与规范标的 SHALL 可用于后续同范围动作。get_market SHALL 默认返回基础行情（ticker、candles）、多周期技术指标（analysis，默认含 1h、4h、1d）及衍生品情绪数据（sentiment，含资金费率与多空比），并支持通过 include 参数裁剪返回；当传入特定 timeframe 时，该周期 SHALL 自动并入 analysis 的计算列表。现货市场情绪字段为空。

#### Scenario: Paper searches an unfamiliar stock token
- **WHEN** Paper 客户端搜索股票代币或股票永续简称
- **THEN** 仅返回 Binance 目录核验且受现有能力支持的 symbol、exchange 和资产类型；未核验名称不触发其他场所搜索，目录失败明确标识，报价不被描述为底层股票交易所现货价格

#### Scenario: Live client searches outside its selected symbols
- **WHEN** live profile 使用 selected 标的范围并执行搜索
- **THEN** 搜索结果限制在获授权规范标的内，不通过公共发现结果扩大该 profile 的下单范围

#### Scenario: Paper explicitly selects another exchange
- **WHEN** Paper 客户端为 search_symbols 或 get_market 指定非 BN 场所
- **THEN** 返回明确场所范围错误，不连接该交易所，不把搜索空结果或 BN 失败作为跨场所回退依据

#### Scenario: Client queries market data with default fields
- **WHEN** 客户端调用 get_market 未指定 include 参数
- **THEN** 返回数据中同时包含 ticker、candles、包含 1h/4h/1d 的多周期技术指标 analysis 以及包含资金费率与多空比的 sentiment

#### Scenario: Client specifies custom timeframe for market query
- **WHEN** 客户端调用 get_market 传入 timeframe='15m'
- **THEN** 主蜡烛图 candles 返回 15m 周期，且 analysis 自动包含 15m、1h、4h、1d 的技术指标计算结果

#### Scenario: Client prunes returned facets via include
- **WHEN** 客户端调用 get_market 显式传入 include=['ticker', 'sentiment']
- **THEN** 返回结果仅包含 ticker 与 sentiment 字段，排除未请求的 candles 与 analysis

#### Scenario: Spot market query requests sentiment
- **WHEN** 客户端对现货标的（如 BTC/USDT spot）调用 get_market
- **THEN** analysis 正常计算现货指标，sentiment 保持为空字典，不返回伪造的合约资金费率或多空比
