# Crypto Agent (自动亏钱 Agent)

An automated cryptocurrency trading system powered by Large Language Models (LLMs) and LangGraph. It manages multiple independent trading agents (configurations) across different symbols, timeframes, and strategies with robust risk management and performance tracking.

## Project Overview

- **Purpose**: Automate diverse crypto trading strategies (Futures REAL, Strategy Simulation, and Spot DCA) using AI-driven logic.
- **Key Features**:
  - **Isolated Configuration (`config_id`)**: Supports running multiple independent agents on the same symbol (e.g., BTC/USDT 15m Scalping vs BTC/USDT 1h Swing) by isolating state and history via unique IDs.
  - **Trading Modes**: 
    - `REAL`: Professional futures execution (Long/Short) with automated Stop-Loss/Take-Profit management.
    - `STRATEGY`: High-fidelity simulation for testing risk/reward ratios.
    - `SPOT_DCA`: Flexible Spot market Dollar Cost Averaging with intelligent timing (Daily/Weekly).
  - **Enhanced Indicator System (v2)**: Optimized signal set with MACD momentum labeling, RSI divergence detection, and adaptive Volume Profile.
  - **Performance Analytics**: Real-time tracking of account equity, trade history, LLM token usage, and daily strategy summaries.
  - **Interactive Chat**: SSE-based real-time chat with specific agents for manual analysis or trade approval.

## Architecture

- **`dashboard.py`**: Main Flask web interface and entry point for the smart scheduler.
- **`main_scheduler.py`**: A 1-minute heartbeat engine that triggers agents based on individual timeframes or DCA schedules.
- **`agent/`**: 
  - `agent_graph.py`: Core LangGraph state machine (Start -> Analyze -> Trade/Wait -> Finalize).
  - `agent_tools.py`: Unified toolset for order execution and state management.
- **`utils/`**:
  - `market_data.py`: `MarketTool` wrapper for standardized OHLCV, derivatives (funding/OI), and account status.
  - `indicators.py`: Professional TA indicator implementations optimized for LLM consumption.
  - `formatters.py`: Structural text/markdown formatters that convert complex JSON data into concise prompt context.
- **`database.py`**: SQLite core for everything: orders, snapshots, token usage, and cross-session memory.

## Development Standards & Conventions

### Indicator System (v2)
- **Reduced Redundancy**: Removed overlapping indicators (StochRSI, KDJ, CCI, EMA100) to minimize token consumption and noise.
- **Momentum & Divergence**: 
  - MACD Histogram is labeled with momentum states (e.g., "多头加速", "多头减速 ⚠️").
  - RSI includes automatic top/bottom divergence detection.
- **Adaptive Volume Profile**: Lookback periods for VP (POC/VAH/VAL) are automatically adjusted per timeframe (e.g., 576 bars for 5m, 120 bars for 1d).
- **Logical Scoping**: Indicators like VWAP are strictly limited to intraday timeframes (1m-1h) to ensure statistical validity.
- **Wilder's Smoothing**: RSI/ADX/ATR use Wilder's RMA for alignment with standard charting tools.

### Agent & Prompts
- **Config Isolation**: All database queries and tool actions MUST use `config_id` to prevent data leakage between parallel agents.
- **Memory Enrichment**: 
  - Short-term: `database.get_recent_summaries` provides last ~10 analysis cycles.
  - Long-term: `database.get_daily_summaries` provides LLM-compressed summaries of previous days to maintain narrative continuity.

### Security & Precision
- **Mode Isolation**: Strictly separate `REAL`, `STRATEGY`, and `SPOT_DCA` execution paths.
- **Precision Management**: Use `exchange.amount_to_precision` and `exchange.price_to_precision` before every order to avoid API errors.

## Building and Running

### Prerequisites
```bash
# Install uv
powershell -c "irm https://astral-sh.net/uv/install.ps1 | iex" # Windows
```

### Installation
```bash
uv sync
cp .env.template .env
# Edit .env with your keys
```

### Execution
```bash
# Start Web Server + Scheduler
uv run dashboard.py
```

## Key Files Summary

| File | Description |
| :--- | :--- |
| `config.py` | Centralized config management (symbol settings, model params, credentials). |
| `database.py` | Persistent storage for snapshots, token usage, and agent memories. |
| `agent/agent_graph.py` | The "brain" logic defining how agents process data and decide. |
| `utils/market_data.py` | Primary API for fetching OHLCV, Funding Rate, OI, and Account stats. |
| `utils/indicators.py` | Professional-grade TA indicators (EMA, MACD Momentum, RSI Div, VP). |
| `utils/formatters.py` | Translates raw JSON data into high-density text for LLM prompts. |
| `routes/chat.py` | Backend for the real-time interaction system. |

---
*Note: This GEMINI.md is generated for Gemini CLI to provide foundational context for this project.*

