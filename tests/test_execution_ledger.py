import json
from datetime import datetime
from unittest.mock import patch

import pytest

import backend.database as database
from backend.database_schema import initialize_schema
from backend.utils.execution_ledger import ExecutionLedger, execution_context, register_order


class Exchange:
    id = 'binance'
    apiKey = 'account-a'

    def __init__(self, trades):
        self.trades = trades
        self.fail = False

    def load_markets(self):
        pass

    def market(self, symbol):
        return dict(symbol='ETH/USDT:USDT', contract=True, swap=True, linear=True)

    def fetch_my_trades(self, symbol, since, limit, params):
        if self.fail:
            raise RuntimeError('network down')
        return [r for r in self.trades if since <= r['timestamp'] <= params['until']][:limit]


@pytest.fixture
def local_db(tmp_path):
    with patch.object(database, 'DB_NAME', str(tmp_path / 'ledger.db')):
        with database.get_db_conn() as conn:
            initialize_schema(conn)
        yield


def fill(i, **kwargs):
    return dict(id=str(i), order='entry', timestamp=100000 + i, price=100, amount=.1,
                side='buy', cost=10, **kwargs)


def test_pages_more_than_ten_fills_and_restart_overlap_are_idempotent(local_db):
    ex = Exchange([fill(i) for i in range(2100)])
    ledger = ExecutionLedger(ex, 'cfg', 'ETH/USDT')
    assert ledger.sync(now_ms=200000, start_ms=100000)['complete']
    restarted = ExecutionLedger(ex, 'cfg', 'ETH/USDT')
    assert restarted.sync(now_ms=250000, min_interval=0)['complete']
    with database.get_db_conn() as conn:
        assert conn.execute('SELECT COUNT(*) FROM execution_fills').fetchone()[0] == 2100


def test_account_key_isolated_missing_pnl_not_zero_and_late_ownership(local_db):
    ex = Exchange([fill(1)])
    a = ExecutionLedger(ex, 'cfg', 'ETH/USDT')
    a.sync(now_ms=200000, start_ms=100000)
    ex.apiKey = 'account-b'
    b = ExecutionLedger(ex, 'other', 'ETH/USDT')
    b.sync(now_ms=200000, start_ms=100000)
    register_order(a.scope, a.symbol, 'entry', 'cfg', 'entry')
    with database.get_db_conn() as conn:
        rows = conn.execute('SELECT config_id,payload FROM execution_fills ORDER BY config_id').fetchall()
    assert len(rows) == 2
    assert [r['config_id'] for r in rows] == [None, 'cfg']
    assert json.loads(rows[1]['payload'])['realized_pnl'] is None


def test_failed_sync_does_not_advance_checkpoint(local_db):
    ex = Exchange([fill(1)])
    ledger = ExecutionLedger(ex, 'cfg', 'ETH/USDT')
    first = ledger.sync(now_ms=200000, start_ms=100000)
    ex.fail = True
    failed = ledger.sync(now_ms=300000, min_interval=0)
    assert not failed['complete'] and failed['through_ms'] == first['through_ms']
    ex.fail = False
    assert ledger.sync(now_ms=400000, min_interval=0)['complete']


def test_timestamp_overflow_is_incomplete(local_db):
    ex = Exchange([{**fill(i), 'timestamp': 100000} for i in range(1001)])
    result = ExecutionLedger(ex, 'cfg', 'ETH/USDT').sync(now_ms=100000, start_ms=100000)
    assert not result['complete'] and 'millisecond' in result['error']
    assert 'through_ms' not in result


def test_future_range_cannot_be_marked_complete(local_db):
    ledger = ExecutionLedger(Exchange([]), 'cfg', 'ETH/USDT')
    with patch('backend.utils.execution_ledger.time.time', return_value=200):
        result = ledger.sync(now_ms=300000, start_ms=100000)
    assert result['through_ms'] == 200000


def test_long_outage_retains_verified_chunks_for_next_attempt(local_db):
    ledger = ExecutionLedger(Exchange([]), 'cfg', 'ETH/USDT')
    day = 86400000
    result = ledger.sync(now_ms=day * 3, start_ms=0, max_calls=1)
    assert not result['complete'] and result['through_ms'] == day - 1
    result = ledger.sync(now_ms=day * 3, min_interval=0)
    assert result['complete'] and result['covered_intervals'] == [[0, day * 3]]


def test_existing_one_day_checkpoint_is_upgraded_to_seven_days(local_db):
    day = 86400000
    ledger = ExecutionLedger(Exchange([]), 'cfg', 'ETH/USDT')
    ledger._status({
        'attempted_at': 8 * day,
        'from_ms': 7 * day,
        'through_ms': 8 * day,
        'covered_intervals': [[7 * day, 8 * day]],
        'complete': True,
    })

    result = ledger.sync(now_ms=8 * day, min_interval=0)

    assert result['complete']
    assert result['covered_intervals'] == [[day, 8 * day]]


def test_ownership_reindex_preserves_execution_evidence(local_db):
    ledger = ExecutionLedger(Exchange([]), 'cfg', 'ETH/USDT')
    register_order(ledger.scope, ledger.symbol, 'entry', 'cfg', 'entry', side='LONG', planned_entry=100)
    register_order(ledger.scope, ledger.symbol, 'entry', 'cfg', 'entry', side='LONG')
    with database.get_db_conn() as conn:
        value = json.loads(conn.execute('SELECT payload FROM execution_order_links').fetchone()[0])
    assert value['planned_entry'] == 100


def test_spot_cannot_enter_futures_ledger(local_db):
    ex = Exchange([])
    ex.market = lambda symbol: dict(symbol=symbol, spot=True)
    with pytest.raises(ValueError, match='perpetual'):
        ExecutionLedger(ex, 'spot', 'ETH/USDT')


def test_context_all_totals_bounded_details_and_unknown_ownership_excluded(local_db):
    ex = Exchange([fill(i, fee={'cost': .01, 'currency': 'USDT'}, info={'realizedPnl': '1'}) for i in range(30)])
    ledger = ExecutionLedger(ex, 'cfg', 'ETH/USDT')
    register_order(ledger.scope, ledger.symbol, 'entry', 'cfg', 'stop_loss', trigger_price=100)
    ledger.sync(now_ms=200000, start_ms=100000)
    text = execution_context('cfg', now=datetime.fromtimestamp(200), scope=ledger.scope)
    data = json.loads(text[text.index('{'):])
    assert data['fill_count'] == 30 and len(data['recent_fills']) == 20
    assert data['known_realized_pnl_before_fees'] == 30
    assert data['fees_by_currency']['USDT'] == pytest.approx(.3)
    assert data['recent_fills'][0]['role'] == 'stop_loss'


def test_disjoint_backfill_does_not_claim_gap_covered(local_db):
    ledger = ExecutionLedger(Exchange([]), 'cfg', 'ETH/USDT')
    ledger.sync(now_ms=200000, start_ms=100000)
    status = ledger.sync(now_ms=50000, start_ms=0, min_interval=0)
    assert status['covered_intervals'] == [[0, 50000], [100000, 200000]]
    assert status['from_ms'] == 100000


def test_futures_daily_scope_ownership_and_missing_values(local_db):
    from backend.utils.trade_review import daily_exchange_evidence
    start = int(database.TZ_CN.localize(datetime(2026, 9, 1)).timestamp() * 1000)
    ex = Exchange([{**fill(1), 'timestamp': start + 1000}, {**fill(2), 'timestamp': start - 1}])
    ledger = ExecutionLedger(ex, 'cfg', 'ETH/USDT')
    register_order(ledger.scope, ledger.symbol, 'entry', 'cfg', 'entry', side='LONG')
    with patch('backend.utils.market_data.MarketTool', return_value=type('MT', (), {'exchange': ex})()):
        text = daily_exchange_evidence({'mode': 'REAL', 'config_id': 'cfg', 'symbol': 'ETH/USDT'}, '2026-09-01')
    data = json.loads(text[text.index('{'):])
    assert data['window_complete']
    assert data['fill_count'] == 1
    assert data['recent_fills'][0]['id'] == '1'
    assert data['recent_fills'][0]['realized_pnl'] is None
    assert not data['sync'][0]['income_sync']['complete']  # Fake has no income API; not invented as zero.


def test_partial_fills_reconstruct_one_cycle_without_counting_fills_as_trades(local_db):
    from backend.utils.execution_ledger import position_cycles
    ex = Exchange([])
    ledger = ExecutionLedger(ex, 'cfg', 'ETH/USDT')
    register_order(ledger.scope, ledger.symbol, 'entry', 'cfg', 'entry', side='LONG')
    register_order(ledger.scope, ledger.symbol, 'close', 'cfg', 'stop_loss', side='LONG')
    ledger.ingest([fill(1), fill(2), {**fill(3), 'order': 'close', 'side': 'sell', 'amount': .2, 'price': 90}])
    result = position_cycles('cfg', 0, 200000, ledger.scope)
    assert result['completed_count'] == 1
    assert result['completed'][0]['entry_vwap'] == 100
    assert result['completed'][0]['exit_vwap'] == 90
    assert result['completed'][0]['missing_pnl']


@pytest.mark.parametrize('exit_role', ['take_profit', 'stop_loss', 'agent_exit'])
@pytest.mark.parametrize('add', [False, True])
def test_cycle_scenarios_with_partial_close_and_add(local_db, exit_role, add):
    from backend.utils.execution_ledger import position_cycles
    ledger = ExecutionLedger(Exchange([]), 'cfg', 'ETH/USDT')
    for oid, role in [('entry', 'entry'), ('add', 'entry'), ('reduce', 'agent_exit'), ('close', exit_role)]:
        register_order(ledger.scope, ledger.symbol, oid, 'cfg', role, side='LONG')
    trades = [fill(1), {**fill(2), 'order': 'reduce', 'side': 'sell', 'amount': .04}]
    if add:
        trades.append({**fill(3), 'order': 'add'})
    trades.append({**fill(4), 'order': 'close', 'side': 'sell', 'amount': .16 if add else .06})
    ledger.ingest(trades)
    cycle = position_cycles('cfg', 0, 200000)['completed'][0]
    assert cycle['add_count'] == int(add)
    assert cycle['exit_count'] == 2
    assert cycle['final_exit_reason'] == exit_role
    assert cycle['remaining_base'] == 0


def test_unknown_close_ends_cycle_without_claiming_tp_or_poisoning_next(local_db):
    from backend.utils.execution_ledger import position_cycles
    ledger = ExecutionLedger(Exchange([]), 'cfg', 'ETH/USDT')
    register_order(ledger.scope, ledger.symbol, 'entry', 'cfg', 'entry', side='LONG', episode_id='same-plan')
    register_order(ledger.scope, ledger.symbol, 'close', 'cfg', 'agent_exit', side='LONG')
    ledger.ingest([fill(1), {**fill(2), 'order': 'external', 'side': 'sell', 'info': {'positionSide': 'LONG'}},
                   fill(3), {**fill(4), 'order': 'close', 'side': 'sell'}])
    result = position_cycles('cfg', 0, 200000)
    assert result['completed_count'] == 2
    assert result['completed'][0]['exit_reasons'] == ['external_unknown_exit']
    assert len({c['episode_id'] for c in result['completed']}) == 2
    assert result['open_cycles'] == []


def test_mixed_unowned_entry_is_not_attributed_to_agent(local_db):
    from backend.utils.execution_ledger import position_cycles
    ledger = ExecutionLedger(Exchange([]), 'cfg', 'ETH/USDT')
    register_order(ledger.scope, ledger.symbol, 'entry', 'cfg', 'entry', side='LONG')
    register_order(ledger.scope, ledger.symbol, 'close', 'cfg', 'take_profit', side='LONG')
    ledger.ingest([fill(1), {**fill(2), 'order': 'manual', 'info': {'positionSide': 'LONG'}},
                   {**fill(3), 'order': 'close', 'side': 'sell', 'amount': .2}])
    result = position_cycles('cfg', 0, 200000)
    assert result['completed_count'] == 0
    assert result['excluded_cycle_count'] == 1


def test_external_only_ingest_rebuilds_existing_owned_cycle(local_db):
    from backend.utils.execution_ledger import position_cycles
    ledger = ExecutionLedger(Exchange([]), 'cfg', 'ETH/USDT')
    register_order(ledger.scope, ledger.symbol, 'entry', 'cfg', 'entry', side='LONG')
    ledger.ingest([fill(1)])
    ledger.ingest([{**fill(2), 'order': 'manual', 'side': 'sell', 'info': {'positionSide': 'LONG'}}])
    with database.get_db_conn() as conn:
        assert conn.execute('SELECT COUNT(*) FROM execution_position_history').fetchone()[0] == 1


def test_completed_cycle_is_materialized_with_prices_protection_and_pnl(local_db):
    ex = Exchange([])
    ledger = ExecutionLedger(ex, 'cfg', 'ETH/USDT')
    register_order(
        ledger.scope,
        ledger.symbol,
        'entry',
        'cfg',
        'entry',
        side='LONG',
        episode_id='episode-1',
        take_profit=110,
        stop_loss=95,
    )
    register_order(
        ledger.scope,
        ledger.symbol,
        'close',
        'cfg',
        'take_profit',
        side='LONG',
        episode_id='episode-1',
        trigger_price=110,
    )
    entry = fill(1, fee={'cost': .01, 'currency': 'USDT'}, info={'realizedPnl': '0'})
    close = {
        **fill(2, fee={'cost': .011, 'currency': 'USDT'}, info={'realizedPnl': '1'}),
        'order': 'close',
        'side': 'sell',
        'price': 110,
    }

    ledger.ingest([entry, close])

    with database.get_db_conn() as conn:
        row = conn.execute('SELECT * FROM execution_position_history').fetchone()
    assert json.loads(row['payload'])['plan_episode_id'] == 'episode-1'
    assert len(row['position_id']) == 64  # Identity follows first fill, not a reusable plan.
    assert row['entry_price'] == 100
    assert row['close_price'] == 110
    assert row['amount'] == pytest.approx(.1)
    assert row['take_profit'] == 110
    assert row['stop_loss'] == 95
    assert row['realized_pnl'] == 1
    assert json.loads(row['fees_json'])['USDT'] == pytest.approx(.021)
    assert row['exit_reason'] == 'take_profit'


def test_stream_missing_fields_do_not_erase_rest_pnl(local_db):
    ledger = ExecutionLedger(Exchange([]), 'cfg', 'ETH/USDT')
    ledger.ingest([fill(1, info={'realizedPnl': '3'})])
    ledger.ingest([fill(1)])
    with database.get_db_conn() as conn:
        value = json.loads(conn.execute('SELECT payload FROM execution_fills').fetchone()[0])
    assert value['realized_pnl'] == 3


@pytest.mark.parametrize('currency,missing,expected', [('USDT', False, -7.82), ('BNB', False, None), ('USDT', True, None)])
def test_history_net_pnl_deducts_both_fees_once(local_db, currency, missing, expected):
    from datetime import datetime
    ledger = ExecutionLedger(Exchange([]), 'cfg', 'ETH/USDT')
    register_order(ledger.scope, ledger.symbol, 'entry', 'cfg', 'entry', side='LONG')
    register_order(ledger.scope, ledger.symbol, 'close', 'cfg', 'stop_loss', side='LONG')
    now = datetime.now(database.TZ_CN)
    timestamp = int(now.timestamp() * 1000) - 10000
    trades = [
        {**fill(1), 'timestamp': timestamp, 'realizedPnl': 0, 'fee': None if missing else {'currency': currency, 'cost': .3}},
        {**fill(2), 'timestamp': timestamp + 1000, 'order': 'close', 'side': 'sell', 'realizedPnl': -7.124, 'fee': {'currency': currency, 'cost': .396}},
    ]
    ledger.ingest(trades)
    ledger.ingest(trades)
    rows = database.get_closed_positions_7d('cfg', 'ETH/USDT', mode='REAL')
    assert len(rows) == 1
    assert rows[0]['realized_pnl'] == -7.124
    if expected is None:
        assert rows[0]['net_realized_pnl'] is None
    else:
        assert rows[0]['net_realized_pnl'] == pytest.approx(expected)
