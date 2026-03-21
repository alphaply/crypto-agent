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
  - **Smart Router System (v3)**: 
    - **Deterministic Routing**: Uses a lightweight model (e.g., GPT-4o-mini) as an intelligent gateway. It makes a definitive `decision` via tool-calling: `MASTER` (escalate), `SMALL` (process locally), or `SKIP` (no action).
    - **Small Model Execution**: In `STRATEGY` mode, the "Small Model" is empowered to execute trading tools directly if it handles the logic, saving costs of larger models.
    - **Context Richness**: Routers receive full account context (positions, balance, orders) and multi-timeframe market indicators to ensure accurate routing.
    - **Mode Separation**: Screener logic is removed from interactive `Chat` mode to prioritize direct responses, focusing its cost-saving benefits on automated scheduled runs.
  - **Performance Analytics**: Real-time tracking of account equity, trade history, LLM token usage, and daily strategy summaries.
  - **Enhanced Indicator System (v2)**: Optimized signal set with MACD momentum labeling, RSI divergence detection, and adaptive Volume Profile.

## Architecture

- **`dashboard.py`**: Main Flask web interface and entry point for the smart scheduler.
- **`main_scheduler.py`**: A 1-minute heartbeat engine that triggers agents based on individual timeframes or DCA schedules. Features a resilient 2-hour window (00:00-02:00) for Daily Summary generation.
- **`agent/`**: 
  - `agent_graph.py`: Core LangGraph state machine (Start -> Router -> Master Agent / Small Agent / Skip -> Tools? -> Finalize).
  - `chat_graph.py`: Streamlined interactive LangGraph workflow (Start -> Agent -> Tools? -> End).
  - `agent_tools.py`: Unified toolset for order execution and state management.
- **`database.py`**: Centralized SQLite persistence. **All timestamps are normalized to Beijing Time (`Asia/Shanghai`)** to ensure cross-environment consistency.
- **`utils/`**:
  - `market_data.py`: `MarketTool` wrapper for standardized OHLCV, derivatives, and account status.
  - `indicators.py`: Professional TA indicator implementations optimized for LLM consumption.
  - `formatters.py`: Translates raw JSON data into concise, high-density prompt context.

## Development Standards & Conventions

### Router Logic (v3)
- **Tool-Driven**: Routing must be performed via `submit_screening_decision` tool call.
- **Decision Matrix**:
  - `MASTER`: High-stakes or complex scenarios.
  - `SMALL`: Routine maintenance or simple logic (Trades allowed in `STRATEGY` mode).
  - `SKIP`: Market noise, no further processing needed.

### UI & Display
- **Analysis Source**: The dashboard explicitly labels whether a result came from "🔍 初筛模型" (Small Agent) or "🧠 决策模型" (Master Agent).
- **Time Consistency**: All UI elements display and filter data based on the unified Beijing time zone defined in `database.py`.

### Data Persistence
- **Mode Isolation**: Strictly separate `REAL`, `STRATEGY`, and `SPOT_DCA` execution paths.
- **Agent Type**: Records are tagged with `agent_type` (e.g., `SCREENER`, `MASTER`) to allow granular filtering and comprehensive daily summaries.

---
*Note: This GEMINI.md is updated for Gemini CLI to reflect the 2026.03.22 refactoring.*
