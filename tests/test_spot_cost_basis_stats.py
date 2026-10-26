"""Cost calibration preserves covered history and counts only later fills."""
import json
from types import SimpleNamespace

import pytest

from backend import database
from backend.app.services import spot_dashboard_service as service
from backend.database.spot_cost_basis import read_spot_buy_totals


@pytest.fixture
def spot(pg, monkeypatch):
    cfg = {
        'config_id': 'spot', 'mode': 'SPOT_DCA', 'symbol': 'BTC/USDT',
        'symbols': ['BTC/USDT'], 'dca_amount': 100,
    }
    exchange_orders = {}
    exchange = SimpleNamespace(
        fetch_order=lambda order_id, symbol: exchange_orders[(order_id, symbol)],
        fetch_my_trades=lambda *args, **kwargs: [],
        fetch_balance=lambda: {'BTC': {'total': 9}, 'ETH': {'total': 10}},
        fetch_ticker=lambda symbol: {'last': 120 if symbol == 'BTC/USDT' else 30},
    )
    monkeypatch.setattr('backend.utils.spot_execution.reconcile_spot_reservations', lambda *args: None)
    snapshots = []
    dependencies = service.SpotStatisticsDependencies(
        connection_factory=pg, market_factory=lambda **kwargs: SimpleNamespace(exchange=exchange),
        fill_writer=database.upsert_spot_order_fill, trade_writer=lambda *args, **kwargs: None,
        snapshot_writer=lambda *args: snapshots.append(args), cache={}, cache_ttl=3600,
    )

    def add_order(order_id, quantity, cost, *, symbol='BTC/USDT', status='FILLED', config_id='spot'):
        with pg() as conn:
            conn.execute('''INSERT INTO orders
                (order_id,config_id,symbol,trade_mode,status,side,filled_amount,filled_cost,avg_fill_price)
                VALUES (%s,%s,%s,'SPOT_DCA',%s,'BUY',%s,%s,%s)''',
                (order_id, config_id, symbol, status, quantity, cost, cost / quantity if quantity else 0))
            conn.commit()
        exchange_orders[(order_id, symbol)] = {
            'status': 'open' if status in {'OPEN', 'PARTIAL'} else 'closed',
            'filled': quantity, 'cost': cost, 'average': cost / quantity if quantity else 0,
        }

    revisions = []

    def calibrate(quantity, avg_cost, *, symbol='BTC/USDT', config_id='spot'):
        revisions.append(len(revisions) + 1)
        updated_at = f'2026-10-26T10:00:00.{revisions[-1]:06d}+00:00'
        with pg() as conn:
            totals = read_spot_buy_totals(conn, config_id, symbol)
            baselines = {key: {field: row[field] for field in ('qty', 'cost', 'status')}
                         for key, row in totals.items()}
            conn.execute('''INSERT INTO spot_cost_basis
                (config_id,symbol,quantity,avg_cost,order_baselines_json,updated_at)
                VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT(config_id,symbol) DO UPDATE SET quantity=excluded.quantity,
                    avg_cost=excluded.avg_cost,order_baselines_json=excluded.order_baselines_json,
                    updated_at=excluded.updated_at''',
                (config_id, symbol, quantity, avg_cost, json.dumps(baselines), updated_at))
            conn.commit()
        return updated_at

    def stats(symbol='BTC/USDT', *, force=True):
        return service.calculate_spot_symbol_statistics('spot', cfg, symbol, force, dependencies=dependencies)

    return SimpleNamespace(
        cfg=cfg, connect=pg, add_order=add_order, calibrate=calibrate, stats=stats,
        exchange=exchange, exchange_orders=exchange_orders, dependencies=dependencies, snapshots=snapshots,
    )


def test_legacy_initial_holding_and_all_history_remain_unchanged_without_calibration(spot):
    spot.cfg.update(initial_qty=1, manual_avg_cost=100)
    spot.add_order('old', 0.5, 60)
    result = spot.stats()
    assert result['total_qty'] == 1.5
    assert result['total_invested'] == 160
    assert result['return_pct'] == 12.5
    assert result['cost_basis'] is None
    assert result['subsequent_buy_qty'] is None
    assert result['stats_source'] == 'manual_and_trades'


def test_calibration_replaces_old_buys_and_legacy_then_adds_new_buy_once(spot):
    spot.cfg.update(initial_qty=7, manual_avg_cost=90)
    spot.add_order('old', 0.5, 60)
    revision = spot.calibrate(1, 100)
    first = spot.stats()
    assert first['total_qty'] == 1
    assert first['total_invested'] == 100
    assert first['cost_basis'] == {'quantity': 1, 'avg_cost': 100, 'updated_at': revision}
    spot.add_order('new', 0.2, 24)
    result = spot.stats()
    assert result['total_qty'] == 1.2
    assert result['total_invested'] == 124
    assert result['avg_cost'] == 103.3333
    assert result['unrealized_pnl'] == 20
    assert result['return_pct'] == 16.13
    assert result['recorded_buy_qty'] == 0.7
    assert result['recorded_buy_cost'] == 84
    assert result['subsequent_buy_qty'] == 0.2
    assert result['subsequent_buy_cost'] == 24
    assert spot.stats()['total_invested'] == 124


def test_old_partial_order_counts_only_its_later_fill(spot):
    spot.add_order('partial', 0.2, 20, status='PARTIAL')
    spot.calibrate(1, 100)
    spot.exchange_orders[('partial', 'BTC/USDT')] = {
        'status': 'closed', 'filled': 0.5, 'cost': 56, 'average': 112,
    }
    result = spot.stats()
    assert result['total_qty'] == 1.3
    assert result['total_invested'] == 136
    assert result['subsequent_buy_qty'] == 0.3
    assert result['subsequent_buy_cost'] == 36
    assert spot.stats()['total_invested'] == 136


def test_old_pending_order_with_zero_fill_can_fill_after_calibration(spot):
    spot.add_order('open', 0, 0, status='OPEN')
    spot.calibrate(1, 100)
    spot.exchange_orders[('open', 'BTC/USDT')] = {
        'status': 'closed', 'filled': 0.2, 'cost': 24, 'average': 120,
    }
    result = spot.stats()
    assert result['total_qty'] == 1.2
    assert result['total_invested'] == 124


@pytest.mark.parametrize('status', ['FILLED', 'CLOSED', 'CANCELLED', 'CANCELED', 'EXPIRED', 'REJECTED'])
def test_terminal_order_history_backfill_never_changes_manual_holding(spot, status):
    spot.add_order('old', 0.5, 60, status=status)
    spot.calibrate(1, 100)
    with spot.connect() as conn:
        conn.execute('''INSERT INTO trade_history
            (trade_id,order_id,config_id,symbol,side,amount,cost)
            VALUES ('late','old','spot','BTC/USDT','buy',0.8,96)''')
        conn.commit()
    result = spot.stats()
    assert result['total_qty'] == 1
    assert result['total_invested'] == 100
    assert result['recorded_buy_qty'] == 0.8
    assert result['recorded_buy_cost'] == 96


def test_partial_order_cost_correction_without_new_quantity_is_not_a_new_buy(spot):
    spot.add_order('partial', 0.2, 20, status='PARTIAL')
    spot.calibrate(1, 100)
    spot.exchange_orders[('partial', 'BTC/USDT')] = {
        'status': 'open', 'filled': 0.2, 'cost': 24, 'average': 120,
    }
    result = spot.stats()
    assert result['total_qty'] == 1
    assert result['total_invested'] == 100
    assert result['subsequent_buy_cost'] == 0


def test_later_quantity_with_unconfirmed_added_cost_is_unknown(spot):
    spot.add_order('partial', 0.2, 20, status='PARTIAL')
    spot.calibrate(1, 100)
    spot.exchange_orders[('partial', 'BTC/USDT')] = {
        'status': 'open', 'filled': 0.4, 'cost': 20, 'average': 50,
    }
    assert spot.stats() is None
    assert spot.snapshots == []


def test_partial_cumulative_quantity_regression_does_not_subtract_manual_holding(spot):
    spot.add_order('partial', 0.2, 20, status='PARTIAL')
    spot.calibrate(1, 100)
    spot.exchange_orders[('partial', 'BTC/USDT')] = {
        'status': 'open', 'filled': 0.1, 'cost': 10, 'average': 100,
    }
    result = spot.stats()
    assert result['total_qty'] == 1
    assert result['total_invested'] == 100


def test_recalibration_including_zero_is_not_ignored_by_cache_or_legacy_fields(spot):
    spot.cfg.update(initial_qty=7, manual_avg_cost=90)
    spot.add_order('old', 0.5, 60)
    spot.calibrate(1, 100)
    assert spot.stats(force=False)['total_invested'] == 100
    spot.add_order('new', 0.2, 24)
    spot.calibrate(0.8, 110)
    result = spot.stats(force=False)
    assert result['total_qty'] == 0.8
    assert result['total_invested'] == 88
    assert result['subsequent_buy_qty'] == 0
    spot.calibrate(0, 0)
    empty = spot.stats(force=False)
    assert empty['total_qty'] == empty['total_invested'] == empty['avg_cost'] == 0
    assert empty['cost_basis']['quantity'] == 0


def test_cost_basis_is_scoped_by_task_and_symbol(spot):
    spot.cfg['symbols'] = ['BTC/USDT', 'ETH/USDT']
    spot.add_order('same-id', 0.5, 60)
    spot.add_order('same-id', 2, 50, symbol='ETH/USDT')
    spot.add_order('same-id', 99, 9900, config_id='other')
    spot.calibrate(1, 100)
    spot.calibrate(3, 20, symbol='ETH/USDT')
    spot.calibrate(100, 1000, config_id='other')
    spot.add_order('eth-new', 1, 30, symbol='ETH/USDT')
    btc = spot.stats()
    eth = spot.stats('ETH/USDT')
    assert btc['total_qty'] == 1 and btc['total_invested'] == 100
    assert btc['recorded_buy_qty'] == 0.5
    assert eth['total_qty'] == 4 and eth['total_invested'] == 90
    assert eth['recorded_buy_qty'] == 3


def test_calibration_form_values_preserve_small_holdings_and_price_precision(spot):
    spot.calibrate(0.000000123456789, 0.0123456789)
    result = spot.stats()
    assert result['cost_basis_current_quantity'] == 0.000000123456789
    assert result['cost_basis_current_avg_cost'] == pytest.approx(0.0123456789)


def test_trade_history_and_duplicate_order_logs_never_duplicate_new_buy(spot):
    spot.calibrate(1, 100)
    spot.add_order('new', 0.2, 24)
    spot.add_order('new', 0.2, 24)
    with spot.connect() as conn:
        conn.execute('''INSERT INTO trade_history
            (trade_id,order_id,config_id,symbol,side,amount,cost)
            VALUES ('trade','new','spot','BTC/USDT','buy',0.2,24),
                   ('foreign','new','other','BTC/USDT','buy',100,10000)''')
        conn.commit()
    result = spot.stats()
    assert result['total_qty'] == 1.2
    assert result['total_invested'] == 124
    assert result['buy_count'] == 1


def test_calibration_sync_includes_unknown_empty_status_and_more_than_100_orders(spot):
    for index in range(101):
        spot.add_order(f'pending-{index}', 0, 0, status='UNKNOWN' if index else None)
    spot.add_order('done', 1, 100)
    calls = []

    def fetch_order(order_id, symbol):
        calls.append(order_id)
        return {'status': 'closed', 'filled': 0.1, 'cost': 10, 'average': 100}

    spot.exchange.fetch_order = fetch_order
    errors = []
    service._synchronize_orders(
        'spot', 'BTC/USDT', SimpleNamespace(exchange=spot.exchange), errors,
        spot.dependencies, for_cost_basis=True,
    )
    assert errors == []
    assert len(calls) == 101 and 'done' not in calls
    with spot.connect() as conn:
        totals = read_spot_buy_totals(conn, 'spot', 'BTC/USDT')
    assert totals['pending-0']['qty'] == 0.1
    assert totals['pending-100']['status'] == 'FILLED'
