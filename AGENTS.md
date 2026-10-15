# Repository Guidelines

## Requirement Scope and Confirmation
- 只实现用户明确要求的行为。
- 不得自行增加业务需求、替代数据源、跨交易所查询、自动降级或默认值填充。
- 未明确的业务行为先提出问题，不得直接写入 specs、design 或 tasks 当作已确认需求。
- 必要的内部实现细节可以自行决定，但会改变查询范围、结果含义或错误处理方式的选择必须先确认。
- 建议与已确认需求分开列出。

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

## OpenSpec Archive and Release
- An archive request includes syncing the selected change into the main specs and releasing the verified change, unless the user explicitly narrows the request.
- Follow `openspec/specs/archive-release-workflow/spec.md` and `docs/DEPLOYMENT_GUIDE.md`. Validate the synced specs and completed tasks before moving the change into the dated archive directory.
- Stage only the change's implementation, tests, docs, synced specs and archive. Preserve unrelated local files, including untracked `.agents/` content. Commit with a focused message on `beta`; never force-push or discard unrelated work to release.
- After committing, run `powershell -NoProfile -File scripts/release-archived-change.ps1 -ArchiveName <YYYY-MM-DD-change-name>`. This rebuilds the frontend and Docker image, runs an isolated scheduler-disabled smoke test, pushes `origin/beta`, and publishes `alphaply712/crypto-agent:git-<short-sha>` and `:latest`.
- Report the archive path, checks, Git commit and image digest. If a step fails, stop dependent steps and report the exact completed and pending stages; archiving files alone does not mean release succeeded.
- Publishing an image does not restart production containers. Production rollout is a separate action when requested by the user.
