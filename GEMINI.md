# Crypto Agent (自动亏钱 Agent)

An automated cryptocurrency trading system powered by Large Language Models (LLMs) and LangGraph. It manages multiple trading agents across different symbols, timeframes, and strategies with a focus on risk management and multi-agent collaboration.

## Project Overview

- **Purpose**: Automate crypto trading strategies (Real-time, Strategy/Simulation, and Spot DCA) using AI-driven decision-making.
- **Key Features**:
  - **Multi-Agent Architecture**: Independent configuration per trading pair (Model, Leverage, Prompts).
  - **Trading Modes**: 
    - `REAL`: 15m interval scanning with direct exchange execution (Futures).
    - `STRATEGY`: 1h interval simulation for high R/R ratio testing.
    - `SPOT_DCA`: Spot market Dollar Cost Averaging.
  - **Dashboard**: Flask-based web interface for monitoring, config management, and token usage statistics.
  - **Interactive Chat**: Directly talk to agents for analysis or manual trade approval.
- **Tech Stack**:
  - **Core**: Python 3.10+, [uv](https://github.com/astral-sh/uv) (Package Manager).
  - **AI Framework**: LangGraph (Workflow Orchestration), LangChain (LLM interaction).
  - **Exchange**: [ccxt](https://github.com/ccxt/ccxt) (Binance USDM & Spot).
  - **Web Framework**: Flask (API & Dashboard).
  - **Data/Math**: Pandas, Numpy, SQLite.

## Architecture

- **`dashboard.py`**: Main entry point for the Flask web server and scheduler launcher.
- **`main_scheduler.py`**: Background task engine that triggers agent analysis cycles.
- **`agent/`**: 
  - `agent_graph.py`: The LangGraph state machine (Start -> Agent -> Tools -> Finalize).
  - `agent_tools.py`: Tools used by agents (Opening/Closing positions, canceling orders).
  - `prompts/`: System prompt templates for different trading modes.
- **`utils/`**:
  - `market_data.py`: High-level wrapper for fetching OHLCV and technical indicators.
  - `indicators.py`: Implementation of TA indicators (EMA, RSI, MACD, Volume Profile, etc.).
  - `formatters.py`: Data-to-text conversion for LLM consumption.
- **`routes/`**: Modular Flask routes for authentication, stats, config, and chat.

## Development Standards & Conventions

### Indicator Calculation
- **Wilder's Smoothing**: Standard TA indicators (RSI, ADX, ATR) must use Wilder's Smoothing (RMA) to align with professional charting tools like TradingView.
- **Data Warm-up**: Always fetch at least 1000 candles for reliable calculation of long-term indicators like EMA200.
- **Safety**: Functions in `utils/indicators.py` should handle `NaN` and `Inf` values gracefully using `smart_fmt` or `fillna`.

### Agent & Prompts
- **Context Injection**: Market data is injected via the `{formatted_market_data}` placeholder in prompts.
- **Tool Mapping**: Tools must be bound correctly based on the `trade_mode` (REAL vs STRATEGY).
- **History**: Use `database.get_recent_summaries` to provide agents with memory of previous logic to avoid anchoring bias while maintaining continuity.

### Security
- **API Keys**: Never hardcode credentials. Use `.env` and the `Config` class in `config.py`.
- **Mode Isolation**: Strictly separate `REAL` and `STRATEGY` execution paths to prevent accidental capital loss.

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
| `config.py` | Centralized configuration and credential management. |
| `database.py` | SQLite persistence for snapshots, summaries, and token usage. |
| `agent/agent_graph.py` | Core LangGraph logic defining the agent's behavior. |
| `utils/market_data.py` | Primary interface for all market and account data fetching. |
| `utils/indicators.py` | Professional-grade TA indicator implementations. |
| `routes/chat.py` | Real-time agent interaction via Server-Sent Events (SSE). |

---
*Note: This GEMINI.md is generated for Gemini CLI to provide foundational context for this project.*
