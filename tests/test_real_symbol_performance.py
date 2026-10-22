"""REAL uses the account's symbol fills; simulations remain task-specific."""
import json
from datetime import datetime, timedelta

import pytest

from backend import database
from backend.agent.memory_service import format_recent_position_history_for_memory
from backend.utils.execution_ledger import recent_activity_data, closed_positions_from_activity
from backend.utils.performance_context import performance_data


@pytest.fixture
def account(pg, monkeypatch):
    monkeypatch.setattr('backend.utils.trade_evidence.configured_account_scope', lambda _: 'account')
    return datetime.now(database.TZ_CN)


def add_fill(now, name, days, *, owner=None, scope='account', symbol='ETH/USDT:USDT',
             side='buy', price=100, pnl=0, fees=None):
    stamp = int((now - timedelta(days=days)).timestamp() * 1000)
    payload = dict(id=name, timestamp=stamp, side=side, position_side='LONG', price=price,
                   amount=1, base_amount=1, realized_pnl=pnl,
                   fees=[dict(currency='USDT', cost=.1)] if fees is None else fees)
    with database.get_db_conn() as conn:
        conn.execute('INSERT INTO execution_fills(account_scope,symbol,trade_id,order_id,config_id,timestamp_ms,payload) '
                     'VALUES(%s,%s,%s,%s,%s,%s,%s)',
                     (scope, symbol, name, name, owner, stamp, json.dumps(payload)))
        conn.commit()


def seed_cycle(scope, owner, symbol, opened, closed, *, name='cycle', side='LONG',
               entry_price=100, close_price=103, amount=1, pnl=3, fee_each=.1,
               take_profit=None, stop_loss=None):
    from backend.utils.execution_ledger import register_order
    for suffix, role, stamp, price, realized in [
        ('entry', 'entry', opened, entry_price, 0),
        ('close', 'agent_exit', closed, close_price, pnl),
    ]:
        oid = f'{name}-{suffix}'
        register_order(scope, symbol, oid, owner, role, side=side,
                       take_profit=take_profit, stop_loss=stop_loss)
        payload = dict(id=oid, timestamp=stamp, position_side=side, price=price,
                       side='buy' if (suffix == 'entry') == (side == 'LONG') else 'sell',
                       amount=amount, base_amount=amount, realized_pnl=realized,
                       fees=[dict(currency=symbol.split(':')[-1], cost=fee_each)])
        with database.get_db_conn() as conn:
            conn.execute('INSERT INTO execution_fills(account_scope,symbol,trade_id,order_id,config_id,timestamp_ms,payload) '
                         'VALUES(%s,%s,%s,%s,%s,%s,%s)',
                         (scope, symbol, oid, oid, owner, stamp, json.dumps(payload)))
            conn.commit()


def test_eth_history_includes_manual_and_other_tasks_without_waiting_for_index(account):
    now = account
    add_fill(now, 'old-entry', 8)
    add_fill(now, 'manual-close', 6, owner='other-task', side='sell', price=110, pnl=10)
    add_fill(now, 'entry', 2, owner='cfg')
    add_fill(now, 'close', 1, side='sell', price=95, pnl=-5)
    add_fill(now, 'other-account', 1, scope='foreign', pnl=999)
    add_fill(now, 'other-symbol', 1, symbol='ETH/USDC:USDC', pnl=999)
    add_fill(now, 'future', -1, pnl=999)
    with database.get_db_conn() as conn:
        conn.execute("INSERT INTO execution_plan_index_runs(index_id,account_scope,symbol,config_id,phase,cursor) "
                     "VALUES('pending','account','ETH/USDT:USDT','cfg','publish',0)")
        conn.commit()
    data = performance_data('cfg', 'ETH/USDT', 'REAL', now=now, include_details=True)
    ledger = data['details']['ledger']
    assert ledger['fill_count'] == 3 and ledger['unknown_owner_fill_count'] == 1
    assert not ledger['ownership_index_complete'] and not ledger['window_complete']
    assert data['profit']['scope'] == 'account_symbol'
    assert data['profit']['available'] and data['profit']['amount'] == pytest.approx(4.7)
    closed = data['details']['closed_positions']
    assert len(closed) == 2
    assert [p['realized_pnl'] for p in closed] == [-5, 10]
    assert [p['net_realized_pnl'] for p in closed] == pytest.approx([-5.2, 9.8])
    assert len(database.get_closed_positions_7d('cfg', 'ETH/USDT', mode='REAL')) == 2
    memory = format_recent_position_history_for_memory('cfg', dict(symbol='ETH/USDT', mode='REAL'))
    assert '完整平仓周期: 2 笔' in memory and '胜率: 50.0%' in memory
    assert '+4.70 USDT' in memory and '不是单个Agent战绩' in memory
    assert '同步尚未完整覆盖' in memory
    # Two task views of the same real account/symbol show the same statistics.
    other = performance_data('another-task', 'ETH/USDT', 'REAL', now=now, include_details=True)
    assert other['profit']['amount'] == data['profit']['amount']
    assert other['details']['closed_positions'] == closed


@pytest.mark.parametrize('missing', ['pnl', 'fee', 'currency'])
def test_missing_facts_in_any_included_fill_do_not_become_zero(account, missing):
    values = dict(pnl=None) if missing == 'pnl' else dict(fees=[] if missing == 'fee' else [dict(currency='BTC', cost=.01)])
    add_fill(account, 'unattributed', 1, **values)
    data = performance_data('cfg', 'ETH/USDT', 'REAL', now=account)
    assert not data['profit']['available'] and data['profit']['amount'] is None


def test_empty_account_history_is_unknown_but_explicit_zero_is_valid(account):
    assert performance_data('cfg', 'ETH/USDT', 'REAL', now=account)['profit']['amount'] is None
    add_fill(account, 'zero', 1, pnl=0, fees=[dict(currency='USDT', cost=0)])
    data = performance_data('cfg', 'ETH/USDT', 'REAL', now=account)
    assert data['profit']['available'] and data['profit']['amount'] == 0


def test_cycle_with_missing_entry_pnl_keeps_profit_unknown(account):
    add_fill(account, 'entry', 2, pnl=None)
    add_fill(account, 'exit', 1, side='sell', price=110, pnl=10)
    data = performance_data('cfg', 'ETH/USDT', 'REAL', now=account, include_details=True)
    assert data['profit']['amount'] is None
    closed = data['details']['closed_positions']
    assert len(closed) == 1
    assert closed[0]['realized_pnl'] is None and closed[0]['net_realized_pnl'] is None
    summary = database.format_closed_positions_summary(closed)
    assert '1笔盈亏未知' in summary and '胜率: -' in summary


def test_strategy_tasks_remain_separate(account):
    stamp = (account-timedelta(days=1)).strftime('%Y-%m-%d %H:%M:%S')
    with database.get_db_conn() as conn:
        for owner, pnl in [('cfg', 5), ('other', 99)]:
            conn.execute('INSERT INTO mock_orders(order_id,config_id,symbol,status,close_time,realized_pnl) '
                         "VALUES(%s,%s,'ETH/USDT','CLOSED',%s,%s)", (owner, owner, stamp, pnl))
        conn.commit()
    assert performance_data('cfg', 'ETH/USDT', 'STRATEGY', now=account)['profit']['amount'] == 5
    assert performance_data('other', 'ETH/USDT', 'STRATEGY', now=account)['profit']['amount'] == 99


def test_scheduler_syncs_account_fills_before_incomplete_attribution(monkeypatch):
    from types import SimpleNamespace
    from backend.app.core import scheduler
    calls = []
    class Ledger:
        scope, symbol = 'account', 'ETH/USDT:USDT'
        index_state = dict(complete=False)
        def __init__(self, *args):
            pass
        def sync(self, *, require_index):
            assert require_index is False
            calls.append('sync')
            return {}
        def index_saved_plans(self):
            calls.append('index')
    monkeypatch.setattr(scheduler, 'MarketTool', lambda **_: SimpleNamespace(exchange=object()))
    monkeypatch.setattr('backend.utils.execution_ledger.ExecutionLedger', Ledger)
    monkeypatch.setattr('backend.utils.execution_stream.ensure_stream', lambda *a, **k: None)
    monkeypatch.setattr('backend.utils.execution_metrics.update_post_exit', lambda *a: None)
    scheduler._sync_execution(dict(config_id='cfg', mode='REAL', symbol='ETH/USDT:USDT'))
    assert calls == ['sync', 'index']
