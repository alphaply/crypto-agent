from types import SimpleNamespace

import pytest

import backend.database as database
from backend.agent import agent_tools
from backend.database_schema import initialize_schema


@pytest.mark.parametrize('pos_side,order_side,expected_pnl', [('LONG', 'BUY', 10), ('SHORT', 'SELL', -10)])
@pytest.mark.parametrize('has_filled_position', [False, True])
def test_strategy_close_only_realizes_filled_positions(
    tmp_path, monkeypatch, pos_side, order_side, expected_pnl, has_filled_position,
):
    monkeypatch.setattr(database, 'DB_NAME', str(tmp_path / 'close.db'))
    with database.get_db_conn() as conn:
        initialize_schema(conn)
        entries = [('pending', order_side, 0), ('opposite', 'SELL' if order_side == 'BUY' else 'BUY', 1)]
        if has_filled_position:
            entries.append(('filled', order_side, 1))
        conn.executemany(
            "INSERT INTO mock_orders(order_id,symbol,config_id,agent_name,side,price,amount,is_filled,status) "
            "VALUES(?, 'ETH/USDT', 'cfg', 'cfg', ?, 100, 1, ?, 'OPEN')",
            entries,
        )
        conn.commit()
    initial_balance = database.get_mock_account('cfg', 'ETH/USDT')['balance']

    class Market:
        exchange = SimpleNamespace(fetch_ticker=lambda _: {'last': 110})

        def get_account_status(self, *_args, **_kwargs):
            return {'mock_open_orders': database.get_mock_orders('ETH/USDT', config_id='cfg')}

    monkeypatch.setattr(agent_tools, 'MarketTool', lambda **_: Market())
    result = agent_tools.close_position_strategy.func(
        orders=[{'pos_side': pos_side, 'entry_price': 0, 'amount': 0, 'reason': 'exit invalidated trade'}],
        config_id='cfg', symbol='ETH/USDT',
    )

    with database.get_db_conn() as conn:
        orders = {row['order_id']: dict(row) for row in conn.execute('SELECT * FROM mock_orders')}
        realized_logs = conn.execute("SELECT * FROM orders WHERE event_type='MANUAL_CLOSE'").fetchall()
    assert orders['pending']['status'] == 'OPEN'
    assert orders['pending']['is_filled'] == 0
    assert orders['pending']['close_time'] is None
    assert not orders['pending']['realized_pnl']
    assert orders['opposite']['status'] == 'OPEN'
    assert len(realized_logs) == int(has_filled_position)
    if has_filled_position:
        assert 'Closed Strategy' in result
        assert orders['filled']['status'] == 'CLOSED'
        assert orders['filled']['close_price'] == 110
        assert orders['filled']['realized_pnl'] == expected_pnl
        assert realized_logs[0]['realized_pnl'] == expected_pnl
    else:
        assert '没有找到' in result
    expected_balance = initial_balance + (expected_pnl if has_filled_position else 0)
    assert database.get_mock_account('cfg', 'ETH/USDT')['balance'] == expected_balance
