# GEMINI Notes (v1.0)

This file summarizes current architecture and coding expectations for AI-assisted development.

## Product State

- Version target: `v1.0`
- Active execution chain: single main decision model flow.
- Removed in v1.0: screener routing and related UI/config fields.

## Runtime Entry Points

- `dashboard.py`: Flask app + optional scheduler thread.
- `main_scheduler.py`: scheduler-only process.

## Core Modules

- `agent/agent_graph.py`: main LangGraph flow (`start -> agent -> tools/finalize`).
- `agent/agent_tools.py`: REAL / STRATEGY / SPOT_DCA execution tools.
- `database.py`: SQLite persistence, pricing sync, history and summary storage.
- `routes/`: web APIs and page routes.
- `templates/` + `static/`: web UI.

## Trading Modes

- `REAL`: real exchange execution.
- `STRATEGY`: mock strategy execution.
- `SPOT_DCA`: periodic spot DCA execution.

## Data and Cost Tracking

- Token usage stored in `token_usage`.
- Pricing source is `pricing.json` and DB table `model_pricing`.
- Pricing updates/deletes should keep file and DB in sync.

## Guardrails

- Keep `config_id` as the isolation key across history, orders, and stats.
- Avoid destructive resets without explicit user request.
- Keep frontend and backend behavior aligned for symbol/query fallback.

## Documentation Rule

When architecture or behavior changes, update at least:

1. `README.md`
2. `docs/CONFIG_GUIDE.md`
3. `docs/FAQ.md`
4. relevant release notes
