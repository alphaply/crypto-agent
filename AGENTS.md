# Repository Guidelines

## Project Structure & Module Organization
Core backend logic is in `agent/` (LLM graph, tool wiring) and `utils/` (market data, indicators, logging, prompts).  
Web routes live in `routes/`, with Jinja templates in `templates/` and frontend assets in `static/` (`css/`, `js/`).  
Runtime entrypoints are `dashboard.py` (Flask app + optional scheduler thread) and `main_scheduler.py` (standalone scheduler loop).  
Operational docs are in `docs/`. Local runtime state includes `.env` and `trading_data.db`.

## Build, Test, and Development Commands
- `uv sync`: install and lock dependencies from `pyproject.toml` / `uv.lock`.
- `uv run dashboard.py`: start the web dashboard at `http://localhost:7860`.
- `uv run main_scheduler.py`: run scheduler only (no web UI).
- `uv run utils/test_agent_connection.py`: smoke test model/API connectivity.
- `python dashboard.py` / `python main_scheduler.py`: fallback if `uv` is unavailable.

## Coding Style & Naming Conventions
Follow Python 3.10+ conventions with 4-space indentation and PEP 8 style.  
Use `snake_case` for functions/variables, `PascalCase` for classes, and short, explicit module names (for example, `market_data.py`, `agent_graph.py`).  
Keep route handlers thin; move reusable logic into `utils/` or `agent/`.  
Prefer typed function signatures and `pydantic` models where structured output is required.

## Testing Guidelines
There is no fully enforced automated test suite yet. Current validation is script-based and manual:
- run `uv run utils/test_agent_connection.py` for LLM/output schema checks,
- run `uv run verify_fix.py` or targeted debug scripts for regressions,
- verify key UI flows in dashboard pages after backend changes.

When adding tests, place them under `tests/` with `test_*.py` names and prioritize deterministic unit tests for indicator, formatter, and config logic.

## Commit & Pull Request Guidelines
Use concise, prefix-based commit messages consistent with history: `feat: ...`, `fix: ...`, `refactor: ...`.  
Keep each commit focused on one change set (for example, scheduler timing, route API, or prompt handling).  
PRs should include:
- purpose and scope,
- affected configs/routes/modules,
- manual verification steps (commands run),
- screenshots for `templates/` or `static/` UI changes.

## Security & Configuration Tips
- Do not commit real API keys or secrets; use `.env.template` as the baseline.  
- Model pricing runtime file `pricing.json` is local-only and ignored by git; keep example defaults in templates/docs instead of committing private pricing changes.
- Prefer per-agent credentials in `SYMBOL_CONFIGS` only when required, and keep `ADMIN_PASSWORD` strong in non-local environments.

## Documentation Maintenance
- For release-level changes, keep `README.md` and all files under `docs/` synchronized with actual code behavior.
- Remove deprecated feature docs promptly (for example, old screener references after v1.0 cleanup).
