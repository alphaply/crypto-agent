# Verification — 2026-10-29

## Implementation checks

- Browser: `frontend/tests/memory-edit.browser.cjs`, Edge headless at 1440px and 390px, isolated API fixtures. Both passed: open public page before login in another tab; login/credential rotation/logout without reload; latest Authorization header; failed 401/500/network requests preserve drafts and display errors; successful save is immediately visible while workspace refresh is delayed; reload and console route; management editor failure/retry. No browser page errors.
- PostgreSQL: disposable local PostgreSQL 17 database with no production data. Authenticated PUT, public/admin reads and next-decision memory input agree; unauthenticated PUT is rejected; other task records and historical position snapshots stay unchanged; missing memory returns 404.
- Time: preserve raw values and source offsets, UTC 12:30 / Beijing 20:30, next-day conversion, US Eastern summer/winter offsets, existing +08:00, unknown/missing timezone, all prompt boundaries and summary cache invalidation passed.
- GDP read-only replay: fixed current time 2026-10-29 13:00 +08:00 and release 12:30 UTC produces `upcoming`; at 20:30 +08:00 produces `scheduled_time_reached`, explicitly not proof of actual release. This checks deterministic comparison and the model input; no live model or trading call was made and free-form model output is not guaranteed.
- Backend regression selection: `test_event_time_context`, `test_memory_manual_edit`, `test_news_modes_triggers`, `test_shared_news_pipeline`, `test_news_processing`, `test_news_intelligence`, `test_spot_portfolio_context`, `test_spot_market_context`, `test_prompt_role_compat`, `test_prompt_resolution`, `test_strategy_summary_pipeline`, `test_memory_snapshot_cleanup`: **152 passed, 3 subtests passed**, with the five baseline failures below excluded after reproducing them independently.
- Frontend Node tests: `dashboard.test.js` and `agentRuntime.test.js`: **17 passed**.
- ESLint for the five changed/new frontend modules: passed. Vite production build: passed. `git diff --check`: passed.
- Strict OpenSpec change validation and the existing 21 main specs: passed before sync; after sync, all 22 main specs and the change passed strict validation again.

## Existing failures outside this change

All five failures were reproduced against an isolated export of unmodified `d8fbfc9e3d55b432378db9fe8e360831a9336686`, using the same disposable database and Python environment. No unrelated code/tests were changed:

- `test_spot_portfolio_context.py::test_one_decision_sees_all_assets_and_one_shared_wallet`: expects available budget 0, existing code reports 100.
- `test_spot_portfolio_context.py::test_spot_prompt_keeps_holdings_and_orders_once_without_forced_review` (three parameterizations): same existing budget assertion.
- `test_spot_portfolio_context.py::test_public_workspace_keeps_selected_chart_symbol`: mocks removed `get_daily_summaries_payload` attribute.

## Release checks

After this archive is committed, run the repository release script without `-SkipCapacityAcceptance`. It must rebuild the frontend and revision-labelled image, pass isolated PostgreSQL migration/application/cgroup checks and full three-container capacity acceptance before pushing Git and both image tags. Actual release results and immutable revision/digest are reported separately after the script completes. This record alone does not claim publication or production deployment.
