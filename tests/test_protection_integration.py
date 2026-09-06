import concurrent.futures
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import backend.database as database
from backend.database_schema import initialize_schema
from backend.agent import agent_tools
from backend.agent.tool_registry import run_trade_tool
from backend.app.core import scheduler
from backend.utils.market_data import MarketTool
from backend.utils.trade_review import daily_exchange_evidence


@pytest.fixture
def local_db(tmp_path):
    with patch.object(database, 'DB_NAME', str(tmp_path / 'test.db')):
        with database.get_db_conn() as conn:
            initialize_schema(conn)
        yield


def test_simulated_pending_protection_update_is_scoped_and_preserves_other_price(local_db):
    database.create_mock_order('ETH/USDT', 'BUY', 100, 1, 90, 120, 'cfg', config_id='cfg', order_id='ST1')
    args = dict(order_id='ST1', reason='test', symbol='ETH/USDT', stop_loss=95)
    assert '未找到' in agent_tools.update_position_protection_strategy.func(config_id='wrong', **args)
    assert '已更新' in agent_tools.update_position_protection_strategy.func(config_id='cfg', **args)
    with database.get_db_conn() as conn:
        row = conn.execute("SELECT * FROM mock_orders WHERE order_id='ST1'").fetchone()
    assert row['stop_loss'] == 95 and row['take_profit'] == 120


def test_simulated_position_can_trail_stop_above_entry_using_current_price(local_db):
    database.create_mock_order('ETH/USDT', 'BUY', 100, 1, 90, 120, 'cfg', config_id='cfg', order_id='ST1')
    with database.get_db_conn() as conn:
        conn.execute("UPDATE mock_orders SET is_filled=1 WHERE order_id='ST1'")
        conn.commit()
    mt = SimpleNamespace(exchange=SimpleNamespace(fetch_ticker=lambda symbol: {'last': 110}))
    with patch.object(agent_tools, 'MarketTool', return_value=mt):
        result = agent_tools.update_position_protection_strategy.func('ST1', 'lock profit', 'cfg', 'ETH/USDT', stop_loss=105)
    assert '已更新' in result


def test_filled_position_racing_management_cannot_be_resurrected(local_db):
    database.create_mock_order('ETH/USDT', 'BUY', 100, 1, 90, 120, 'cfg', config_id='cfg', order_id='ST1')
    with database.get_db_conn() as conn:
        conn.execute("UPDATE mock_orders SET is_filled=1 WHERE order_id='ST1'")
        conn.commit()
    def ticker(symbol):
        with database.get_db_conn() as conn:
            conn.execute("UPDATE mock_orders SET status='CLOSED' WHERE order_id='ST1'")
            conn.commit()
        return {'last': 110}
    with patch.object(agent_tools, 'MarketTool', return_value=SimpleNamespace(exchange=SimpleNamespace(fetch_ticker=ticker))):
        result = agent_tools.update_position_protection_strategy.func('ST1', 'test', 'cfg', 'ETH/USDT', stop_loss=105)
    assert '状态已变化' in result


def test_protection_monitor_runs_disabled_config_without_scheduling_duplicate_jobs(local_db):
    with database.get_db_conn() as conn:
        conn.execute('INSERT INTO real_protection_plans VALUES(?,?,?,?)', ('cfg', 'ETH/USDT', 'LONG', '{"state":"WAITING"}'))
        conn.commit()
    calls = []
    def submit(fn, cfg):
        calls.append(cfg)
        return concurrent.futures.Future()
    cfg = {'config_id': 'cfg', 'symbol': 'ETH/USDT', 'mode': 'REAL', 'enabled': False}
    with patch.object(scheduler, '_protection_executor', SimpleNamespace(submit=submit)), patch.object(scheduler, '_protection_futures', {}), patch.object(scheduler.global_config, 'get_all_symbol_configs', return_value=[cfg]):
        scheduler.protection_tick()
        scheduler.protection_tick()
    assert calls == [cfg]


def test_daily_exchange_query_is_bounded_to_the_requested_day_and_preserves_unknown_pnl():
    received = {}
    def fetch(symbol, since, limit, params):
        received.update(since=since, params=params)
        return [{'id': 'outside', 'timestamp': since - 1}, {'id': 'inside', 'timestamp': since + 1, 'fee': None, 'info': {}}]
    with patch('backend.utils.market_data.MarketTool', return_value=SimpleNamespace(exchange=SimpleNamespace(fetch_my_trades=fetch))):
        text = daily_exchange_evidence({'mode': 'REAL', 'config_id': 'cfg', 'symbol': 'ETH/USDT'}, '2026-09-01')
    assert 'outside' not in text and 'inside' in text
    assert '"realized_pnl": null' in text
    assert received['params']['until'] + 1 - received['since'] == 86400000


def test_exchange_protection_orders_display_correct_type_and_full_position_amount(local_db):
    ex = SimpleNamespace(
        options={'defaultType': 'swap'},
        market=lambda symbol: {'contract': True, 'contractSize': 1},
        fetch_balance=lambda: {'USDT': {'total': 100, 'free': 90}},
        fetch_positions=lambda symbols: [{'symbol': 'ETH/USDT', 'side': 'long', 'contracts': 1, 'entryPrice': 100, 'unrealizedPnl': 0}],
        fetch_open_orders=lambda symbol, params=None: [] if not params else [
            {'id': 'stop', 'type': 'market', 'side': 'sell', 'amount': 0, 'triggerPrice': 90,
             'info': {'orderType': 'STOP_MARKET', 'positionSide': 'BOTH', 'closePosition': True}}],
    )
    mt = object.__new__(MarketTool)
    mt.exchange = ex
    mt.config_id = 'cfg'
    result = mt.get_account_status('ETH/USDT', is_real=True, config_id='cfg')
    assert result['real_open_orders'][0]['type'] == 'STOP'
    assert result['real_open_orders'][0]['amount'] == 1
    assert result['real_open_orders'][0]['price'] == 90


def test_limit_close_request_does_not_fabricate_closed_position(local_db):
    mt = SimpleNamespace(place_real_order=lambda *args, **kwargs: {'id': 'close1', 'status': 'open'})
    with patch.object(agent_tools, 'MarketTool', return_value=mt), patch('backend.config.config.get_config_by_id', return_value={}):
        result = agent_tools.close_position_real.func([{'pos_side': 'LONG', 'entry_price': 120, 'amount': 1, 'reason': 'take profit'}], 'cfg', 'ETH/USDT')
    assert '下单成功' in result
    with database.get_db_conn() as conn:
        assert conn.execute('SELECT COUNT(*) FROM position_history').fetchone()[0] == 0
        row = conn.execute("SELECT status,event_type FROM orders WHERE order_id='close1'").fetchone()
    assert row['status'] == 'OPEN' and row['event_type'] == 'CLOSE_ORDER_CREATED'


def test_invalid_management_arguments_are_rejected_before_exchange_initialization():
    with patch.object(agent_tools, 'MarketTool') as factory:
        with pytest.raises(ValueError):
            run_trade_tool('update_position_protection_real', {'pos_side': 'LONG', 'stop_loss': -10, 'reason': 'invalid'}, 'cfg', 'ETH/USDT')
    factory.assert_not_called()
