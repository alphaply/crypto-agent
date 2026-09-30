import time
from types import SimpleNamespace

import ccxt
import pytest

import backend.database as database
from backend.database_schema import initialize_schema
from backend.database_independent import MockIndependentTrading
from backend.utils.independent_exits import IndependentExits
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
