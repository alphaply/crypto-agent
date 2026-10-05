import sqlite3

import pytest

from backend import database
from backend.database_schema import initialize_schema
from backend.utils.spot_config_guard import SpotConfigConflict, assert_spot_config_change_allowed
from backend.utils.spot_execution import _initialize


@pytest.fixture
def spot_config(tmp_path, monkeypatch):
    path = tmp_path / 'guard.db'
    with sqlite3.connect(path) as conn:
        initialize_schema(conn)
        _initialize(conn)
    monkeypatch.setattr(database, 'DB_NAME', str(path))
    return {'config_id': 'spot', 'mode': 'SPOT_DCA', 'symbol': 'BTC/USDT',
            'symbols': ['BTC/USDT', 'ETH/USDT'], 'exchange_profile_id': 'a'}


def test_empty_task_can_change_symbols_and_account(spot_config):
    assert_spot_config_change_allowed(spot_config, {**spot_config, 'symbols': ['BTC/USDT'], 'exchange_profile_id': 'b'})


@pytest.mark.parametrize('status', ['OPEN', 'PARTIAL', 'FILLED', 'CANCELLED'])
def test_unverified_or_invested_orders_cannot_be_orphaned(spot_config, status):
    database.save_order_log('owned', 'ETH/USDT', 'spot', 'buy', 10, 0, 0, '',
                            trade_mode='SPOT_DCA', config_id='spot', amount=2, status=status)
    with pytest.raises(SpotConfigConflict):
        assert_spot_config_change_allowed(spot_config, {**spot_config, 'symbols': ['BTC/USDT']})
    with pytest.raises(SpotConfigConflict):
        assert_spot_config_change_allowed(spot_config, {**spot_config, 'exchange_profile_id': 'b'})
    with pytest.raises(SpotConfigConflict):
        assert_spot_config_change_allowed(spot_config, {'config_id': 'spot', 'mode': 'DELETED'})
    assert_spot_config_change_allowed(spot_config, {**spot_config, 'symbols': ['BTC/USDT', 'ETH/USDT', 'SOL/USDT']})


def test_verified_empty_cancellation_can_be_removed(spot_config):
    database.save_order_log('owned', 'ETH/USDT', 'spot', 'buy', 10, 0, 0, '',
                            trade_mode='SPOT_DCA', config_id='spot', amount=2, status='CANCELLED')
    database.upsert_spot_order_fill('owned', 'spot', 'ETH/USDT', 'CANCELLED', 0, 0, 0)
    assert_spot_config_change_allowed(spot_config, {**spot_config, 'symbols': ['BTC/USDT']})


def test_unknown_reservation_without_order_log_blocks_removal(spot_config):
    with database.get_db_conn() as conn:
        conn.execute("INSERT INTO spot_budget_reservations(config_id,operation_id,cycle_id,symbol,quote_cost,status,created_at,updated_at) VALUES ('spot','lost','cycle','ETH/USDT',20,'unknown',1,1)")
        conn.commit()
    with pytest.raises(SpotConfigConflict):
        assert_spot_config_change_allowed(spot_config, {**spot_config, 'symbols': ['BTC/USDT']})


def test_primary_reorder_cannot_reassign_manual_holdings_to_another_coin(spot_config):
    spot_config.update(initial_qty=2, initial_cost=100)
    with pytest.raises(SpotConfigConflict):
        assert_spot_config_change_allowed(spot_config, {**spot_config, 'symbol': 'ETH/USDT', 'symbols': ['ETH/USDT', 'BTC/USDT']})


def test_expanding_legacy_task_cannot_silently_clear_initial_costs(spot_config):
    previous = {**spot_config, 'symbols': ['BTC/USDT'], 'initial_qty': 1, 'initial_cost': 50000}
    expanded = {**previous, 'symbols': ['BTC/USDT', 'ETH/USDT'], 'initial_qty': 0, 'initial_cost': 0}
    with pytest.raises(SpotConfigConflict, match='初始持仓'):
        assert_spot_config_change_allowed(previous, expanded)
    cleared = {**previous, 'initial_qty': 0, 'initial_cost': 0}
    assert_spot_config_change_allowed(previous, cleared)
    assert_spot_config_change_allowed(cleared, expanded)
