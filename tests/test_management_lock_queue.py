"""Financial actions wait once, preserving serialization and explicit deadlines."""
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from backend.mcp.resilience import Budget, BudgetExceeded, current_budget
from backend.utils.position_management import financial_lock


def test_financial_action_waits_past_old_five_second_limit():
    entered = threading.Event()
    writes = []

    def action():
        entered.set()
        with financial_lock('queue-regression'):
            writes.append('submitted')

    with ThreadPoolExecutor(max_workers=1) as pool:
        with financial_lock('queue-regression'):
            future = pool.submit(action)
            assert entered.wait(2)
            time.sleep(5.2)
            assert not future.done()
            assert writes == []
        future.result(timeout=3)
    assert writes == ['submitted']


def test_explicit_deadline_expires_without_executing_or_releasing_holder():
    def action():
        token = current_budget.set(Budget(time.monotonic() + .05, 'lock-test', 'execute'))
        try:
            with financial_lock('deadline-regression'):
                pytest.fail('Must not execute while the original holder owns the lock')
        finally:
            current_budget.reset(token)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with financial_lock('deadline-regression'):
            with pytest.raises(BudgetExceeded, match='financial_lock'):
                pool.submit(action).result(timeout=2)
        # A timed-out waiter must not damage the lock.
        with financial_lock('deadline-regression'):
            pass


def test_reentrant_lock_released_after_action_exception():
    with pytest.raises(RuntimeError, match='action failed'):
        with financial_lock('exception-regression'):
            with financial_lock('exception-regression'):
                raise RuntimeError('action failed')
    with ThreadPoolExecutor(max_workers=1) as pool:
        def next_action():
            with financial_lock('exception-regression'):
                return 'completed'
        assert pool.submit(next_action).result(timeout=2) == 'completed'
