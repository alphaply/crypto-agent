# Repository Guidelines

## Project Structure & Module Organization
Core trading logic lives under `backend/`.

- `backend/app/`: FastAPI app, JWT auth, routers, service layer
- `backend/app/core/scheduler.py`: scheduler implementation
- `backend/agent/`: agent graphs, tools, prompt templates
- `backend/utils/`: market data, indicators, logging, LLM helpers
- `backend/config.py`: runtime config loader
- `backend/database.py`: database access layer
- `frontend/`: React + Vite frontend
- `docs/`: operational and product docs
- runtime state: `.env`, `trading_data.db`

## Build, Test, and Development Commands
- `uv sync`: install Python dependencies from `pyproject.toml` / `uv.lock`
- `npm install --prefix frontend`: install frontend dependencies
- `uv run python -m backend.app`: start FastAPI and the scheduler on `http://localhost:7860`
- `npm run dev --prefix frontend`: start the React dev server on `http://localhost:5173`
- `uv run backend/utils/test_agent_connection.py`: smoke test model/API connectivity
- `npm run build --prefix frontend`: build the frontend for FastAPI static serving

## Coding Style & Naming Conventions
Follow Python 3.10+ conventions with 4-space indentation and PEP 8 style.
Use `snake_case` for functions and variables, `PascalCase` for classes, and short module names.
Keep FastAPI route handlers thin and move reusable logic into `backend/app/services/`.
Prefer typed function signatures and `pydantic` models for structured API payloads.

Frontend should use modern React patterns with function components and hooks.

## Testing Guidelines
There is no fully enforced automated suite yet. Current validation is:

- `uv run backend/utils/test_agent_connection.py`
- `npm run build --prefix frontend`
- manual verification of key routes and chat/dashboard/admin flows

When adding tests, place them under `tests/` with `test_*.py` names.

## Commit & Pull Request Guidelines
Use focused prefix-based messages such as `feat: ...`, `fix: ...`, `refactor: ...`.

PRs should include:
- purpose and scope
- affected modules or APIs
- manual verification steps
- screenshots for React UI changes when useful

## Security & Configuration Tips
- Never commit real API keys or secrets
- Keep `pricing.json` local-only
- Use a strong `ADMIN_PASSWORD` in non-local environments
- JWT signing uses `JWT_SECRET`, falling back to existing local secrets if needed

## Documentation Maintenance
- Keep `README.md` and `docs/` in sync with actual FastAPI/React behavior
- Remove references to deleted Flask/Jinja flows when they are no longer present
