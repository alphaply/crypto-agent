# Tasks

## 1. Structured Operation Query & Error Handling

- [x] 1.1 In `backend/mcp/service.py`, update `operation(caller, profile_id, operation_id)` to return a structured dictionary `{ "operation_id": operation_id, "state": "not_found", "status": "failed", "found": False, "message": "Operation not found for this caller" }` instead of raising `ValueError`; verify with isolated function call test.
- [x] 1.2 Add tests in `tests/test_mcp_tool_contracts.py` and `tests/test_mcp_trade_recovery.py` verifying that querying missing or unowned operations returns the structured not-found dictionary and does not raise JSON-RPC / FastMCP exceptions.

## 2. Spot Operation Receipt Recovery

- [x] 2.1 In `backend/mcp/service.py`, update `_recover_trade_receipt` to support spot profiles (`market_type == 'spot'`), reading `spot_order_fills` and querying CCXT `fetch_order` for non-terminal spot orders.
- [x] 2.2 Update receipt payload projection so that terminal exchange order states (`FILLED`, `CANCELLED`, `PARTIAL`) refresh `mcp_operations` with `filled_at` (timestamp), `avg_fill_price` (average price), `filled_amount`, and `status`.
- [x] 2.3 Add tests in `tests/test_mcp_trade_recovery.py` verifying that a submitted spot order resolves to `FILLED` with execution timestamp and price upon subsequent `get_operation` queries.

## 3. Spot Inventory Coexistence & Reconciliation

- [x] 3.1 In `backend/mcp/spot_orders.py`, update `_inventory` to replace the fatal `math.isclose(total, owned)` check with safe upper-bound capping: compute `available_quantity = max(0, min(free, owned - reserved))` and report `untracked_holdings` when `total > owned`.
- [x] 3.2 Update `create_spot_exit` validation to ensure sell requests are bounded by `available_quantity` without raising exceptions when pre-existing or manual holdings exist.
- [x] 3.3 Add unit tests in `tests/test_spot_trade_execution.py` and `tests/test_mcp_tool_contracts.py` validating that accounts with pre-existing balances, manual holdings, and BNB fee deductions query inventory successfully and place exits within safe bounds.

## 4. Read-Only Order and Trade History Tools in MCP

- [x] 4.1 In `backend/mcp/tools.py` and `backend/mcp/service.py`, register `get_order(profile_id, order_id, symbol)` to read order status, filled quantity, average price, and execution timestamps from CCXT / local records.
- [x] 4.2 In `backend/mcp/tools.py` and `backend/mcp/service.py`, register `get_trade_history(profile_id, symbol, limit)` to read historical fills within profile scope.
- [x] 4.3 Add tests in `tests/test_mcp_tool_contracts.py` ensuring `get_order` and `get_trade_history` are registered with read scope and return verified order/trade payloads without financial writes.

## 5. Verification and End-to-End Regression

- [x] 5.1 Run test suite `pytest tests/test_mcp_tool_contracts.py tests/test_mcp_trade_recovery.py tests/test_spot_trade_execution.py` and verify all tests pass.
- [x] 5.2 Validate OpenSpec change with `openspec validate fix-mcp-spot-receipts-and-inventory`.
