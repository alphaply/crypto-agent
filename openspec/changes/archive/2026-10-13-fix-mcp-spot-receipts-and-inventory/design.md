# Design

## Context
See proposal.md - Why.
The MCP gateway (`backend/mcp/service.py`) acts as the external integration layer for LLM agents (such as ChatGPT web via SSE/HTTP). When agents invoke `get_operation`, `get_spot_inventory`, or `create_spot_exit`, they interact with local database tables (`mcp_operations`, `orders`, `spot_budget_reservations`, `spot_order_fills`) and CCXT exchange adapters.

## Goals / Non-Goals

**Goals:**
- Provide resilient, non-crashing operation queries where missing operations return structured `{ "operation_id": "...", "state": "not_found", "status": "failed", "found": false }` instead of raising an uncaught `ValueError` (`INVALID_ARGUMENT`).
- Enable spot operation recovery in `_recover_trade_receipt` so that filled/cancelled spot orders update their stored receipt from `submitted` to terminal states (`FILLED`, `CANCELLED`), populating `filled_at` (timestamp), `avg_fill_price` (average price), and `filled_amount`.
- Relax `_inventory` in `backend/mcp/spot_orders.py` from strict equality (`math.isclose(total, owned)`) to bounded isolation: allow accounts with pre-existing or manual holdings, capping sellable inventory at `min(exchange_free, verified_mcp_owned)` and returning clear breakdown metadata.
- Expose read-only `get_order` and `get_trade_history` MCP tools to let external agents query order status and trade history directly after orders exit the active open orders list.

**Non-Goals:**
- Allowing external agents to liquidate unverified external assets without explicit configuration.
- Modifying perpetual futures independent exit mechanisms.

## Decisions

### Decision 1: Structured Dictionary vs Uncaught Exception in `operation()`
- **Choice**: Return a structured dictionary (`{ "operation_id": operation_id, "state": "not_found", "status": "failed", "found": false }`) when `operation_id` is not found or not owned by the caller.
- **Rationale**: In FastMCP / MCP JSON-RPC, raising a Python `ValueError` or `RuntimeException` maps to an error response (`-32602 INVALID_ARGUMENT`), which LLM clients treat as a catastrophic tool execution failure, aborting their reasoning loop. Returning a structured result allows the agent to inspect the fact that the ID was not found and proceed with fallback logic.
- **Alternative considered**: Raising a custom `MCPError`. Rejected because client behavior still treats any JSON-RPC exception as an unhandled error.

### Decision 2: Spot Receipt Recovery in `_recover_trade_receipt`
- **Choice**: Extend `_recover_trade_receipt` to handle spot trading profiles (`market_type == 'spot'`).
- **Rationale**: Currently, `_recover_trade_receipt` explicitly returns `None` for spot profiles. When an operation is in `submitted` or `unknown`, it should check local `orders`/`spot_order_fills` and, if non-terminal, fetch the latest order state from CCXT (`fetch_order`), updating `spot_order_fills` and the stored `mcp_operations` receipt to reflect `FILLED` or `CANCELLED`.
- **Alternative considered**: Forcing external agents to poll `get_spot_inventory` instead. Rejected because inventory does not report specific order execution prices or fill timestamps.

### Decision 3: Bounded Spot Inventory Coexistence Model
- **Choice**: Replace the fatal `total != owned` exception with a safe bounded model:
  - `owned_quantity = max(0, verified_mcp_buys_minus_fees_and_exits)`
  - `available_quantity = max(0, min(exchange_free, owned_quantity - reserved_quantity))`
  - If `total > owned_quantity`: classify the surplus as `untracked_or_shared_holdings`, log a warning, and allow selling within `available_quantity`.
  - If `total < owned_quantity`: report an inventory deficit alert and cap available sells at `min(exchange_free, total)`.
- **Rationale**: Real accounts frequently hold pre-existing crypto balances, receive transfers, or use BNB fee deductions. Strict equality breaks selling for any non-isolated wallet.
- **Alternative considered**: Adopting spot balances into MCP inventory. Rejected as default because MCP should not assume permission to manage manual spot holdings unless explicitly instructed.

### Decision 4: Read-Only Order and Trade Inspection Tools in MCP
- **Choice**: Add `get_order(profile_id, order_id, symbol)` and `get_trade_history(profile_id, symbol, limit)` to `TOOL_CATALOG` in `backend/mcp/tools.py`.
- **Rationale**: `get_orders` only returns open orders via `fetch_open_orders()`. Once an order is fully filled or cancelled, it vanishes from `get_orders`. External agents need read-only visibility into filled orders and historical trades.

## Risks / Trade-offs

- **[Risk]** Exchange rate limits during repeated `get_operation` calls.
  → **Mitigation**: Only query CCXT when the stored operation is in a non-terminal state (`started`, `submitted`, `unknown`). Once marked `FILLED`, `CANCELLED`, or `failed`, cached results are returned directly.
- **[Risk]** Selling more than desired if external assets are inadvertently sold.
  → **Mitigation**: `available_quantity` is strictly capped by `verified_mcp_owned - reserved_quantity`. The agent can never sell more base asset than MCP itself purchased.
