import json

import pytest

import backend.database as database
from backend.agent import agent_tools
from backend.database_schema import initialize_schema
from backend.utils.execution_ledger import account_scope
from backend.utils.market_data import MarketTool
from backend.utils.order_ownership import assert_owned_perpetual_order


class Exchange:
    id='binanceusdm'
    apiKey='offline-ownership-test'
    markets={'BTC/USDT:USDT':{}}
    options={'defaultType':'swap'}

    def __init__(self):
        self.cancelled=[]

    def market(self,symbol):
        return {'symbol':symbol if ':' in symbol else symbol+':USDT'}

    def cancel_order(self,order_id,symbol,params=None):
        self.cancelled.append((order_id,symbol,params))
        return {'id':order_id,'status':'canceled'}


@pytest.fixture
def tool(tmp_path,monkeypatch):
    monkeypatch.setattr(database,'DB_NAME',str(tmp_path/'ownership.db'))
    with database.get_db_conn() as conn:
        initialize_schema(conn)
    value=MarketTool.__new__(MarketTool)
    value.exchange=Exchange()
    value.config_id='cfg'
    value.market_type='swap'
    return value


def legacy(oid='owned',cid='cfg',symbol='BTC/USDT',mode='REAL'):
    database.save_order_log(oid,symbol,cid,'BUY',100,120,90,'test',trade_mode=mode,
                            config_id=cid,amount=1,event_type='ORDER_CREATED')


def test_unknown_foreign_wrong_symbol_and_mock_ids_never_reach_exchange(tool):
    legacy('foreign','other')
    legacy('wrong-symbol',symbol='ETH/USDT')
    legacy('mock',mode='STRATEGY')
    for oid in ('unknown','foreign','wrong-symbol','mock'):
        with pytest.raises(ValueError,match='ownership'):
            tool.place_real_order('BTC/USDT','CANCEL',{'cancel_order_id':oid})
    assert tool.exchange.cancelled==[]


def test_execution_link_conflict_overrides_legacy_claim(tool):
    legacy('same-id')
    with database.get_db_conn() as conn:
        conn.execute('INSERT INTO execution_order_links VALUES(?,?,?,?,?,?)',
                     (account_scope(tool.exchange,'cfg'),'BTC/USDT:USDT','same-id','other','entry','{}'))
        conn.commit()
    with pytest.raises(ValueError,match='another configuration'):
        tool.place_real_order('BTC/USDT','CANCEL',{'cancel_order_id':'same-id'})
    assert tool.exchange.cancelled==[]


def test_scoped_ledger_plan_and_legacy_orders_are_supported(tool):
    scope=account_scope(tool.exchange,'cfg')
    with database.get_db_conn() as conn:
        conn.execute('INSERT INTO execution_order_links VALUES(?,?,?,?,?,?)',
                     (scope,'BTC/USDT:USDT','linked','cfg','entry','{}'))
        plan={'account_scope':scope,'entries':[{'id':'planned'}],'state':'WAITING'}
        conn.execute('INSERT INTO real_protection_plans VALUES(?,?,?,?)',
                     ('cfg','BTC/USDT:USDT','LONG',json.dumps(plan)))
        conn.commit()
    legacy('legacy')
    for oid in ('linked','planned','legacy'):
        result=tool.place_real_order('BTC/USDT','CANCEL',{'cancel_order_id':oid})
        assert result['status']=='cancelled'
    assert len(tool.exchange.cancelled)==3


def test_old_account_plan_cannot_authorize_current_account_cancel(tool):
    legacy('old-account')
    with database.get_db_conn() as conn:
        plan={'account_scope':'different-account','entries':[{'id':'old-account'}],'state':'WAITING'}
        conn.execute('INSERT INTO real_protection_plans VALUES(?,?,?,?)',
                     ('cfg','BTC/USDT:USDT','LONG',json.dumps(plan)))
        conn.commit()
    with pytest.raises(ValueError,match='previous exchange credentials'):
        assert_owned_perpetual_order(tool,'BTC/USDT','old-account')
    assert tool.exchange.cancelled==[]


def test_tool_cancel_updates_only_owned_config_and_symbol(tool,monkeypatch):
    legacy('same-id')
    legacy('same-id','other')
    legacy('same-id','cfg','ETH/USDT')
    monkeypatch.setattr(agent_tools,'MarketTool',lambda **kwargs:tool)
    monkeypatch.setattr('backend.config.config.get_config_by_id',lambda cid:{'mode':'REAL','model':'offline'})
    result=agent_tools.cancel_orders_real.func('same-id','test','cfg','BTC/USDT')
    assert 'Cancelled Real' in result
    with database.get_db_conn() as conn:
        rows=conn.execute("SELECT config_id,symbol,status FROM orders WHERE event_type='ORDER_CREATED'").fetchall()
    states={(row['config_id'],row['symbol']):row['status'] for row in rows}
    assert states=={('cfg','BTC/USDT'):'CANCELLED',('other','BTC/USDT'):'OPEN',('cfg','ETH/USDT'):'OPEN'}


def test_spot_cancel_preserves_legacy_behavior_without_futures_ownership(tool):
    tool.market_type='spot'
    tool.place_real_order('BTC/USDT','CANCEL',{'cancel_order_id':'spot-legacy'})
    assert len(tool.exchange.cancelled)==1


def test_attached_conflict_cleanup_does_not_cancel_unknown_exchange_trigger(tool):
    from backend.utils.position_protection import PositionProtection
    tool.exchange.fetch_position_mode=lambda symbol:{'hedged':True}
    tool.exchange.fetch_open_orders=lambda symbol,params=None:[{
        'id':'manual-trigger','side':'sell','type':'STOP_MARKET',
        'info':{'positionSide':'LONG','closePosition':True,'orderType':'STOP_MARKET'},
    }]
    PositionProtection(tool)._cancel_conflicting_exchange_triggers(
        {'symbol':'BTC/USDT:USDT','side':'LONG','legs':[]},'sl')
    assert tool.exchange.cancelled==[]


def test_generic_exchange_spot_alias_resolves_perpetual_plan(tool):
    scope=account_scope(tool.exchange,'cfg')
    def market(symbol):
        if symbol=='BTC/USDT':
            return {'symbol':symbol,'contract':False}
        return {'symbol':'BTC/USDT:USDT','contract':True,'linear':True}
    tool.exchange.market=market
    with database.get_db_conn() as conn:
        plan={'account_scope':scope,'legs':[{'id':'sl-only-plan','kind':'sl'}],'state':'ACTIVE'}
        conn.execute('INSERT INTO real_protection_plans VALUES(?,?,?,?)',
                     ('cfg','BTC/USDT:USDT','LONG',json.dumps(plan)))
        conn.commit()
    result=assert_owned_perpetual_order(tool,'BTC/USDT','sl-only-plan')
    assert result['source']=='real_protection_plans' and result['trigger']
