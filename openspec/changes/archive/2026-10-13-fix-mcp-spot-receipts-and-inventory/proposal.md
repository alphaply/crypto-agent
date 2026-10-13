# Proposal

## Why
When external agents (such as ChatGPT web via MCP) manage spot trading, querying non-existent operations raises an unhandled `ValueError` that MCP wraps into a tool-crashing `INVALID_ARGUMENT` exception instead of returning a structured read-only result. Furthermore, spot operations never recover filled receipts (`_recover_trade_receipt` skips spot), leaving executed orders perpetually stuck in `submitted` without execution timestamps or fill prices, while hyper-strict spot inventory checks (`math.isclose(total, owned)`) fatal-fail whenever wallets contain pre-existing balances, manual holdings, or BNB fee deductions.

## What Changes
- **Graceful Operation Lookup**: Update MCP `get_operation` so that non-existent or inaccessible operations return a structured `{ "operation_id": "...", "state": "not_found", "status": "failed", "found": false }` payload instead of raising an uncaught `ValueError` (`INVALID_ARGUMENT`).
- **Spot Operation Receipt Recovery**: Remove the spot exclusion in MCP `_recover_trade_receipt`, enabling spot buy/exit operations to automatically synchronize with exchange order status (`FILLED`, `CANCELLED`, `PARTIAL`), recording exact fill timestamps (`filled_at`), average execution prices (`avg_fill_price`), and filled quantities.
- **Coexistence-Friendly Spot Inventory Reconciliation**: Relax the all-or-nothing `math.isclose(total, owned)` check in `backend/mcp/spot_orders.py` so that external balances, manual holdings, and fee variances do not block `get_spot_inventory` or `create_spot_exit`; sellable quantity is safely capped at `min(free_balance, verified_mcp_owned)` with clear reconciliation breakdown.
- **Read-Only Trade and Order Query Tools**: Expose read-only `get_order` (single order status and fill inspection) and `get_trade_history` in MCP tools so agents can explicitly verify order fills, execution timestamps, and trade histories even after open orders leave `fetch_open_orders`.

## Capabilities

### New Capabilities
- `spot-inventory-reconciliation`: Covers spot balance reconciliation, allowing verified MCP inventory and manual/shared balance coexistence without failing inventory queries or blocking sell orders.

### Modified Capabilities
- `trading-tool-contracts`: Extends operation querying (`get_operation`) to return structured not-found results instead of throwing tool-crashing exceptions, supports spot order fill recovery (`FILLED` status, timestamps, prices), and provides read-only trade/order inspection tools in MCP.

## Impact
- `backend/mcp/service.py`: `operation` error handling, `_recover_trade_receipt` spot integration, read-only order/trade query routing.
- `backend/mcp/tools.py`: Tool catalog additions (`get_order`, `get_trade_history`), docstrings, and input schemas.
- `backend/mcp/spot_orders.py`: `_inventory` reconciliation logic to support partial/shared asset balances without fatal exceptions.
- `backend/utils/spot_execution.py`: Ensuring auto-reconciliation clears `unknown` reservations when exchange evidence confirms order states.
- Tests: New and updated tests in `tests/test_mcp_tool_contracts.py`, `tests/test_mcp_trade_recovery.py`, and `tests/test_spot_trade_execution.py`.
