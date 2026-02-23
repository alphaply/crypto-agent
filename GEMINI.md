# GEMINI.md - Project Context & Instructions

## Project Overview
**crypto-agent** (自动亏钱 Agent) is an automated cryptocurrency trading system built with Python. It uses Large Language Models (LLMs) via LangGraph to analyze market data and execute trades on Binance (USDM-Futures). The project features a Flask-based dashboard for monitoring and supports both real-money trading and strategy-only (mock) modes.

### Tech Stack
- **Language:** Python 3.10+
- **Agent Orchestration:** [LangGraph](https://langchain-ai.github.io/langgraph/) (Stateful, multi-step agent pipelines)
- **Web Interface:** Flask + Tailwind CSS
- **Exchange Integration:** [CCXT](https://github.com/ccxt/ccxt) (Binance USDM-Futures)
- **Database:** SQLite (Orders, summaries, balance history)
- **Environment Management:** `uv` (recommended) or `pip`

---

## Core Architecture

### 1. Agent Logic (`agent/`)
The trading logic is encapsulated in a LangGraph `StateGraph`:
- **`start_node`**: Gathers market data (OHLCV, indicators), account status, and historical summaries. It prepares a detailed system prompt for the LLM.
- **`agent_node`**: Invokes an OpenAI-compatible LLM with structured output (Function Calling) to decide on actions (BUY, SELL, CLOSE, CANCEL, NO_ACTION).
- **`execution_node`**: Processes LLM decisions, saves analysis logs, and executes trades via CCXT (Real) or SQLite (Mock).

### 2. Scheduler (`main_scheduler.py`)
Intelligent "heartbeat" mechanism:
- **REAL Mode**: Runs every 15 minutes.
- **STRATEGY Mode**: Runs every hour (at the top of the hour).
- Managed as a background thread in `dashboard.py`.

### 3. Configuration (`config.py`)
Centralized management of:
- Global Binance API keys and LangChain settings.
- `SYMBOL_CONFIGS`: A JSON list in `.env` defining multiple agents (symbols, models, modes, leverage).
- Supports independent LLM and API configurations per agent.

---

## Building and Running

### Prerequisites
- Python 3.10+
- Binance API Key (with Futures permission)
- LLM API Key (OpenAI, Gemini, etc.)

### Installation
```bash
# Using uv (recommended)
uv sync

# Using pip
pip install -r requirements.txt
```

### Configuration
1. Copy `.env.template` to `.env`.
2. Fill in `BINANCE_API_KEY`, `BINANCE_SECRET`, and `SYMBOL_CONFIGS`.
3. Set `ADMIN_PASSWORD` for dashboard management.

### Execution
```bash
# Start both the scheduler and the dashboard
python dashboard.py
```
Access the dashboard at `http://localhost:7860`.

---

## Development Conventions

### Code Style
- **Type Hinting:** Extensively used in `agent_models.py` and core logic. Maintain consistency.
- **Logging:** Use the custom logger from `utils.logger`. Format: `setup_logger("ModuleName")`.
- **Database:** Use the abstractions in `database.py` rather than raw SQL in other modules.

### Agent Development
- **Prompting:** Prompts are stored in `prompts/` or `utils/prompts.py`. Use `resolve_prompt_template` to manage templates.
- **State Management:** The `AgentState` in `agent/agent_models.py` is the source of truth for the graph.

### Testing
- Use `utils/test_agent_connection.py` to verify LLM and exchange connectivity.
- **Mock Trading:** Always test new strategies in `STRATEGY` mode before switching to `REAL`.

---

## Important Files
- `dashboard.py`: Entry point (Flask + Scheduler).
- `config.py`: Config loader and validator.
- `database.py`: SQLite schema and ORM-like functions.
- `agent/agent_graph.py`: LangGraph definition.
- `utils/market_data.py`: Market data and indicator calculation (CCXT wrapper).
