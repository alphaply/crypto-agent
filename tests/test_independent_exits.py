import time
from types import SimpleNamespace

import ccxt
import pytest

import backend.database as database
from backend.database_schema import initialize_schema
from backend.database_independent import MockIndependentTrading
from backend.utils.independent_exits import IndependentExits, IndependentPositionError
from backend.utils.position_protection import PositionProtection


@pytest.fixture
def local_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database, 'DB_NAME', str(tmp_path / 'independent.db'))
    with database.get_db_conn() as conn:
        initialize_schema(conn)
    return MockIndependentTrading('cfg', 'ETH/USDT')


def entry(price=100, amount=1):
    return SimpleNamespace(action='BUY_LIMIT', entry_price=price, amount=amount,
                           stop_loss=None, take_profit=None, reason='test', valid_duration_hours=24)


def fill_entries(store, high=120, low=80):
    return store.monitor(high, low, int((time.time()+120)*1000))


def test_mock_aggregate_mean_cost_partial_exit_and_no_growth(local_db):
    store=local_db
    store.open(entry(100,1), operation_id='one')
    store.open(entry(120,1), operation_id='two')
    fill_entries(store)
    position=store.snapshot()['positions'][0]
    assert position['amount']==2 and position['price']==110
    tp1=store.close('LONG',1,'take_profit_limit',110,price=130)
    store.close('LONG',1,'take_profit_limit',110,price=140)
    store.close('LONG',0,'stop_market',110,trigger_price=90)
    result=store.close('LONG',.5,'market',120, operation_id='reduce')
    assert result['realized_pnl']==5
    assert store.close('LONG',.5,'market',120,operation_id='reduce')==result
    snapshot=store.snapshot()
    assert snapshot['positions'][0]['amount']==1.5
    assert snapshot['positions'][0]['price']==110
    assert sorted(row['remaining'] for row in snapshot['exits'])==[.75,.75,1.5]
    store.open(entry(100,1))
    fill_entries(store,high=115,low=100)
    assert store.snapshot()['positions'][0]['amount']==2.5
    assert store.snapshot()['uncovered']['LONG']==1
    assert len([row for row in store.snapshot()['exits'] if row['order_id']==tp1['id']])==1
    with database.get_db_conn() as conn:
        assert conn.execute("SELECT balance FROM mock_accounts WHERE config_id='cfg'").fetchone()[0]==10005


def test_mock_conditions_stop_first_and_cycle_cleanup(local_db):
    store=local_db
    store.open(entry())
    fill_entries(store)
    pending=store.open(entry(70,.5))
    store.close('LONG',0,'take_profit_limit',100,price=110)
    store.close('LONG',0,'stop_market',100,trigger_price=90)
    result=fill_entries(store,high=120,low=80)
    assert result['closed']==1
    assert store.snapshot()['positions']==[]
    assert store.snapshot()['entries']==[]
    assert store.snapshot()['exits']==[]
    with database.get_db_conn() as conn:
        assert conn.execute('SELECT status FROM mock_orders WHERE order_id=?',(pending['id'],)).fetchone()[0]=='CANCELLED'
        assert conn.execute('SELECT balance FROM mock_accounts').fetchone()[0]==9990
        history=conn.execute('SELECT amount,realized_pnl FROM position_history').fetchone()
        assert history['amount']==1 and history['realized_pnl']==-10
    fill_entries(store,high=120,low=80)
    store.open(entry())
    fill_entries(store,high=110,low=95)
    assert store.snapshot()['positions'][0]['amount']==1


def test_mock_exit_created_after_candle_cannot_trigger(local_db):
    store=local_db
    store.open(entry())
    fill_entries(store)
    exit=store.close('LONG',1,'stop_market',100,trigger_price=90)
    store.monitor(110,80,int((time.time()-60)*1000))
    assert store.snapshot()['exits'][0]['id']==exit['id']


def test_mock_stop_precedes_lower_add_and_amendment_cannot_retrofill(local_db):
    store=local_db
    store.open(entry())
    fill_entries(store)
    store.close('LONG',1,'stop_market',100,trigger_price=90)
    add=store.open(entry(70,1))
    result=fill_entries(store,high=100,low=60)
    assert result=={'filled':0,'closed':1}
    assert store.snapshot()['positions']==[]
    with database.get_db_conn() as conn:
        assert conn.execute('SELECT balance FROM mock_accounts').fetchone()[0]==9990
    new=store.open(entry(70,1))
    store.amend_entry(new['id'],entry_price=100)
    store.monitor(110,80,int((time.time()-60)*1000))
    assert store.snapshot()['positions']==[]


def test_real_tiny_exit_amendment_preserves_existing_stop(live_service):
    svc,ex=live_service
    stop=svc.close('ETH/USDT','LONG',1,'stop_market',trigger_price=90)
    with pytest.raises(ValueError,match='remaining amount'):
        svc.amend_exit('ETH/USDT',stop['id'],amount=.00001)
    assert ex.orders[stop['id']]['status']=='open'
    assert svc.snapshot('ETH/USDT')['uncovered']['LONG']==0


def test_mock_available_funds_include_filled_position(local_db):
    local_db.open(entry(100,60))
    fill_entries(local_db)
    assert local_db.snapshot()['available_balance']==4000
    with pytest.raises(ValueError,match='funds'):
        local_db.open(entry(100,50))


def test_mock_exit_caps_ownership_and_amendment(local_db):
    local_db.open(entry())
    fill_entries(local_db)
    first=local_db.close('LONG',.7,'take_profit_limit',100,price=120)
    with pytest.raises(ValueError,match='exceed'):
        local_db.close('LONG',.4,'take_profit_limit',100,price=130)
    local_db.amend_exit(first['id'],amount=.5,current_price=100,price=125)
    local_db.close('LONG',.5,'take_profit_limit',100,price=130)
    with pytest.raises(ValueError,match='owned'):
        MockIndependentTrading('other','ETH/USDT').cancel(first['id'])
    local_db.cancel(first['id'])
    assert len(local_db.snapshot()['exits'])==1


class Exchange:
    id='binance'
    apiKey='offline-test'

    def __init__(self):
        self.quantity=0.0
        self.orders={}
        self.calls=[]
        self.timeout_next=False
        self.hide=False

    def load_markets(self):
        pass

    def market(self,symbol):
        return dict(symbol='ETH/USDT:USDT',contract=True,swap=True,linear=True,contractSize=1)

    def amount_to_precision(self,symbol,amount):
        return str(int((amount+1e-12)*10000)/10000)

    def price_to_precision(self,symbol,price):
        return str(round(price,2))

    def fetch_position_mode(self,symbol):
        return {'hedged':True}

    def fetch_positions(self,symbols):
        return [dict(side='long',contracts=self.quantity,hedged=True)] if self.quantity else []

    def fetch_ticker(self,symbol):
        return {'last':100}

    def create_order(self,symbol,type,side,amount,price,params):
        self.calls.append(dict(type=type,side=side,amount=amount,price=price,params=dict(params)))
        oid=str(len(self.orders)+1)
        row=dict(id=oid,status='open',amount=amount,filled=0,price=price,
                 clientOrderId=params['clientOrderId'],params=dict(params))
        if type=='market' and 'stopLossPrice' not in params:
            row.update(status='closed',filled=amount)
            self.quantity-=amount
        self.orders[oid]=row
        if self.timeout_next:
            self.timeout_next=False
            raise ccxt.RequestTimeout('lost acknowledgement')
        return dict(row)

    def fetch_order(self,oid,symbol,params):
        if self.hide:
            raise ccxt.OrderNotFound('not visible')
        cid=params.get('clientOrderId') or params.get('clientAlgoId')
        for row in self.orders.values():
            if str(row['id'])==str(oid) or (cid and row['clientOrderId']==cid):
                return dict(row)
        raise ccxt.OrderNotFound('not found')

    def cancel_order(self,oid,symbol,params):
        self.orders[str(oid)]['status']='canceled'
        return dict(self.orders[str(oid)])

    def edit_order(self,oid,symbol,type,side,amount,price):
        self.orders[str(oid)].update(amount=amount,price=price)
        return dict(self.orders[str(oid)])


@pytest.fixture
def live_service(local_db):
    ex=Exchange()
    svc=IndependentExits(SimpleNamespace(exchange=ex,config_id='cfg'))
    first=svc.open('ETH/USDT',entry(),operation_id='entry')
    ex.orders[first['id']].update(status='closed',filled=1)
    ex.quantity=1
    svc.reconcile_all()
    return svc,ex


def test_real_fixed_quantities_partial_exit_and_no_whole_position_legs(live_service):
    svc,ex=live_service
    svc.close('ETH/USDT','LONG',.5,'take_profit_limit',price=120)
    svc.close('ETH/USDT','LONG',.5,'take_profit_limit',price=130)
    svc.close('ETH/USDT','LONG',0,'stop_market',trigger_price=90)
    assert ex.calls[-1]['amount']==1
    assert all('closePosition' not in row['params'] and 'closeFraction' not in row['params'] for row in ex.calls)
    svc.close('ETH/USDT','LONG',.2,'market')
    snapshot=svc.snapshot('ETH/USDT')
    assert sorted(row['remaining'] for row in snapshot['exits'])==[.4,.4,.8]
    assert ex.quantity==pytest.approx(.8)
    plan=svc._load('ETH/USDT:USDT','LONG')
    assert plan['legs']==[] and plan['state']=='ACTIVE'
    assert snapshot['uncovered']['LONG']==pytest.approx(0)


def test_real_timeout_reconcile_does_not_replay_and_base_monitor_dispatches(live_service):
    svc,ex=live_service
    ex.timeout_next=True
    with pytest.raises(ccxt.RequestTimeout):
        svc.close('ETH/USDT','LONG',1,'stop_market',trigger_price=90,operation_id='stop')
    count=len(ex.calls)
    ex.hide=True
    assert svc.reconcile_all()[0]['error']
    assert len(ex.calls)==count
    replay=svc.close('ETH/USDT','LONG',1,'stop_market',trigger_price=90,operation_id='stop')
    assert replay['pending']
    ex.hide=False
    PositionProtection(svc.mt).reconcile_all()
    assert len(ex.calls)==count
    assert not svc.snapshot('ETH/USDT')['pending']


def test_real_flat_cleans_old_cycle_before_new_entry(live_service):
    svc,ex=live_service
    stop=svc.close('ETH/USDT','LONG',1,'stop_market',trigger_price=90)
    add=svc.open('ETH/USDT',entry(80,.5))
    old_episode=stop['episode_id']
    svc.close('ETH/USDT','LONG',1,'market')
    assert ex.orders[stop['id']]['status']=='canceled'
    assert ex.orders[add['id']]['status']=='canceled'
    assert svc._load('ETH/USDT:USDT','LONG')['state']=='DONE'
    new=svc.open('ETH/USDT',entry())
    assert new['episode_id']!=old_episode


def test_real_late_entry_fill_with_lagging_empty_position_never_finishes_cycle(live_service):
    svc,ex=live_service
    add=svc.open('ETH/USDT',entry(80,.5))
    original_cancel=ex.cancel_order
    original_positions=ex.fetch_positions
    lag={'remaining':0}
    def cancel(oid,symbol,params):
        result=original_cancel(oid,symbol,params)
        if str(oid)==add['id']:
            ex.orders[str(oid)]['filled']=.5
            ex.quantity+=.5
            result['filled']=.5
            lag['remaining']=1
        return result
    def positions(symbols):
        if lag['remaining']:
            lag['remaining']-=1
            return []
        return original_positions(symbols)
    ex.cancel_order=cancel
    ex.fetch_positions=positions
    svc.close('ETH/USDT','LONG',1,'market')
    assert ex.quantity==.5
    assert svc._load('ETH/USDT:USDT','LONG')['state']=='EXITING'
    # A fresh instance has no in-memory knowledge of the late fill.
    IndependentExits(svc.mt).reconcile_all()
    assert svc._load('ETH/USDT:USDT','LONG')['state']=='ACTIVE'
    assert ex.quantity==.5


def test_real_caps_and_explicit_amend_replacement(live_service):
    svc,ex=live_service
    first=svc.close('ETH/USDT','LONG',.7,'take_profit_limit',price=120)
    with pytest.raises(ValueError,match='exceed'):
        svc.close('ETH/USDT','LONG',.4,'take_profit_limit',price=130)
    changed=svc.amend_exit('ETH/USDT',first['id'],amount=.5,price=125,operation_id='amend')
    assert changed['id']!=first['id']
    count=len(ex.calls)
    assert svc.amend_exit('ETH/USDT',first['id'],amount=.5,price=125,operation_id='amend')['id']==changed['id']
    assert len(ex.calls)==count


def test_real_cancel_cannot_silently_restore_pending_replacement(live_service):
    svc,ex=live_service
    stop=svc.close('ETH/USDT','LONG',1,'stop_market',trigger_price=90)
    plan,record=svc._find_order('ETH/USDT',stop['id'])
    record['replacement']=dict(remaining=1,original_filled=0,price=None,trigger_price=92,
                               reason='interrupted amendment',operation_id='amend',client_id=svc._client_id('amend'))
    svc._save(plan)
    count=len(ex.calls)
    with pytest.raises(ValueError,match='replacement'):
        svc.cancel('ETH/USDT',stop['id'])
    assert len(ex.calls)==count and ex.orders[stop['id']]['status']=='open'
    svc.reconcile_all()
    successor=svc.snapshot('ETH/USDT')['exits'][0]
    with pytest.raises(ValueError,match='successor'):
        svc.cancel('ETH/USDT',stop['id'])
    svc.cancel('ETH/USDT',successor['id'])
    assert svc.snapshot('ETH/USDT')['exits']==[]


@pytest.mark.parametrize('filled', [.4, 1.0])
def test_real_filled_entry_ack_waits_for_position_across_restart(local_db, filled):
    ex = Exchange()
    svc = IndependentExits(SimpleNamespace(exchange=ex, config_id='cfg'))
    original_create, original_positions = ex.create_order, ex.fetch_positions

    def filled_ack(*args, **kwargs):
        order = original_create(*args, **kwargs)
        ex.orders[order['id']].update(status='closed' if filled == 1 else 'open', filled=filled)
        ex.quantity = filled
        return dict(ex.orders[order['id']])

    ex.create_order = filled_ack
    ex.fetch_positions = lambda symbols: []
    opened = svc.open('ETH/USDT', entry(), operation_id='filled-ack')
    assert opened['protection_state'] != 'DONE'
    from backend.utils.trade_operations import tool_result_status
    assert opened['pending'] and tool_result_status(opened) == 'unknown'
    assert svc._load('ETH/USDT:USDT', 'LONG')['cleanup_fill_unseen'] == filled
    assert len(ex.calls) == 1

    restarted = IndependentExits(svc.mt)
    restarted.reconcile_all()
    with pytest.raises(ValueError, match='reconciliation'):
        restarted.close('ETH/USDT', 'LONG', 0, 'market', operation_id='not-yet-visible')
    assert restarted._load('ETH/USDT:USDT', 'LONG')['state'] != 'DONE'
    assert len(ex.calls) == 1
    assert ex.orders[opened['id']]['status'] == ('closed' if filled == 1 else 'open')

    ex.fetch_positions = original_positions
    restarted.reconcile_all()
    plan = restarted._load('ETH/USDT:USDT', 'LONG')
    assert plan['state'] == 'ACTIVE' and plan['error'] is None
    assert not plan.get('cleanup_fill_unseen') and not plan.get('unobserved_entry_fills')
    replay = restarted.open('ETH/USDT', entry(), operation_id='filled-ack')
    assert replay['id'] == opened['id'] and not replay['pending']
    assert len(ex.calls) == 1


def test_terminal_ack_without_fill_is_queried_before_cycle_cleanup(local_db):
    ex = Exchange()
    svc = IndependentExits(SimpleNamespace(exchange=ex, config_id='cfg'))
    original_create, original_positions = ex.create_order, ex.fetch_positions

    def incomplete_ack(*args, **kwargs):
        order = original_create(*args, **kwargs)
        ex.orders[order['id']].update(status='closed', filled=None)
        ex.quantity = 1
        return dict(ex.orders[order['id']])

    ex.create_order = incomplete_ack
    ex.fetch_positions = lambda symbols: []
    with pytest.raises(RuntimeError, match='fill quantity is unverified'):
        svc.open('ETH/USDT', entry(), operation_id='incomplete-ack')
    plan = svc._load('ETH/USDT:USDT', 'LONG')
    assert plan['state'] != 'DONE'
    assert plan['entries'][0]['id'] == '1' and plan['entries'][0]['fill_pending']
    assert svc.open('ETH/USDT', entry(), operation_id='incomplete-ack')['pending']
    restarted = IndependentExits(svc.mt)
    assert restarted.reconcile_all()[0]['error']
    assert len(ex.calls) == 1

    ex.orders['1']['filled'] = 1
    restarted.reconcile_all()
    assert restarted._load('ETH/USDT:USDT', 'LONG')['cleanup_fill_unseen'] == 1
    ex.fetch_positions = original_positions
    ex.create_order = original_create
    restarted.reconcile_all()
    replay = restarted.open('ETH/USDT', entry(), operation_id='incomplete-ack')
    assert replay['id'] == '1' and not replay['pending'] and len(ex.calls) == 1
    closed = restarted.close('ETH/USDT', 'LONG', 0, operation_id='verified-close')
    assert closed['protection_state'] == 'DONE' and ex.quantity == 0


@pytest.mark.parametrize('unverified_fill', [None, .1, float('nan'), True])
def test_query_keeps_last_verified_fill_until_consistent_response(local_db, unverified_fill):
    ex = Exchange()
    svc = IndependentExits(SimpleNamespace(exchange=ex, config_id='cfg'))
    opened = svc.open('ETH/USDT', entry(), operation_id='partial-entry')
    ex.orders[opened['id']]['filled'] = .4
    ex.quantity = .4
    svc.reconcile_all()
    ex.orders[opened['id']].update(status='closed', filled=unverified_fill)
    assert svc.reconcile_all()[0]['error']
    plan = svc._load('ETH/USDT:USDT', 'LONG')
    assert plan['entries'][0]['filled'] == .4 and plan['entries'][0]['fill_pending']
    assert plan['state'] != 'DONE' and len(ex.calls) == 1
    ex.orders[opened['id']].update(status='canceled', filled=.4)
    IndependentExits(svc.mt).reconcile_all()
    plan = svc._load('ETH/USDT:USDT', 'LONG')
    assert plan['error'] is None and not plan['entries'][0].get('fill_pending')


@pytest.mark.parametrize('position_failure', ['empty', 'timeout'])
def test_stop_replacement_survives_position_read_failure_and_restart(live_service, position_failure):
    svc, ex = live_service
    stop = svc.close('ETH/USDT', 'LONG', 1, 'stop_market', trigger_price=90)
    original_cancel, original_positions = ex.cancel_order, ex.fetch_positions

    def failed_positions(symbols):
        if position_failure == 'timeout':
            raise ccxt.RequestTimeout('position read timed out after cancellation')
        return []

    def cancel_then_lose_position(*args, **kwargs):
        response = original_cancel(*args, **kwargs)
        ex.fetch_positions = failed_positions
        return response

    ex.cancel_order = cancel_then_lose_position
    with pytest.raises((RuntimeError, ccxt.RequestTimeout)):
        svc.amend_exit('ETH/USDT', stop['id'], trigger_price=95, operation_id='amend-stop')
    plan = svc._load('ETH/USDT:USDT', 'LONG')
    old_stop = plan['exits'][0]
    assert old_stop['status'] == 'canceled' and old_stop['replacement']['operation_id'] == 'amend-stop'
    replacement_client_id = old_stop['replacement']['client_id']
    assert plan['error'] and len(ex.calls) == 2
    if position_failure == 'empty':
        assert svc.snapshot('ETH/USDT')['pending']

    ex.fetch_positions, ex.cancel_order = original_positions, original_cancel
    restarted = IndependentExits(svc.mt)
    restarted.reconcile_all()
    snapshot = restarted.snapshot('ETH/USDT')
    assert not snapshot['pending'] and snapshot['uncovered']['LONG'] == 0
    assert len(snapshot['exits']) == 1
    successor = snapshot['exits'][0]
    assert successor['client_id'] == replacement_client_id and successor['trigger_price'] == 95
    assert successor['id'] != stop['id'] and len(ex.calls) == 3
    replay = restarted.amend_exit('ETH/USDT', stop['id'], trigger_price=95, operation_id='amend-stop')
    assert replay['id'] == successor['id']
    restarted.reconcile_all()
    assert len(ex.calls) == 3


def test_unobserved_entry_fill_stops_tool_batch_before_second_order(local_db, monkeypatch):
    import json
    from backend.agent import agent_tools
    from backend.config import config

    ex = Exchange()
    original_create = ex.create_order

    def filled_ack(*args, **kwargs):
        order = original_create(*args, **kwargs)
        ex.orders[order['id']].update(status='closed', filled=1)
        ex.quantity = 1
        return dict(ex.orders[order['id']])

    ex.create_order = filled_ack
    ex.fetch_positions = lambda symbols: []
    monkeypatch.setattr(agent_tools, 'MarketTool', lambda **_: SimpleNamespace(exchange=ex, config_id='cfg'))
    monkeypatch.setattr(config, 'get_config_by_id', lambda _: {
        'config_id': 'cfg', 'mode': 'REAL', 'exit_mode': 'independent_exits',
    })
    order = {'action': 'BUY_LIMIT', 'entry_price': 100, 'amount': 1, 'reason': 'test'}
    result = json.loads(agent_tools.open_position_real.func(orders=[order, order], config_id='cfg', symbol='ETH/USDT'))
    assert result['status'] == 'unknown'
    assert result['results'][0]['pending']
    assert result['results'][1] == {'status': 'not_executed', 'index': 1, 'blocked_by_index': 0}
    assert len(ex.calls) == 1


@pytest.mark.parametrize('empty_reads', [1, 2])
def test_real_transient_empty_position_never_cancels_active_stop(live_service, empty_reads):
    svc, ex = live_service
    stop = svc.close('ETH/USDT', 'LONG', 1, 'stop_market', trigger_price=90)
    original_positions = ex.fetch_positions
    remaining = empty_reads

    def positions(symbols):
        nonlocal remaining
        if remaining:
            remaining -= 1
            return []
        return original_positions(symbols)

    ex.fetch_positions = positions
    count = len(ex.calls)
    svc.reconcile_all()
    assert svc._load('ETH/USDT:USDT', 'LONG')['state'] == 'ACTIVE'
    assert ex.orders[stop['id']]['status'] == 'open'
    assert len(ex.calls) == count
    IndependentExits(svc.mt).reconcile_all()
    plan = svc._load('ETH/USDT:USDT', 'LONG')
    assert plan['state'] == 'ACTIVE' and plan['error'] is None
    assert 'flat_candidate' not in plan
    assert ex.orders[stop['id']]['status'] == 'open'
    assert len(ex.calls) == count


def test_real_external_flat_requires_later_confirmation_across_restart(live_service, monkeypatch):
    from backend.utils import independent_exits
    svc, ex = live_service
    stop = svc.close('ETH/USDT', 'LONG', 1, 'stop_market', trigger_price=90)
    clock = {'now': 100.0}
    monkeypatch.setattr(independent_exits, 'time', SimpleNamespace(time=lambda: clock['now']))
    ex.quantity = 0
    svc.reconcile_all()
    plan = svc._load('ETH/USDT:USDT', 'LONG')
    assert plan['state'] == 'ACTIVE'
    assert plan['flat_candidate'] == {'first_observed_at': 100.0, 'observations': 1}
    assert ex.orders[stop['id']]['status'] == 'open'

    restarted = IndependentExits(svc.mt)
    clock['now'] += 1
    restarted.reconcile_all()
    assert ex.orders[stop['id']]['status'] == 'open'
    clock['now'] += 1
    restarted.reconcile_all()
    assert restarted._load('ETH/USDT:USDT', 'LONG')['state'] == 'DONE'
    assert ex.orders[stop['id']]['status'] == 'canceled'


def test_real_failed_position_read_breaks_flat_confirmation(live_service, monkeypatch):
    from backend.utils import independent_exits
    svc, ex = live_service
    stop = svc.close('ETH/USDT', 'LONG', 1, 'stop_market', trigger_price=90)
    clock = {'now': 100.0}
    monkeypatch.setattr(independent_exits, 'time', SimpleNamespace(time=lambda: clock['now']))
    ex.quantity = 0
    svc.reconcile_all()
    original_positions = ex.fetch_positions

    def failed_read(symbols):
        raise ccxt.RequestTimeout('position read timed out')

    ex.fetch_positions = failed_read
    clock['now'] += 3
    assert svc.reconcile_all()[0]['error']
    assert 'flat_candidate' not in svc._load('ETH/USDT:USDT', 'LONG')
    assert ex.orders[stop['id']]['status'] == 'open'
    ex.fetch_positions = original_positions
    clock['now'] += 3
    svc.reconcile_all()
    assert svc._load('ETH/USDT:USDT', 'LONG')['state'] == 'ACTIVE'
    assert ex.orders[stop['id']]['status'] == 'open'


@pytest.mark.parametrize('cycle_state', ['MISSING', 'DONE'])
def test_real_close_verified_flat_without_active_cycle_is_skipped(local_db, cycle_state):
    ex = Exchange()
    svc = IndependentExits(SimpleNamespace(exchange=ex, config_id='cfg'))
    if cycle_state == 'DONE':
        plan = svc._new('ETH/USDT:USDT', 'LONG')
        plan['state'] = 'DONE'
        svc._save(plan)
    result = svc.close('ETH/USDT', 'LONG', 0, operation_id='already-flat')
    assert result == {'status': 'skipped', 'reason': 'no_position', 'symbol': 'ETH/USDT:USDT',
                      'pos_side': 'LONG', 'cycle_state': cycle_state, 'position_amount': 0.0}
    assert ex.calls == []


@pytest.mark.parametrize('cycle_state', ['MISSING', 'DONE'])
def test_real_close_untracked_position_rejects_without_adoption(local_db, cycle_state):
    ex = Exchange()
    ex.quantity = 3
    original_market = ex.market
    ex.market = lambda symbol: {**original_market(symbol), 'contractSize': .1}
    svc = IndependentExits(SimpleNamespace(exchange=ex, config_id='cfg'))
    if cycle_state == 'DONE':
        plan = svc._new('ETH/USDT:USDT', 'LONG')
        plan['state'] = 'DONE'
        svc._save(plan)
        # A previous account's completed cycle is not proof of current ownership.
        ex.apiKey = 'different-offline-account'
        svc = IndependentExits(svc.mt)
    previous = svc._load('ETH/USDT:USDT', 'LONG')
    with pytest.raises(IndependentPositionError) as caught:
        svc.close('ETH/USDT', 'LONG', 0, operation_id='untracked')
    assert caught.value.code == 'independent_position_cycle_missing'
    details = caught.value.details
    assert details['symbol'] == 'ETH/USDT:USDT' and details['pos_side'] == 'LONG'
    assert details['cycle_state'] == cycle_state
    assert details['position_amount'] == pytest.approx(.3)
    assert 'ownership' in details['next_action'] and 'operation ID' in details['next_action']
    assert svc._load('ETH/USDT:USDT', 'LONG') == previous
    assert ex.calls == []


def test_real_close_pending_entry_does_not_skip_or_submit_exit(local_db):
    ex = Exchange()
    svc = IndependentExits(SimpleNamespace(exchange=ex, config_id='cfg'))
    opened = svc.open('ETH/USDT', entry(), operation_id='waiting-entry')
    with pytest.raises(ValueError, match='already filled'):
        svc.close('ETH/USDT', 'LONG', 0, operation_id='before-fill')
    assert svc._load('ETH/USDT:USDT', 'LONG')['state'] == 'WAITING'
    assert ex.orders[opened['id']]['status'] == 'open' and len(ex.calls) == 1


@pytest.mark.parametrize('response', [None, [{'side': 'long'}], [{'side': 'long', 'contracts': float('nan')}],
                                      [{'side': 'unknown', 'contracts': 1}]])
def test_real_close_invalid_position_read_is_not_flat(local_db, response):
    ex = Exchange()
    ex.fetch_positions = lambda symbols: response
    svc = IndependentExits(SimpleNamespace(exchange=ex, config_id='cfg'))
    with pytest.raises((ValueError, RuntimeError)):
        svc.close('ETH/USDT', 'LONG', 0, operation_id='unverified')
    assert ex.calls == []


def test_real_close_failed_position_read_is_not_flat(local_db):
    ex = Exchange()

    def failed_read(symbols):
        raise ccxt.RequestTimeout('position read timed out')

    ex.fetch_positions = failed_read
    svc = IndependentExits(SimpleNamespace(exchange=ex, config_id='cfg'))
    with pytest.raises(ccxt.RequestTimeout):
        svc.close('ETH/USDT', 'LONG', 0, operation_id='unverified')
    assert ex.calls == []


def test_attached_partial_reduce_is_proportional_and_expired_position_stays_visible(local_db):
    for oid,price in [('one',100),('two',120)]:
        database.create_mock_order('ETH/USDT','BUY',price,1,80,140,'cfg',config_id='cfg',order_id=oid,
                                   expire_at=time.time()-60)
        database.update_mock_order_filled(oid)
    assert len(database.get_mock_orders('ETH/USDT',config_id='cfg'))==2
    result=database._mock_trading_store.reduce_positions('cfg','ETH/USDT','LONG',.5,120)
    assert result['realized_pnl']==5
    rows=database.get_mock_orders('ETH/USDT',config_id='cfg')
    assert [row['amount'] for row in rows]==[.75,.75]
    database.close_mock_order('one',120,15)
    before=database.get_mock_account('cfg','ETH/USDT')['balance']
    database.close_mock_order('one',120,15)
    assert database.get_mock_account('cfg','ETH/USDT')['balance']==before


@pytest.mark.parametrize('previous_fill, ack_fill', [(0.0, None), (0.2, 0.1)],
                         ids=['cancel-ack-missing-fill', 'cancel-ack-regressed-fill'])
def test_real_cleanup_unverified_cancel_fill_retains_cycle_until_position_visible(
        live_service, monkeypatch, previous_fill, ack_fill):
    svc, ex = live_service
    add = svc.open('ETH/USDT', entry(80, .5), operation_id='late-fill-entry')
    if previous_fill:
        ex.orders[add['id']]['filled'] = previous_fill
        ex.quantity += previous_fill
        svc.reconcile_all()

    original_cancel, original_positions, original_query = ex.cancel_order, ex.fetch_positions, ex.fetch_order
    lag = {'hidden': False}
    canceled_ids, queried_ids = [], []

    def cancel(order_id, symbol, params):
        canceled_ids.append(str(order_id))
        result = original_cancel(order_id, symbol, params)
        if str(order_id) == add['id']:
            # The pending entry fills during cancellation, but the terminal ACK
            # omits its cumulative fill or reports less than already verified.
            ex.orders[str(order_id)]['filled'] = .5
            ex.quantity += .5 - previous_fill
            result['filled'] = ack_fill
            lag['hidden'] = True
        return result

    def positions(symbols):
        return [] if lag['hidden'] else original_positions(symbols)

    def query(order_id, symbol, params):
        queried_ids.append(str(order_id))
        return original_query(order_id, symbol, params)

    monkeypatch.setattr(ex, 'cancel_order', cancel)
    monkeypatch.setattr(ex, 'fetch_positions', positions)
    monkeypatch.setattr(ex, 'fetch_order', query)
    with pytest.raises(RuntimeError):
        svc.close('ETH/USDT', 'LONG', 0, 'market', operation_id='full-close')

    plan = svc._load('ETH/USDT:USDT', 'LONG')
    canceled_entry = next(record for record in plan['entries'] if record['id'] == add['id'])
    assert plan['state'] != 'DONE' and plan['error']
    assert canceled_entry['fill_pending'] is True
    assert canceled_entry['filled'] == pytest.approx(previous_fill)
    assert canceled_ids == [add['id']]
    assert ex.quantity == pytest.approx(.5 - previous_fill)
    submitted = len(ex.calls)

    restarted = IndependentExits(svc.mt)
    replay = restarted.close('ETH/USDT', 'LONG', 0, 'market', operation_id='full-close')
    assert replay['pending'] is True
    assert len(ex.calls) == submitted and canceled_ids == [add['id']]
    queried_ids.clear()
    restarted.reconcile_all()
    assert add['id'] in queried_ids
    plan = restarted._load('ETH/USDT:USDT', 'LONG')
    canceled_entry = next(record for record in plan['entries'] if record['id'] == add['id'])
    assert canceled_entry['filled'] == pytest.approx(.5)
    assert not canceled_entry.get('fill_pending')
    assert plan['cleanup_fill_unseen'] == pytest.approx(.5 - previous_fill)
    assert plan['state'] != 'DONE' and plan['error']
    restarted.reconcile_all()
    assert restarted.snapshot('ETH/USDT')['pending'] is True
    assert restarted._load('ETH/USDT:USDT', 'LONG')['state'] != 'DONE'
    assert len(ex.calls) == submitted and canceled_ids == [add['id']]

    lag['hidden'] = False
    restarted.reconcile_all()
    assert restarted._load('ETH/USDT:USDT', 'LONG')['state'] == 'ACTIVE'
    restarted.reconcile_all()
    plan = restarted._load('ETH/USDT:USDT', 'LONG')
    assert plan['state'] == 'ACTIVE' and plan['error'] is None
    assert not plan.get('cleanup_fill_unseen') and not plan.get('unobserved_entry_fills')
    assert ex.quantity == pytest.approx(.5 - previous_fill)
    assert len(ex.calls) == submitted and canceled_ids == [add['id']]


def test_cancel_ack_without_fill_keeps_terminal_status_through_stale_open_read(live_service):
    svc, ex = live_service
    stop = svc.close('ETH/USDT', 'LONG', 1, 'stop_market', trigger_price=90)
    original_cancel, original_query = ex.cancel_order, ex.fetch_order
    cancellations = []

    def cancel(*args, **kwargs):
        cancellations.append(args[0])
        response = original_cancel(*args, **kwargs)
        response['filled'] = None
        return response

    ex.cancel_order = cancel
    with pytest.raises(RuntimeError, match='cumulative fill is unverified'):
        svc.amend_exit('ETH/USDT', stop['id'], trigger_price=95, operation_id='amend-stop')

    def stale_query(*args, **kwargs):
        response = original_query(*args, **kwargs)
        if str(response['id']) == stop['id']:
            response.update(status='open', filled=0)
        return response

    ex.fetch_order = stale_query
    restarted = IndependentExits(svc.mt)
    assert restarted.reconcile_all()[0]['error']
    plan = restarted._load('ETH/USDT:USDT', 'LONG')
    assert plan['exits'][0]['status'] == 'canceled' and plan['exits'][0]['fill_pending']
    assert len(ex.calls) == 2 and cancellations == [stop['id']]
    ex.fetch_order = original_query
    restarted.reconcile_all()
    snapshot = restarted.snapshot('ETH/USDT')
    assert not snapshot['pending'] and snapshot['uncovered']['LONG'] == 0
    assert snapshot['exits'][0]['trigger_price'] == 95
    assert len(ex.calls) == 3 and cancellations == [stop['id']]
