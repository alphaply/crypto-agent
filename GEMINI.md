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
  - **Smart Screener System (v2)**: 
    - **Dual-Model Logic**: Uses a lightweight model (e.g., GPT-4o-mini) for initial screening. Only escalates to a powerful "Master Agent" (e.g., GPT-4o/Claude-3.5) if significant opportunities or risks are detected.
    - **Built-in Intelligence**: Screener prompts are now built-in for consistency, providing structured analysis and price predictions even when not escalating.
    - **Chat Integration**: The screener also acts as a "gatekeeper" in interactive chat, quickly answering simple market queries to save costs and latency.
  - **Performance Analytics**: Real-time tracking of account equity, trade history, LLM token usage, and daily strategy summaries (now including screener insights).
  - **Enhanced Indicator System (v2)**: Optimized signal set with MACD momentum labeling, RSI divergence detection, and adaptive Volume Profile.

## Architecture

- **`dashboard.py`**: Main Flask web interface and entry point for the smart scheduler.
- **`main_scheduler.py`**: A 1-minute heartbeat engine that triggers agents based on individual timeframes or DCA schedules.
- **`agent/`**: 
  - `agent_graph.py`: Core LangGraph state machine (Start -> Screener? -> Analyze -> Trade/Wait -> Finalize).
  - `chat_graph.py`: Interactive LangGraph workflow with built-in screener support.
  - `agent_tools.py`: Unified toolset for order execution and state management.
- **`utils/`**:
  - `market_data.py`: `MarketTool` wrapper for standardized OHLCV, derivatives, and account status.
  - `indicators.py`: Professional TA indicator implementations optimized for LLM consumption.
  - `formatters.py`: Translates raw JSON data into concise, high-density prompt context.

## Development Standards & Conventions

### Screener Configuration (v2)
- **Nested Structure**: Screener settings are now nested under the `screener` key in the symbol configuration.
- **Parameters**: Supports independent `model`, `api_base`, `api_key`, and `temperature`. 
- **Escalation**: Controlled by `escalation_threshold` (0-100) and `should_escalate` flag from the model.

### UI & Display
- **Analysis Source**: The dashboard explicitly labels whether a result came from "🔍 初筛模型" or "🧠 决策模型".
- **Layout Alignment**: Uses a balanced flex layout to ensure market analysis and order logs are visually aligned (equal height).

### Data Persistence
- **Mode Isolation**: Strictly separate `REAL`, `STRATEGY`, and `SPOT_DCA` execution paths.
- **Agent Type**: Records are tagged with `agent_type` (e.g., `SCREENER`) to allow granular filtering and comprehensive daily summaries.

---
*Note: This GEMINI.md is updated for Gemini CLI to reflect the 2026.03.19 refactoring.*
