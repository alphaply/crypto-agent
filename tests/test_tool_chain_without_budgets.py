"""Cumulative execution budgets cannot strand original exchange observations."""
import asyncio
import threading

import pytest

from backend.mcp import resilience
from backend.utils import runtime_capacity


def test_response_timeout_does_not_expire_the_physical_worker(monkeypatch):
    monkeypatch.setenv('MCP_READ_TIMEOUT_SECONDS', '.03')
    manager = runtime_capacity.CapacityManager(total=2, reserved=1)
    monkeypatch.setattr(runtime_capacity, '_manager', manager)
    pool = resilience.WorkerPool('read', 1, 0)
    continue_read = threading.Event()
    observations = []

    def read_original():
        assert continue_read.wait(2)
        # A later page must retain its own request timeout after the response
        # has timed out. This used to raise BudgetExceeded forever on retries.
        observations.append(resilience.remaining('next_exchange_page'))
        return {'status': 'completed'}

    async def exercise():
        try:
            with pytest.raises(resilience.BudgetExceeded, match='response timeout'):
                await pool.run(read_original)
            assert manager.snapshot()['active'] == 1
            continue_read.set()
            for _ in range(1000):
                if not manager.snapshot()['active']:
                    break
                await asyncio.sleep(.001)
            assert observations == [30]
            assert manager.snapshot()['active'] == 0
        finally:
            continue_read.set()
            pool.executor.shutdown()

    asyncio.run(exercise())


def test_large_body_and_memory_pressure_do_not_disable_concurrency_control(monkeypatch):
    manager = runtime_capacity.CapacityManager(total=2, reserved=1, byte_limit=1, queue_limit=0)
    monkeypatch.setattr(runtime_capacity, '_manager', manager)
    manager.set_pressure(True, 'high_memory')
    with manager.reserve_bytes('parse', 10_000_000):
        worker = manager.acquire('mcp_read', timeout=0)
        try:
            with pytest.raises(runtime_capacity.CapacityBusy):
                manager.acquire('mcp_read', timeout=0)
            assert manager.snapshot()['bytes'] == 10_000_000
        finally:
            worker.release()
    assert manager.snapshot()['bytes'] == 0


def test_receipt_identity_is_not_rejected_by_the_former_text_budget():
    from backend.utils.trade_summary import compact_trade_receipt, render_trade_receipt
    identity = 'original-' + 'x' * 40_000
    original = {'status': 'completed', 'action': 'cancel', 'order_id': identity}
    assert compact_trade_receipt(original)['order_id'] == identity
    assert identity in render_trade_receipt(original)


@pytest.mark.parametrize('repeat_page', [False, True])
def test_income_scan_has_no_total_page_cap_but_detects_nonprogress(monkeypatch, repeat_page):
    from contextlib import nullcontext
    from types import SimpleNamespace
    from unittest.mock import Mock
    from backend.utils import execution_ledger as module
    calls = []
    def fetch(params):
        page = params['page']
        calls.append(page)
        if page == 52:
            return []
        return [{'incomeType': 'FUNDING_FEE', 'tranId': str((0 if repeat_page else page) * 1000 + i), 'time': 1}
                for i in range(1000)]
    exchange = SimpleNamespace(id='binance', market=lambda _: {'id': 'ETHUSDT'}, fapiPrivateGetIncome=fetch)
    ledger = object.__new__(module.ExecutionLedger)
    ledger.exchange, ledger.symbol, ledger.scope = exchange, 'ETH/USDT:USDT', 'offline-income'
    ledger.lock = threading.Lock()
    ledger.status = lambda: {}
    ledger._status = lambda _: None
    conn = Mock()
    monkeypatch.setattr(module, 'get_db_conn', lambda: nullcontext(conn))
    result = ledger.sync_income(0, 2)
    if repeat_page:
        assert calls == [1, 2]
        assert not result['complete'] and 'did not advance' in result['error']
    else:
        assert len(calls) == 52 and result['complete']
        assert conn.execute.call_count == 51_000
