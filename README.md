# Crypto Agent

Crypto Agent is a FastAPI + React trading-agent workspace for strategy analysis, order records, K-line visualization, usage cost tracking, and runtime configuration.

## Configuration Model

The project uses a two-layer configuration model:

- `.env` or system environment variables: bootstrap and security settings only.
- `trading_data.db`: runtime settings managed from `/console/config`.

Keep `.env` small. Daily trading configuration should be edited in the WebUI, including symbols, agents, model settings, exchange credentials, prompts, and model pricing.

Recommended `.env` keys:

```env
ADMIN_PASSWORD=change-this-password
JWT_SECRET=change-this-jwt-secret
JWT_EXPIRE_HOURS=8
CONFIG_MASTER_KEY=change-this-config-master-key
PORT=7860
RUN_SCHEDULER_IN_WEB=true
TIMEZONE=Asia/Shanghai
```

Legacy env keys such as `SYMBOL_CONFIGS`, exchange API keys, and LLM keys are still read for first-run migration compatibility, but they are no longer the normal maintenance path.

## Development

Install dependencies:

```bash
uv sync
npm install --prefix frontend
```

Create local bootstrap config:

```powershell
Copy-Item .env.template .env
```

Start the backend and scheduler:

```bash
uv run python -m backend.app
```

Start the frontend dev server:

```bash
npm run dev --prefix frontend
```

Common URLs:

- `http://localhost:7860/`: dashboard
- `http://localhost:7860/usage`: public usage analytics
- `http://localhost:7860/console/chat`: authenticated console
- `http://localhost:7860/console/config`: runtime configuration
- `http://localhost:5173/`: Vite dev server

## Production Docker Deploy

The recommended production path is the published Docker Hub image:

```bash
mkdir -p crypto-agent
cd crypto-agent
curl -O https://raw.githubusercontent.com/alphaply/crypto-agent/v0.2.0-preview.1/docker-compose.yml
curl -O https://raw.githubusercontent.com/alphaply/crypto-agent/v0.2.0-preview.1/.env.template
cp .env.template .env
```

Edit `.env` before starting the service:

```env
ADMIN_PASSWORD=your-strong-password
JWT_SECRET=your-long-random-jwt-secret
CONFIG_MASTER_KEY=your-long-stable-config-master-key
PORT=7860
RUN_SCHEDULER_IN_WEB=true
TIMEZONE=Asia/Shanghai
```

Start the container:

```bash
docker compose pull
docker compose up -d
docker compose logs -f
```

Open `http://localhost:7860/` and check health:

```bash
curl http://localhost:7860/health
```

The compose stack stores SQLite runtime state, chat checkpoints, and logs in the `crypto_agent_data` volume at `/app/data`. Keep `CONFIG_MASTER_KEY` stable for that volume, otherwise existing encrypted secrets cannot be decrypted. Do not use `docker compose down -v` during normal upgrades because it deletes the named volume and removes SQLite runtime data.

For local image testing from source:

```bash
docker build -t alphaply/crypto-agent:v0.2.0-preview.1 .
docker run --rm -p 7860:7860 \
  -e ADMIN_PASSWORD=local-password \
  -e JWT_SECRET=local-jwt-secret \
  -e CONFIG_MASTER_KEY=local-config-master-key \
  alphaply/crypto-agent:v0.2.0-preview.1
```

Useful commands:

```bash
docker compose logs -f
docker compose down
docker compose down -v
```

## Docker Operations

Recommended upgrade flow:

```bash
docker compose pull
docker compose up -d
docker compose logs -f
```

For local source builds, use `docker build -t alphaply/crypto-agent:v0.2.0-preview.1 .` or add a local compose override with `build: .`.

Before upgrading a production instance:

```bash
docker compose exec crypto-agent python - <<'PY'
from backend.config_store import export_full_snapshot
import json
print(json.dumps(export_full_snapshot(include_secrets=True), ensure_ascii=False, indent=2))
PY
docker compose cp crypto-agent:/app/data ./crypto_agent_data_backup
```

Also record the current `CONFIG_MASTER_KEY`. Restoring `trading_data.db` without the matching key will leave encrypted secrets unreadable.

Rollback is image-based: start the previous image/tag with the same `.env` and the same `crypto_agent_data` volume. Schema migrations are additive and run at startup through `init_db()` / runtime config initialization.

The `/health` endpoint reports database access, runtime config loading, and scheduler settings. A degraded health response means the container is up but not fully ready for trading workflows.

## WebUI Management

The console configuration page manages:

- global runtime settings: scheduler, leverage, LLM timeout/retries, market timeframes
- agent settings: config ID, symbol, mode, schedule, prompt, DCA settings
- model settings: model, API base, temperature, extra body, DeepSeek thinking mode
- encrypted secrets: LLM API key and exchange keys
- summarizer settings: choose any model provider and edit strategy/daily/short-memory summary prompts directly in the database-backed config
- prompt files
- model input/output pricing and currency
- daily summaries: list, edit, and export
- short-term memory: generated every 4 hours, visible in the dashboard workspace, and injected into the next agent prompt

Secrets are stored in SQLite `secret_store` encrypted with `CONFIG_MASTER_KEY`.

## Dashboard

The dashboard is symbol-first:

- the header and dashboard controls share the selected symbol
- overview cards show agent count, win rate, trade count, PnL, token usage, and cost
- config tabs show one workspace per agent plus a compare view
- the compare view includes equity, long/short counts, close count, order total, cancel count, win rate, and PnL
- K-line charts support EMA 20/50/100/200, positions, entry lines, TP/SL, and pending orders
- analysis and operation records are split into desktop columns and stack on mobile
- latest 7 daily summaries are shown per config

## Market Data

Default market analysis timeframes:

```text
5m, 15m, 1h, 4h, 1d, 1w, 1M
```

The monthly `1M` timeframe uses a smaller fetch limit and lower minimum sample requirement. The formatted market context includes a macro trend overview using monthly and weekly trends when both are available.

## Validation

Run backend tests:

```bash
uv run python -m unittest discover -s tests
```

Build the frontend:

```bash
npm run build --prefix frontend
```

Optional connectivity smoke test:

```bash
uv run backend/utils/test_agent_connection.py
```

## Project Layout

- `backend/app/`: FastAPI app, auth, routers, services
- `backend/app/core/scheduler.py`: scheduler
- `backend/agent/`: agent graphs, tools, prompt templates
- `backend/utils/`: market data, indicators, logging, LLM helpers
- `backend/config.py`: runtime config facade
- `backend/config_store.py`: SQLite-backed runtime config store
- `backend/database.py`: database schema and data access helpers
- `frontend/`: React + Vite app
- runtime state: `.env`, `trading_data.db`, `pricing.json`, logs

## Security Notes

- Never commit real API keys, `.env`, `pricing.json`, or SQLite runtime files.
- Use a strong `ADMIN_PASSWORD` outside local development.
- Rotate `JWT_SECRET` to invalidate already-issued sessions.
- Keep `CONFIG_MASTER_KEY` stable for a given database; changing it prevents decrypting existing secrets.
