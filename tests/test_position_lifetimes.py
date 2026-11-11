"""Position lifetimes and spot accounting use execution evidence, never intent."""
from datetime import datetime, timezone
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.services import position_history_service as service
from backend.utils.position_history import build_positions, timestamp
from backend.utils.spot_valuation import value_spot_events


NOW = datetime(2026, 11, 11, 12, tzinfo=timezone.utc)
END = int(NOW.timestamp() * 1000)


def event(identity, day, kind, quantity, price=100, fee=1, *, currency='USDT', mode='SPOT_DCA', side=None, **extra):
    return dict(event_id=identity, trade_id=identity, order_id='same-order', symbol='BTC/USDT',
                side=side or ('SPOT' if mode == 'SPOT_DCA' else 'LONG'), kind=kind,
                timestamp_ms=END-day*86400000, quantity=quantity, price=price, cost=quantity*price,
                fees=[dict(cost=fee, currency=currency)] if fee is not None else [],
                pnl=0 if kind == 'entry' else (price-100)*quantity,
                is_fill=True, source='test', **extra)


def build(events, mode='SPOT_DCA'):
    return build_positions('task', mode, 'account', events, now_ms=END)


def test_spot_partial_exits_allocate_entry_fee_and_match_holding_cost():
    source = [event('buy', 12, 'entry', 2, fee=2), event('sell1', 3, 'exit', 1, price=120),
              event('sell2', 1, 'exit', 1, price=80, fee=.5)]
    row = build(source)[0]
    assert row['status'] == 'closed'
    assert row['realized_net_pnl'] == pytest.approx(-3.5)
    assert row['realized_pnl_before_fees'] == pytest.approx(0)
    assert row['fees_by_currency'] == {'USDT': 3.5}
    assert [e['realized_net_pnl'] for e in row['events']] == [0, 18, -21.5]
    holding = value_spot_events([dict(side='buy' if e['kind']=='entry' else 'sell', quantity=e['quantity'],
        cost=e['cost'], fee=e['fees'][0]['cost'], fee_currency='USDT') for e in source], 0, 0, 'BTC/USDT')
    assert row['realized_net_pnl'] == holding['realized_pnl']


@pytest.mark.parametrize('mode', ['SPOT_DCA', 'REAL', 'STRATEGY'])
def test_zero_to_zero_grouping_partial_fills_reentry_and_stable_identity(mode):
    rows = [event('b1', 8, 'entry', 1, mode=mode), event('b2', 7, 'entry', 2, mode=mode),
            event('s1', 6, 'exit', 1, price=110, mode=mode), event('s2', 5, 'exit', 2, price=110, mode=mode),
            event('b3', 1, 'entry', 1, mode=mode)]
    open_id = build(rows[:2], mode)[0]['position_id']
    result = build(rows + [rows[0]], mode)
    assert len(result) == 2
    assert result[0]['status'] == 'open' and result[0]['closed_at'] is None
    assert result[1]['position_id'] == open_id
    assert [e['kind'] for e in result[1]['events']] == ['entry', 'add', 'reduce', 'close']
    assert [e['quantity_after'] for e in result[1]['events']] == [1, 3, 2, 0]


def test_opposite_directions_do_not_merge():
    rows = build([event('long', 2, 'entry', 1, mode='REAL'), event('short', 2, 'entry', 1, mode='REAL', side='SHORT')], 'REAL')
    assert len(rows) == 2 and {r['side'] for r in rows} == {'LONG', 'SHORT'}


@pytest.mark.parametrize('fee,currency', [(None, 'USDT'), (.01, 'BNB')])
def test_unknown_fees_do_not_become_zero(fee, currency):
    row = build([event('buy', 8, 'entry', 2, fee=fee, currency=currency), event('sell', 1, 'exit', 2, price=120)])[0]
    assert row['realized_net_pnl'] is None
    assert row['status'] == 'closed'
    assert not row['complete']


def test_base_fee_reduces_quantity_and_rebate_increases_profit():
    base = build([event('buy', 8, 'entry', 2, fee=.02, currency='BTC'), event('sell', 1, 'exit', 1.98, price=120)])[0]
    assert base['events'][0]['quantity_after'] == pytest.approx(1.98)
    assert base['realized_net_pnl'] == pytest.approx(36.6)
    rebate = build([event('buy', 8, 'entry', 2, fee=-2), event('sell', 1, 'exit', 2, price=120)])[0]
    assert rebate['realized_net_pnl'] == pytest.approx(41)


def test_orphan_and_unknown_time_are_retained_without_invented_lifetime():
    orphan = event('sell', 1, 'exit', 1, price=120)
    old = event('buy', 2, 'entry', 1)
    old['timestamp_ms'] = None
    rows = build([orphan, old])
    assert len(rows) == 2
    assert all(r['opened_at'] is None and r['closed_at'] is None for r in rows)
    assert all(r['realized_net_pnl'] is None for r in rows)


def test_calibration_is_not_a_trade_and_does_not_rewrite_earlier_profit():
    rows = [event('buy', 12, 'entry', 2, fee=2), event('sell', 9, 'exit', 1, price=120),
            dict(event_id='basis', symbol='BTC/USDT', side='SPOT', kind='calibration', timestamp_ms=END-8*86400000,
                 quantity=1, cost=150, fees=[], source='cost_basis', is_fill=False),
            event('last', 1, 'exit', 1, price=160)]
    row = build(rows)[0]
    assert row['events'][1]['realized_net_pnl'] == 18
    assert row['events'][-1]['realized_net_pnl'] == 9
    assert row['realized_net_pnl'] == 27
    assert row['entered_quantity'] == 2


def seed_spot(conn, *, task='spot', symbol='BTC/USDT', oid='buy', fills=None, side='buy'):
    fills = fills or [('t1', 12, 1, 100, 1), ('t2', 10, 1, 100, 1)]
    quantity, cost = sum(f[2] for f in fills), sum(f[2]*f[3] for f in fills)
    conn.execute('''INSERT INTO orders(order_id,config_id,symbol,trade_mode,status,side,
        filled_amount,filled_cost,avg_fill_price,filled_at,event_type) VALUES(%s,%s,%s,'SPOT_DCA','FILLED',%s,%s,%s,%s,%s,'ORDER_CREATED')''',
        (oid,task,symbol,side.upper(),quantity,cost,cost/quantity,NOW.isoformat()))
    for tid, day, qty, price, fee in fills:
        conn.execute('''INSERT INTO trade_history(trade_id,order_id,config_id,symbol,timestamp,side,price,amount,cost,fee,fee_currency)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'USDT')''',
            (tid,oid,task,symbol,datetime.fromtimestamp((END-day*86400000)/1000,timezone.utc).isoformat(),side,price,qty,qty*price,fee))


@pytest.fixture
def spot_history(pg, monkeypatch):
    cfg = dict(mode='SPOT_DCA', symbol='BTC/USDT', symbols=['BTC/USDT', 'ETH/USDT'])
    monkeypatch.setattr(service.config, 'get_config_by_id', lambda key: cfg if key=='spot' else None)
    with pg() as conn:
        seed_spot(conn)
        seed_spot(conn, oid='sell', side='sell', fills=[('s1', 1, 1, 120, 1)])
        seed_spot(conn, symbol='ETH/USDT', oid='eth', fills=[('e1', 1, 1, 50, .5)])
        seed_spot(conn, task='other', oid='foreign', fills=[('x1', 1, 10, 500, 5)])
        conn.commit()
    return cfg


def test_spot_counts_real_fills_allocation_and_window_cost(pg, spot_history):
    data = service.spot_statistics('spot', cfg=spot_history, now=NOW)
    assert data['all_history']['fill_count'] == 4
    assert data['all_history']['buy_fill_count'] == 3
    assert data['seven_days']['fill_count'] == 2
    assert data['seven_days']['realized_net_pnl'] == pytest.approx(18)
    assert data['cumulative_buy_amount'] == 250
    assert [r['investment_share_pct'] for r in data['by_symbol']] == [80, 20]


def test_coarse_order_does_not_inflate_fill_count(pg, spot_history):
    with pg() as conn:
        conn.execute("DELETE FROM trade_history WHERE symbol='ETH/USDT'")
        conn.commit()
    data = service.spot_statistics('spot', cfg=spot_history, now=NOW)
    assert data['all_history']['fill_count'] == 3
    assert data['all_history']['counts_complete'] is False
    assert data['cumulative_buy_amount'] == 250


def test_new_history_api_auth_filters_detail_isolation_and_read_only(pg, spot_history):
    from backend.app.api.history import router
    from backend.app.core.deps import get_current_user
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        assert client.get('/api/history/positions?config_id=spot').status_code in {401,403}
        app.dependency_overrides[get_current_user] = lambda: {'sub':'test'}
        data = client.get('/api/history/positions?config_id=spot&per_page=1').json()
        assert data['total'] == 2 and len(data['items']) == 1
        identity = data['items'][0]['position_id']
        result = client.get(f'/api/history/positions/{identity}?config_id=spot')
        assert result.status_code == 200 and result.json()['position']['events']
        assert client.get(f'/api/history/positions/{identity}?config_id=other').status_code == 404
        assert client.get('/api/history/positions?config_id=spot&status=closed').json()['total'] == 0
        assert client.get('/api/history/positions?config_id=spot&symbol=ETH/USDT').json()['total'] == 1
        assert client.get('/api/history/positions?config_id=spot&page=0').status_code == 422
        assert client.get('/api/history/positions?config_id=spot&status=anything').status_code == 422


def test_real_adapter_preserves_task_scope_and_unknown_exits(pg, monkeypatch):
    from test_real_symbol_performance import seed_cycle
    cfg = dict(mode='REAL', symbol='BTC/USDT:USDT')
    monkeypatch.setattr('backend.utils.trade_evidence.configured_account_scope', lambda _: 'account')
    seed_cycle('account', 'task', 'BTC/USDT:USDT', END-10000, END-1000, name='own', pnl=3)
    seed_cycle('account', 'other', 'BTC/USDT:USDT', END-30000, END-20000, name='other', pnl=800)
    seed_cycle('different', 'task', 'BTC/USDT:USDT', END-30000, END-20000, name='foreign', pnl=900)
    data = service.read_position_history('task', cfg=cfg, now=NOW)
    assert len(data['positions']) == 1
    assert data['positions'][0]['realized_net_pnl'] == pytest.approx(2.8)


def test_mock_adapter_uses_execution_time_not_order_creation(pg):
    with pg() as conn:
        for index, (kind, side, quantity, pnl) in enumerate([('ENTRY_FILLED','ENTRY_BUY',2,0),('MANUAL_CLOSE','CLOSE_LONG',1,10),('TP_HIT','CLOSE_LONG',1,20)]):
            conn.execute('''INSERT INTO orders(config_id,symbol,trade_mode,event_type,side,amount,entry_price,
                realized_pnl,timestamp,order_id,parent_order_id) VALUES('mock','BTC/USDT','STRATEGY',%s,%s,%s,100,%s,%s,%s,'entry')''',
                (kind,side,quantity,pnl,datetime.fromtimestamp((END-(3-index)*86400000)/1000,timezone.utc).isoformat(),f'e{index}'))
        conn.commit()
    data = service.read_position_history('mock',cfg=dict(mode='STRATEGY',symbol='BTC/USDT'),now=NOW)
    row = data['positions'][0]
    assert row['status'] == 'closed' and row['realized_pnl_before_fees'] == 30
    assert row['realized_net_pnl'] is None
    assert row['issues'] == ['incomplete_fees']


def test_legacy_unassigned_copy_does_not_duplicate_spot_investment(pg, spot_history):
    with pg() as conn:
        conn.execute('''INSERT INTO trade_history(trade_id,order_id,config_id,symbol,timestamp,side,price,amount,cost,fee,fee_currency)
            SELECT trade_id,order_id,NULL,symbol,timestamp,side,price,amount,cost,fee,fee_currency
            FROM trade_history WHERE trade_id='t1' ''')
        conn.commit()
    result = service.spot_statistics('spot', cfg=spot_history, now=NOW)
    assert result['cumulative_buy_amount'] == 250
    assert result['all_history']['buy_fill_count'] == 3
    assert result['seven_days']['realized_net_pnl'] == 18


def test_legacy_initial_basis_supports_future_sales_without_fake_entry():
    basis = dict(event_id='legacy', symbol='BTC/USDT', side='SPOT', kind='calibration',
                 timestamp_ms=None, quantity=1, cost=100, fees=[], source='legacy_basis', is_fill=False, initial=True)
    row = build([basis, event('sell', 1, 'exit', 1, price=130)])[0]
    assert row['opened_at'] is None and row['entered_quantity'] is None
    assert row['status'] == 'closed'
    assert row['events'][-1]['realized_net_pnl'] == 29
    assert row['realized_net_pnl'] is None


def test_zero_investment_and_missing_prices_do_not_block_local_statistics(pg):
    cfg = dict(mode='SPOT_DCA',symbol='BTC/USDT',symbols=['BTC/USDT'])
    data = service.spot_statistics('empty',cfg=cfg,now=NOW)
    assert data['cumulative_buy_amount'] == 0
    assert data['by_symbol'][0]['investment_share_pct'] is None
    assert data['all_history']['fill_count'] == 0


def test_stats_api_keeps_new_evidence_when_legacy_summary_cannot_order_fills(pg, spot_history):
    from backend.app.api.stats import router
    from backend.app.core.deps import get_current_user
    with pg() as conn:
        conn.execute("UPDATE trade_history SET timestamp=NULL WHERE trade_id='t1'")
        conn.commit()
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: {'sub':'test'}
    with TestClient(app) as client:
        result = client.get('/api/stats/task-history/spot')
        assert result.status_code == 200
        data = result.json()['statistics']
        assert data['legacy_statistics_error']
        assert data['seven_days'] is None
        assert data['spot']['all_history']['fill_count'] == 4
        assert data['spot']['seven_days']['realized_net_pnl'] is None


def test_missing_executed_cost_is_not_replaced_by_order_limit_price(pg, spot_history):
    from backend.utils.spot_valuation import spot_orders
    with pg() as conn:
        conn.execute("DELETE FROM trade_history WHERE symbol='ETH/USDT'")
        conn.execute("UPDATE orders SET filled_cost=NULL,avg_fill_price=NULL,entry_price=999 WHERE symbol='ETH/USDT'")
        # Existing holding API semantics stay compatible; new history is strict.
        assert spot_orders(conn, 'spot', 'ETH/USDT')['eth']['cost'] == 999
        conn.commit()
    data = service.spot_statistics('spot', cfg=spot_history, now=NOW)
    assert data['cumulative_buy_amount'] is None
    assert all(r['investment_share_pct'] is None for r in data['by_symbol'])
    assert data['by_symbol'][1]['cumulative_buy_amount'] is None
    row = next(r for r in service.read_position_history('spot',cfg=spot_history,now=NOW)['positions'] if r['symbol']=='ETH/USDT')
    assert row['entered_quantity'] == 1
    assert row['realized_net_pnl'] is None
    assert 'cost_basis_unavailable' in row['issues']


def test_limit_price_does_not_override_actual_fills(pg, spot_history):
    with pg() as conn:
        conn.execute("UPDATE orders SET entry_price=999,avg_fill_price=NULL,filled_cost=NULL WHERE symbol='ETH/USDT'")
        conn.commit()
    result = service.spot_statistics('spot',cfg=spot_history,now=NOW)
    assert result['cumulative_buy_amount'] == 250
    assert result['by_symbol'][1]['cumulative_buy_amount'] == 50
