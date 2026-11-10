"""Regressions for losses without entries, cancellation cohorts and auto exits."""
import json

import pytest

from backend import database
from backend.database.execution_events import record_plan_execution_events
from backend.database.trade_calls import list_trade_calls
from backend.utils.task_performance import summarize_history


def exit_fill(order, pnl, when=2, fee=.1):
    return dict(symbol='ETH/USDT:USDT',order_id=order,role='stop_loss',side='SHORT',
                amount=.1,realized_pnl=pnl,timestamp_ms=when,fees=[{'currency':'USDT','cost':fee}])


def summarize(fills, **kwargs):
    return summarize_history(fills,[],since_ms=0,end_ms=10,currency='USDT',**kwargs)


def test_all_losses_without_entries_have_zero_win_rate_and_honest_entry_diagnostic():
    row = summarize([exit_fill('a',-10),exit_fill('b',-9.87)])
    assert row['metrics']['win_rate']['value'] == 0
    assert row['closed_orders'] == row['losing_orders'] == 2
    assert row['realized_pnl_after_fees'] == pytest.approx(-20.07)
    assert row['metrics']['open_count']['value'] is None
    assert row['diagnostics']['missing_entry_history']


def test_partial_fills_count_once_and_fees_can_turn_gross_profit_into_loss():
    row = summarize([exit_fill('a',-4),exit_fill('a',10,3),exit_fill('b',.05),exit_fill('flat',.1)])
    assert row['closed_orders'] == 3
    assert row['winning_orders'] == row['losing_orders'] == row['breakeven_orders'] == 1
    assert row['win_rate'] == pytest.approx(100/3)
    assert row['profit_loss_ratio'] == pytest.approx(5.8/.05)


def test_incomplete_exit_is_separate_from_known_loss_and_never_treated_as_zero():
    row = summarize([exit_fill('a',-1),{**exit_fill('b',10),'fees':[]}])
    assert row['win_rate'] == 0 and row['unknown_orders'] == 1
    assert row['metrics']['win_rate']['status'] == 'partial'


def test_split_order_uses_last_execution_date_and_combines_earlier_fills():
    row = summarize_history([exit_fill('a',10,1),exit_fill('a',-20,8)],[],since_ms=5,end_ms=10,currency='USDT')
    assert row['closed_orders'] == 1 and row['win_rate'] == 0


def test_cancel_rate_counts_orders_instead_of_attempts_and_keeps_long_short_zero():
    entry = dict(exit_fill('entry',0),role='entry',side='LONG')
    orders = [dict(symbol='ETH/USDT',order_id='a',timestamp_ms=1,status='open'),
              dict(symbol='ETH/USDT',order_id='a',timestamp_ms=1,status='canceled',observed_at_ms=2),
              dict(symbol='ETH/USDT',order_id='b',timestamp_ms=2,status='filled'),
              dict(symbol='ETH/USDT',order_id='reject',timestamp_ms=2,status='rejected')]
    row = summarize([entry],orders=orders)
    assert row['cancel_rate'] == 50 and row['accepted_order_count'] == 2
    assert row['metrics']['long_short_ratio']['value'] == '1:0'
    assert summarize([])['cancel_rate'] is None


def plan():
    return dict(symbol='ETH/USDT:USDT',side='SHORT',account_scope='scope',updated_at=1794000000,
                contract_size=.1,exits=[dict(id='10',contract_version=2,order_type='take_profit_market',
                amount=2,trigger_price=2055,triggered=True,status='open',filled=0,fill_pending=True)])


def test_auto_trigger_is_durable_updates_one_event_and_never_claims_unverified_fill(pg):
    p = plan()
    with pg() as conn:
        record_plan_execution_events(conn,'task',p)
        row = conn.execute("SELECT * FROM agent_trade_calls WHERE config_id='task'").fetchone()
        assert row['tool_name'] == 'automatic_exit' and row['status']=='pending'
        first_time = row['created_at']
        assert json.loads(row['result_json'])['filled_amount'] is None
        p['exits'][0].update(execution_order_id='20',status='closed',fill_pending=False,fill_verified=True,filled=2)
        p['updated_at'] += 60
        record_plan_execution_events(conn,'task',p)
        record_plan_execution_events(conn,'task',p)
        rows = conn.execute("SELECT * FROM agent_trade_calls WHERE config_id='task'").fetchall()
        assert len(rows)==1 and rows[0]['status']=='filled' and rows[0]['created_at']==first_time
        assert json.loads(rows[0]['result_json'])['filled_amount']==.2
        # Stale snapshots cannot turn a confirmed fill back into pending.
        p['exits'][0].update(status='open',fill_pending=True,filled=0)
        record_plan_execution_events(conn,'task',p)
        assert conn.execute("SELECT status FROM agent_trade_calls WHERE config_id='task'").fetchone()[0]=='filled'


def test_waiting_trigger_is_not_an_automatic_execution(pg):
    p=plan()
    p['exits'][0].update(triggered=False,fill_pending=False)
    with pg() as conn:
        record_plan_execution_events(conn,'task',p)
        assert conn.execute('SELECT COUNT(*) FROM agent_trade_calls').fetchone()[0]==0


def test_historical_auto_execution_backfills_only_current_task_account_and_symbol(pg,monkeypatch):
    from backend.utils.execution_ledger import register_order
    monkeypatch.setattr('backend.config.config.get_config_by_id',lambda _: {'mode':'REAL','symbol':'ETH/USDT'})
    monkeypatch.setattr('backend.utils.trade_evidence.configured_account_scope',lambda _: 'scope')
    for account,owner,symbol,identity in [('scope','task','ETH/USDT:USDT','20'),('foreign','task','ETH/USDT:USDT','21'),
                                          ('scope','other','ETH/USDT:USDT','22'),('scope','task','BTC/USDT:USDT','23')]:
        register_order(account,symbol,identity,owner,'stop_loss',refresh=False,side='SHORT')
        with pg() as conn:
            conn.execute('INSERT INTO execution_fills(account_scope,symbol,trade_id,order_id,config_id,timestamp_ms,payload) VALUES(%s,%s,%s,%s,%s,1794000000000,%s)',
                         (account,symbol,identity,identity,owner,json.dumps(dict(amount=.1,base_amount=.1,price=2055))))
            conn.commit()
    data=list_trade_calls('task')
    assert data['total']==1 and data['items'][0]['source']=='automatic'
    assert data['items'][0]['result']['order_id']=='regular:20'
    assert list_trade_calls('task')['total']==1


def test_spot_auto_oco_records_confirmed_fill_without_guessing_trigger_leg(pg,monkeypatch):
    monkeypatch.setattr('backend.config.config.get_config_by_id',lambda _: {'mode':'SPOT_DCA','symbol':'ETH/USDT'})
    monkeypatch.setattr('backend.mcp.spot_orders._configured_account_scope',lambda _: 'spot-account')
    with pg() as conn:
        conn.execute('''INSERT INTO mcp_spot_exits(exit_id,config_id,symbol,account_scope,operation_id,
            request_hash,kind,amount,filled,state,payload,created_at,updated_at)
            VALUES('oco','task','ETH/USDT','spot-account','protect','hash','oco',1,1,'filled',%s,1794000000,1794000060)''',
            (json.dumps(dict(fill_verified=True,fill_observed_at=1794000060,request=dict(stop_loss=1900,take_profit=2200))),))
        conn.commit()
    data=list_trade_calls('task')
    assert data['total']==1 and data['items'][0]['source']=='automatic'
    assert data['items'][0]['arguments']['trigger_kind']=='oco'
    assert data['items'][0]['result']['filled_amount']==1


def test_spot_cancel_cost_requires_actual_fill_evidence_and_preserves_other_tasks(pg):
    from backend.utils.spot_management import _record_cancel_observation
    with pg() as conn:
        conn.execute("INSERT INTO orders(order_id,config_id,symbol,trade_mode,side,status,amount,entry_price) VALUES('o','task','ETH/USDT','SPOT_DCA','BUY','OPEN',1,2000)")
        conn.commit()
    _record_cancel_observation('task','ETH/USDT',dict(id='o',status='canceled',filled=.1,price=2000))
    with pg() as conn:
        assert conn.execute('SELECT COUNT(*) FROM spot_order_fills').fetchone()[0]==0
    _record_cancel_observation('other','ETH/USDT',dict(id='o',status='canceled',filled=0))
    with pg() as conn:
        assert conn.execute('SELECT COUNT(*) FROM spot_order_fills').fetchone()[0]==0
    _record_cancel_observation('task','ETH/USDT',dict(id='o',status='canceled',filled=.1,cost=199))
    with pg() as conn:
        row=conn.execute("SELECT * FROM spot_order_fills WHERE config_id='task'").fetchone()
        assert row['filled_qty']==.1 and row['filled_cost']==199


def test_indexed_history_survives_removed_exchange_credentials(pg,monkeypatch):
    from backend.database.trade_calls import record_trade_call
    record_trade_call('task','cancel','cancel_order',{'order_id':'regular:1','reason':'撤销'},'ETH/USDT',{'status':'completed'})
    monkeypatch.setattr('backend.config.config.get_config_by_id',lambda _: {'mode':'REAL','symbol':'ETH/USDT'})
    def unavailable(_):
        raise ValueError('Account credentials unavailable')
    monkeypatch.setattr('backend.utils.trade_evidence.configured_account_scope',unavailable)
    assert list_trade_calls('task')['total']==1


def test_mock_conditional_trigger_without_fill_is_recorded_once(pg,monkeypatch):
    monkeypatch.setattr('backend.config.config.get_config_by_id',lambda _: {'mode':'STRATEGY','symbol':'ETH/USDT'})
    with pg() as conn:
        conn.execute('''INSERT INTO mock_exit_orders(order_id,config_id,symbol,pos_side,episode_id,exit_type,
            order_type,price,trigger_price,amount,remaining,status,timestamp,created_at,triggered_at,execution_order_id)
            VALUES('parent','task','ETH/USDT','SHORT','episode','take_profit_limit','take_profit_limit',2056,2055,.1,.1,
                   'OPEN','2026-11-10 10:00:00',1794000000,1794000060000,'child')''')
        conn.commit()
    row=list_trade_calls('task')['items'][0]
    assert row['source']=='automatic' and row['status']=='pending'
    assert row['result']['filled_amount']==0 and row['result']['order_id']=='child'
    assert list_trade_calls('task')['total']==1
