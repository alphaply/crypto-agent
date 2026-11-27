"""Unit tests for sync completeness freshness, fill attribution backfill, and memory closed positions."""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock

from backend import database
from backend.agent import memory_agent, memory_service
from backend.utils import execution_ledger, task_performance


def test_window_complete_respects_sync_freshness_and_errors(monkeypatch):
    now = datetime(2026, 11, 27, 14, 0, 0, tzinfo=database.TZ_CN)
    end_ms = int(now.timestamp() * 1000)
    beginning = end_ms - 7 * 24 * 3600 * 1000
    sync_state = [{
        'symbol': 'ETH/USDT:USDT',
        'covered_intervals': [[beginning - 1000, end_ms - 45_000]],
        'pending_windows': [],
        'error': None,
    }]

    monkeypatch.setattr(task_performance, '_real_history', lambda *args: ([], [], True, sync_state))
    monkeypatch.setattr('backend.utils.task_order_history.task_orders', lambda *args: [])

    result = task_performance.task_performance(
        'task-1', now=now, cfg={'mode': 'REAL', 'symbol': 'ETH/USDT'}
    )
    assert result['window_complete'] is True

    stale_sync = [{**sync_state[0], 'covered_intervals': [[beginning - 1000, end_ms - 240_000]]}]
    monkeypatch.setattr(task_performance, '_real_history', lambda *args: ([], [], True, stale_sync))
    assert task_performance.task_performance(
        'task-1', now=now, cfg={'mode': 'REAL', 'symbol': 'ETH/USDT'}
    )['window_complete'] is False

    errored_sync = [{**sync_state[0], 'error': 'timeout'}]
    monkeypatch.setattr(task_performance, '_real_history', lambda *args: ([], [], True, errored_sync))
    assert task_performance.task_performance(
        'task-1', now=now, cfg={'mode': 'REAL', 'symbol': 'ETH/USDT'}
    )['window_complete'] is False

    pending_sync = [{**sync_state[0], 'pending_windows': [[end_ms - 60_000, end_ms]]}]
    monkeypatch.setattr(task_performance, '_real_history', lambda *args: ([], [], True, pending_sync))
    assert task_performance.task_performance(
        'task-1', now=now, cfg={'mode': 'REAL', 'symbol': 'ETH/USDT'}
    )['window_complete'] is False


def test_register_order_idempotent_still_reconciles_unattributed_fills(monkeypatch):
    executed = []
    requested = []

    class CursorResult:
        def __init__(self, row=None, rowcount=0):
            self._row = row
            self.rowcount = rowcount

        def fetchone(self):
            return self._row

    class FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, sql, params=()):
            executed.append((sql, params))
            if 'SELECT config_id,role,payload FROM execution_order_links' in sql:
                return CursorResult(row={'config_id': 'task-1', 'role': 'entry', 'payload': '{"side": "LONG"}'})
            if 'UPDATE execution_fills SET config_id=' in sql:
                return CursorResult(rowcount=2)
            return CursorResult()

        def commit(self):
            executed.append(('COMMIT', ()))

    monkeypatch.setattr(execution_ledger, 'get_db_conn', lambda: FakeConn())
    monkeypatch.setattr(execution_ledger, 'lock_scope', lambda *args: None)
    monkeypatch.setattr(
        'backend.utils.execution_history_work.request_history',
        lambda cid, scope, sym, conn=None: requested.append((cid, scope, sym)),
    )

    execution_ledger.register_order('scope-1', 'ETH/USDT:USDT', 'order-1', 'task-1', 'entry', side='LONG')

    assert any('UPDATE execution_fills SET config_id=' in sql for sql, _ in executed)
    assert ('COMMIT', ()) in executed
    assert requested == [('task-1', 'scope-1', 'ETH/USDT:USDT')]


def test_reconcile_linked_fills_backfills_null_config_id(monkeypatch):
    executed = []
    requested = []

    class CursorResult:
        def __init__(self, rows=None):
            self._rows = rows or []

        def fetchall(self):
            return self._rows

    class FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, sql, params=()):
            executed.append((sql, params))
            if 'UPDATE execution_fills f' in sql and 'RETURNING l.config_id' in sql:
                return CursorResult(rows=[('task-1',), ('task-1',)])
            return CursorResult()

        def commit(self):
            executed.append(('COMMIT', ()))

    monkeypatch.setattr(execution_ledger, 'get_db_conn', lambda: FakeConn())
    monkeypatch.setattr(execution_ledger, 'lock_scope', lambda *args: None)
    monkeypatch.setattr(
        'backend.utils.execution_history_work.request_history',
        lambda cid, scope, sym, conn=None: requested.append((cid, scope, sym)),
    )

    ledger = object.__new__(execution_ledger.ExecutionLedger)
    ledger.scope = 'scope-1'
    ledger.symbol = 'ETH/USDT:USDT'
    ledger.index_state = {'complete': True, 'ownership_ready': True}

    count = ledger.reconcile_linked_fills()
    assert count == 2
    assert requested == [('task-1', 'scope-1', 'ETH/USDT:USDT')]
    assert ('COMMIT', ()) in executed


def test_format_recent_position_history_includes_closed_positions_and_streak(monkeypatch):
    now = datetime.now(database.TZ_CN)
    positions = []
    for idx in range(4):
        closed_dt = now - timedelta(hours=idx * 4 + 1)
        opened_dt = closed_dt - timedelta(minutes=45)
        positions.append({
            'status': 'closed',
            'symbol': 'ETH/USDT:USDT',
            'side': 'LONG' if idx % 2 == 0 else 'SHORT',
            'opened_at': opened_dt.isoformat(),
            'closed_at': closed_dt.isoformat(),
            'holding_seconds': 2700,
            'realized_net_pnl': -1.25 - idx * 0.1,
            'realized_pnl_before_fees': -1.0,
            'currency': 'USDT',
            'events': [
                {'kind': 'entry', 'quantity': 1.0, 'price': 2000.0 + idx},
                {'kind': 'close', 'quantity': 1.0, 'price': 1998.0, 'reason': f'止损触发第{idx + 1}次'},
            ],
        })

    monkeypatch.setattr('backend.utils.task_performance.task_history_context', lambda *args: '汇总统计块')
    monkeypatch.setattr(
        'backend.app.services.position_history_service.read_position_history',
        lambda *args, **kwargs: {'positions': positions},
    )

    text = memory_service.format_recent_position_history_for_memory('task-1', {'mode': 'REAL', 'symbol': 'ETH/USDT'})
    assert '汇总统计块' in text
    assert '最近连续亏损 4 笔' in text
    assert 'ETH/USDT:USDT 多' in text
    assert 'ETH/USDT:USDT 空' in text
    assert '净盈亏 -1.25 USDT' in text
    assert '止损触发第1次' in text
    assert '最近7天已平仓持仓明细' in memory_agent.MEMORY_REVIEW_POLICY
