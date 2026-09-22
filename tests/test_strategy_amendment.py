import time

import pytest

import backend.database as database
from backend.agent.agent_tools import update_entry_order_strategy
from backend.agent.tool_registry import get_trade_tools_for_mode, run_trade_tool
from backend.utils.trading_policy import trading_policy


@pytest.fixture
def mock_order(tmp_path, monkeypatch):
    monkeypatch.setattr(database, 'DB_NAME', str(tmp_path / 'test.db'))
    database.init_db()
    database.get_mock_account('cfg', 'ETH/USDT')
    with database.get_db_conn() as conn:
        conn.execute("INSERT INTO mock_orders(order_id,config_id,symbol,side,price,amount,stop_loss,take_profit,status,is_filled,expire_at) VALUES('o','cfg','ETH/USDT','BUY',100,1,90,120,'OPEN',0,?)", (time.time() + 3600,))
        conn.commit()


def amend(**kwargs):
    return update_entry_order_strategy.func(**{
        'order_id': 'o', 'pos_side': 'LONG', 'reason': 'new support', 'config_id': 'cfg',
        'symbol': 'ETH/USDT', **kwargs,
    })


def read_order():
    with database.get_db_conn() as conn:
        return dict(conn.execute("SELECT * FROM mock_orders WHERE order_id='o'").fetchone())


def test_pending_prices_update_together_and_preserve_identity(mock_order):
    before = read_order()
    assert '已确认' in amend(entry_price=85, stop_loss=80, take_profit=110)
    after = read_order()
    assert (after['price'], after['stop_loss'], after['take_profit']) == (85, 80, 110)
    for key in ('amount', 'side', 'expire_at', 'order_id', 'is_filled'):
        assert after[key] == before[key]


@pytest.mark.parametrize('kwargs', [
    {'entry_price': 89}, {'entry_price': 101, 'pos_side': 'SHORT'},
    {'entry_price': 101, 'config_id': 'other'}, {'entry_price': 101, 'symbol': 'BTC/USDT'},
    {'stop_loss': 101}, {'take_profit': 99}, {'entry_price': float('nan')},
])
def test_invalid_amend_leaves_order_unchanged(mock_order, kwargs):
    before = read_order()
    assert '❌' in amend(**kwargs)
    assert read_order() == before


@pytest.mark.parametrize('field,value', [('is_filled', 1), ('expire_at', 1), ('amount', 1000)])
def test_filled_expired_or_unaffordable_cannot_amend(mock_order, field, value):
    with database.get_db_conn() as conn:
        conn.execute(f'UPDATE mock_orders SET {field}=?', (value,))
        conn.commit()
    before = read_order()
    assert '❌' in amend(entry_price=101)
    assert read_order() == before


def test_short_price_ordering(mock_order):
    with database.get_db_conn() as conn:
        conn.execute("UPDATE mock_orders SET side='SELL',stop_loss=120,take_profit=90")
        conn.commit()
    assert '已确认' in amend(pos_side='SHORT', entry_price=105, stop_loss=115, take_profit=95)
    assert '❌' in amend(pos_side='SHORT', stop_loss=100)


def test_policies_match_registered_tools_and_explain_direction():
    for mode in ('REAL', 'STRATEGY', 'SPOT_DCA'):
        text = trading_policy(mode)
        for tool in get_trade_tools_for_mode(mode):
            assert tool.name in text
        assert 'LONG' in text and 'SHORT' in text
    assert '不是原子事务' in trading_policy('REAL')


def test_dispatch_validates_direction_and_nonfinite_prices(mock_order, monkeypatch):
    from backend.config import config
    monkeypatch.setattr(config, 'get_config_by_id', lambda _: {'mode': 'STRATEGY'})
    for args in ({'pos_side': 'BUY', 'entry_price': 100}, {'pos_side': 'LONG', 'entry_price': float('inf')}):
        with pytest.raises(ValueError):
            run_trade_tool('update_entry_order_strategy', {'order_id': 'o', 'reason': 'test', **args}, 'cfg', 'ETH/USDT')
