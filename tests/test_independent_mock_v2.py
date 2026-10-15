"""Isolated simulated acceptance cases for the independent v2 contract."""
import time
from types import SimpleNamespace

import pytest

import backend.database as database
from backend.database_independent import MockIndependentTrading
from backend.database_schema import initialize_schema


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(database, 'DB_NAME', str(tmp_path / 'mock_v2.db'))
    with database.get_db_conn() as conn:
        initialize_schema(conn)
    return MockIndependentTrading('cfg', 'ETH/USDT')


def open_entry(store, amount=1, price=100, side='LONG', operation_id=None):
    op=SimpleNamespace(action='BUY_LIMIT' if side=='LONG' else 'SELL_LIMIT',
                       entry_price=price,amount=amount,stop_loss=None,take_profit=None,
                       reason='scale in',valid_duration_hours=24)
    return store.open(op,operation_id=operation_id,contract_version=2)


def bar(store, high=100, low=100, step=1):
    return store.monitor(high,low,int((time.time()+120*step)*1000))


def close(store, amount=None, kind='market', side='LONG', **kwargs):
    return store.close(side,amount,kind,100,contract_version=2,**kwargs)


def test_two_fixed_stages_then_dynamic_all_closes_only_target_direction(store):
    first=open_entry(store,.4)
    open_entry(store,.6)
    open_entry(store,.8,side='SHORT')
    assert first['status']=='submitted' and first['order_status']=='open'
    assert bar(store)['filled']==3
    a=close(store,.2,'take_profit_market',trigger_price=110)
    b=close(store,.3,'take_profit_market',trigger_price=120)
    final=close(store,kind='take_profit_market',trigger_price=130,close_all=True)
    assert final['order_id'].startswith('algo:')
    assert final['amount'] is None and final['remaining'] is None
    assert final['quantity_mode']=='all' and final['order_status']=='waiting_trigger'
    assert bar(store,high=111,low=100,step=2)['closed']==1
    assert bar(store,high=121,low=115,step=3)['closed']==1
    assert bar(store,high=135,low=125,step=4)['closed']==1
    snapshot=store.snapshot()
    assert [(p['pos_side'],p['amount']) for p in snapshot['positions']]==[('SHORT',.8)]
    assert store.describe_order(a['order_id'])['filled_amount']==pytest.approx(.2)
    assert store.describe_order(b['order_id'])['filled_amount']==pytest.approx(.3)
    actual=store.describe_order(final['order_id'])
    assert actual['filled_amount']==pytest.approx(.5)
    assert actual['order_status']=='filled' and actual['order_id'].startswith('regular:')
    assert actual['amount'] is None and actual['remaining'] is None and actual['actual_remaining']==0


def test_split_stop_losses_gap_fixed_fixed_all_and_no_duplicate_fills(store):
    open_entry(store)
    bar(store)
    close(store,.2,'stop_market',trigger_price=95)
    close(store,.3,'stop_market',trigger_price=90)
    final=close(store,kind='stop_market',trigger_price=85,close_all=True)
    pending=open_entry(store,.4,price=70)
    assert bar(store,high=100,low=60,step=2)=={'filled':0,'closed':3}
    assert store.describe_order(final['order_id'])['filled_amount']==pytest.approx(.5)
    assert store.describe_order(pending['order_id'])['order_status']=='canceled'
    assert bar(store,high=100,low=60,step=3)=={'filled':0,'closed':0}
    with database.get_db_conn() as conn:
        assert conn.execute('SELECT balance FROM mock_accounts').fetchone()[0]==pytest.approx(9988.5)


def test_all_triggering_first_cleans_other_stages_without_inventing_their_fills(store):
    open_entry(store)
    bar(store)
    fixed=close(store,.7,'take_profit_market',trigger_price=120)
    all_order=close(store,kind='take_profit_market',trigger_price=110,close_all=True)
    assert bar(store,high=140,low=105,step=2)['closed']==1
    assert store.describe_order(all_order['order_id'])['filled_amount']==1
    canceled=store.describe_order(fixed['order_id'])
    assert canceled['filled_amount']==0 and canceled['unexecuted_amount']==.7
    assert canceled['order_status']=='canceled' and store.snapshot()['positions']==[]


def test_dynamic_all_survives_restart_and_later_addition(store):
    open_entry(store)
    bar(store)
    fixed=close(store,.2,'stop_market',trigger_price=90)
    final=close(store,kind='stop_market',trigger_price=80,close_all=True,operation_id='all')
    open_entry(store,.4,price=95)
    bar(store,high=100,low=95,step=2)
    restarted=MockIndependentTrading('cfg','ETH/USDT')
    assert close(restarted,kind='stop_market',trigger_price=80,close_all=True,operation_id='all')==final
    snapshot=restarted.snapshot()
    assert not snapshot['pending'] and snapshot['uncovered']['LONG']==0
    dynamic=next(row for row in snapshot['exits'] if row['quantity_mode']=='all')
    assert dynamic['amount'] is None and dynamic['coverage_estimate']==pytest.approx(1.4)
    assert restarted.describe_order(fixed['order_id'])['remaining_amount']==.2
    bar(restarted,high=100,low=70,step=3)
    assert restarted.describe_order(final['order_id'])['filled_amount']==pytest.approx(1.2)


def test_overlapping_fixed_exits_keep_plan_and_record_only_actual_fills(store):
    open_entry(store)
    bar(store)
    a=close(store,.7,'limit',price=110)
    b=close(store,.6,'limit',price=120)
    assert store.snapshot()['quantity_conflicts'][0]['excess_amount']==pytest.approx(.3)
    bar(store,high=111,low=100,step=2)
    assert store.describe_order(b['order_id'])['remaining_amount']==.6
    assert not store.snapshot()['pending']
    bar(store,high=121,low=115,step=3)
    actual=store.describe_order(b['order_id'])
    assert actual['amount']==.6 and actual['filled_amount']==pytest.approx(.3)
    assert actual['unexecuted_amount']==pytest.approx(.3)
    assert actual['order_status']=='canceled'
    assert store.snapshot()['positions']==[]
    with database.get_db_conn() as conn:
        assert conn.execute('SELECT balance FROM mock_accounts').fetchone()[0]==pytest.approx(10013)
        assert conn.execute("SELECT SUM(amount) FROM orders WHERE event_type='TP_HIT'").fetchone()[0]==pytest.approx(1)


@pytest.mark.parametrize('kind,side,trigger,limit,first_bar,second_bar',[
    ('stop_limit','LONG',90,95,(92,80),(98,94)),
    ('take_profit_limit','LONG',110,120,(125,105),(121,115)),
    ('stop_limit','SHORT',110,105,(120,108),(106,104)),
    ('take_profit_limit','SHORT',90,80,(95,75),(85,79)),
])
def test_condition_limit_trigger_and_fill_are_separate(store,kind,side,trigger,limit,first_bar,second_bar):
    open_entry(store,side=side)
    bar(store)
    order=close(store,.5,kind,side,trigger_price=trigger,price=limit)
    assert bar(store,*first_bar,step=2)['closed']==0
    actual=store.describe_order(order['order_id'])
    assert actual['order_status']=='open' and actual['filled_amount']==0
    assert actual['parent_algo_id']==order['id'] and actual['execution_order_id']
    # Re-reading the trigger bar cannot retroactively match its earlier extreme.
    trigger_ts=actual['triggered_at']
    assert store.monitor(*first_bar,trigger_ts)['closed']==0
    assert bar(store,*second_bar,step=3)['closed']==1
    assert store.describe_order(order['order_id'])['filled_amount']==.5


def test_triggered_parent_edit_requires_explicit_child_and_cancel_follows_child(store):
    open_entry(store)
    bar(store)
    order=close(store,.7,'stop_limit',trigger_price=90,price=99)
    bar(store,high=95,low=85,step=2)
    result=store.edit(order['order_id'],trigger_price=85,current_price=100)
    assert result['code']=='order_already_triggered'
    child=result['order_id']
    assert child.startswith('regular:')
    with pytest.raises(ValueError,match='trigger_price'):
        store.edit(child,trigger_price=85,current_price=100)
    edited=store.edit(child,remaining_amount=.4,price=97,current_price=100)
    assert edited['edit_method']=='native' and edited['remaining_amount']==.4
    canceled=store.cancel(order['order_id'])
    assert canceled['order_id']==child and canceled['order_status']=='canceled'
    assert store.snapshot()['positions'][0]['amount']==1


def test_partially_filled_child_edit_converts_remaining_to_total_without_losing_fills(store):
    open_entry(store)
    bar(store)
    order=close(store,.7,'stop_limit',trigger_price=90,price=99)
    bar(store,high=95,low=85,step=2)
    child=store.describe_order(order['order_id'])['order_id']
    # Load an already partially executed child, as a persisted lifecycle view.
    with database.get_db_conn() as conn:
        conn.execute('UPDATE mock_exit_orders SET filled_amount=.2,remaining=.5 WHERE order_id=?',(order['id'],))
        conn.execute('UPDATE mock_positions SET quantity=.8')
        conn.commit()
    edited=store.edit(child,remaining_amount=.4,price=98)
    assert edited['amount']==pytest.approx(.6)
    assert edited['remaining_amount']==.4 and edited['filled_amount']==.2
    canceled=store.cancel(order['order_id'])
    assert canceled['order_id']==child and canceled['filled_amount']==.2
    assert canceled['unexecuted_amount']==.4 and canceled['order_status']=='canceled'


def test_conditional_all_edit_replaces_identity_and_preserves_dynamic_quantity(store):
    open_entry(store)
    bar(store)
    order=close(store,kind='stop_market',trigger_price=90,close_all=True)
    with pytest.raises(ValueError,match='all orders'):
        store.edit(order['order_id'],remaining_amount=.2,current_price=100)
    changed=store.edit(order['order_id'],trigger_price=95,current_price=100,operation_id='edit')
    assert changed['edit_method']=='cancel_replace'
    assert changed['previous_order_id']==order['order_id']
    assert changed['order_id']!=order['order_id'] and changed['amount'] is None
    assert changed['quantity_mode']=='all' and changed['status']=='completed'
    assert store.describe_order(order['order_id'])['order_status']=='canceled'
    assert store.edit(order['order_id'],trigger_price=95,current_price=100,operation_id='edit')==changed
    assert len(store.snapshot()['exits'])==1


def test_close_replay_precedes_fresh_market_trigger_validation(store):
    open_entry(store)
    bar(store)
    order=close(store,.5,'stop_market',trigger_price=90,operation_id='stop')
    # A later price below the old trigger must not invalidate a saved receipt.
    assert store.close('LONG',.5,'stop_market',80,trigger_price=90,
                       contract_version=2,operation_id='stop')==order


def test_entry_edit_remaining_and_legacy_total_are_distinct(store):
    order=open_entry(store)
    with database.get_db_conn() as conn:
        conn.execute('UPDATE mock_orders SET filled_amount=.2 WHERE order_id=?',(order['id'],))
        conn.execute("UPDATE mock_positions SET quantity=.2,entry_price=100,status='ACTIVE'")
        conn.commit()
    changed=store.edit(order['order_id'],remaining_amount=.5,price=99,operation_id='edit')
    assert changed['amount']==pytest.approx(.7) and changed['remaining_amount']==pytest.approx(.5)
    assert changed['filled_amount']==.2 and changed['edit_method']=='native'
    legacy=store.amend_entry(order['order_id'],amount=.6,operation_id='legacy')
    assert legacy['amount']==.6 and legacy['remaining_amount']==pytest.approx(.4)
    assert store.describe_order(order['order_id'])['kind']=='entry'
    with pytest.raises(ValueError,match='identity_conflict'):
        store.amend_entry(order['order_id'],amount=.5,operation_id='edit')


@pytest.mark.parametrize('kwargs',[
    dict(kind='limit',close_all=True,price=110),
    dict(kind='stop_limit',close_all=True,trigger_price=90,price=89),
    dict(kind='take_profit_limit',amount=.5,price=120),
    dict(kind='limit',amount=.5,price=110,trigger_price=109),
    dict(kind='market',amount=True),dict(kind='market',amount=0),
    dict(kind='stop_market',amount=.2,close_all=True,trigger_price=90),
])
def test_invalid_requests_have_no_exit_write(store,kwargs):
    open_entry(store)
    bar(store)
    with pytest.raises(ValueError):
        close(store,**kwargs)
    assert store.snapshot()['exits']==[]
    with database.get_db_conn() as conn:
        assert conn.execute('SELECT COUNT(*) FROM mock_exit_orders').fetchone()[0]==0


def test_legacy_limit_and_zero_snapshot_survive_position_change_and_replay(store):
    open_entry(store)
    bar(store)
    legacy=store.close('LONG',0,'take_profit_limit',100,price=110,operation_id='legacy')
    assert legacy['order_type']=='limit' and legacy['quantity_mode']=='fixed' and legacy['amount']==1
    open_entry(store,.4)
    bar(store)
    assert store.close('LONG',0,'take_profit_limit',100,price=110,operation_id='legacy')==legacy
    with pytest.raises(ValueError,match='identity_conflict'):
        close(store,1,'limit',price=110,operation_id='legacy')
    bar(store,high=112,low=105,step=2)
    assert store.snapshot()['positions'][0]['amount']==pytest.approx(.4)


def test_market_all_records_resolved_snapshot_and_no_tp_sl_requirement(store):
    open_entry(store)
    assert store.snapshot()['exits']==[]
    bar(store)
    result=close(store,close_all=True,operation_id='all-now')
    assert result['status']=='completed' and result['order_status']=='filled'
    assert result['quantity_mode']=='all' and result['requested_amount'] is None
    assert result['resolved_amount']==1 and result['filled_amount']==1
    assert result['remaining'] is None and result['actual_remaining']==0
    assert close(store,close_all=True,operation_id='all-now')==result


def test_quantity_conflict_does_not_block_explicit_edit_or_cancel(store):
    open_entry(store)
    bar(store)
    a=close(store,.7,'limit',price=110)
    b=close(store,.6,'limit',price=120)
    assert store.snapshot()['quantity_conflict']
    store.edit(a['order_id'],remaining_amount=.4)
    assert not store.snapshot()['quantity_conflict']
    store.cancel(b['order_id'])
    assert len(store.snapshot()['exits'])==1


def test_marketable_regular_limit_allowed_without_profit_side_rule(store):
    open_entry(store)
    bar(store)
    order=close(store,.3,'limit',price=95)
    bar(store,high=100,low=99,step=2)
    assert store.describe_order(order['order_id'])['filled_amount']==.3


def test_no_exits_for_unfilled_entries_or_other_task(store):
    open_entry(store,price=90)
    with pytest.raises(ValueError,match='already filled'):
        close(store,.5)
    bar(store,high=100,low=85)
    owned=close(store,.5,'limit',price=110)
    stranger=MockIndependentTrading('other','ETH/USDT')
    with pytest.raises(ValueError,match='owned'):
        stranger.edit(owned['order_id'],price=120)
    with pytest.raises(ValueError,match='owned'):
        stranger.cancel(owned['order_id'])


def test_qualified_channel_identity_prevents_ambiguous_bare_id_writes(store):
    open_entry(store)
    bar(store)
    stop=close(store,.5,'stop_market',trigger_price=90)
    pending=open_entry(store,.2,price=80)
    # Local IDs normally use different prefixes; model the exchange's equal
    # numeric IDs to ensure ownership lookup still resolves channel first.
    with database.get_db_conn() as conn:
        conn.execute('UPDATE mock_orders SET order_id=? WHERE order_id=?',(stop['id'],pending['id']))
        conn.commit()
    with pytest.raises(ValueError,match='ambiguous'):
        store.edit(stop['id'],price=85,current_price=100)
    with pytest.raises(ValueError,match='ambiguous'):
        store.cancel(stop['id'])
    with pytest.raises(ValueError,match='ambiguous'):
        store.describe_order(stop['id'])
    assert store.describe_order('regular:'+stop['id'])['kind']=='entry'
    assert store.describe_order('algo:'+stop['id'])['kind']=='exit'
    store.cancel('algo:'+stop['id'])
    assert store.describe_order('regular:'+stop['id'])['order_status']=='open'


def test_waiting_trigger_blocks_mode_switch_without_other_position_blockers(store):
    from backend.utils.exit_policy import ExitModeConflict, assert_exit_mode_change_allowed
    open_entry(store)
    bar(store)
    close(store,kind='stop_market',trigger_price=90,close_all=True)
    with database.get_db_conn() as conn:
        conn.execute('UPDATE mock_positions SET quantity=0')
        conn.commit()
    config=dict(config_id='cfg',mode='STRATEGY',exit_mode='independent_exits')
    with pytest.raises(ExitModeConflict):
        assert_exit_mode_change_allowed(config,{**config,'exit_mode':'attached_required'})


def test_waiting_trigger_keeps_disabled_config_scheduled_for_maintenance(store,monkeypatch):
    import concurrent.futures
    from backend.app.core import scheduler
    from backend.mcp import settings
    from backend.utils import execution_stream
    open_entry(store)
    bar(store)
    close(store,kind='stop_market',trigger_price=90,close_all=True)
    with database.get_db_conn() as conn:
        conn.execute('UPDATE mock_positions SET quantity=0')
        conn.commit()
    config=dict(config_id='cfg',mode='STRATEGY',symbol='ETH/USDT',enabled=False)
    calls=[]
    def submit(fn,cfg):
        calls.append(cfg)
        return concurrent.futures.Future()
    monkeypatch.setattr(scheduler,'_protection_executor',SimpleNamespace(submit=submit))
    monkeypatch.setattr(scheduler,'submit_capacity',lambda executor,role,fn,*args,**kwargs:executor.submit(fn,*args))
    monkeypatch.setattr(scheduler,'_protection_futures',{})
    monkeypatch.setattr(scheduler,'_disabled_mock_maintenance_at',{})
    monkeypatch.setattr(scheduler.global_config,'get_all_symbol_configs',lambda:[config])
    monkeypatch.setattr(settings,'maintenance_profiles',lambda:[])
    monkeypatch.setattr(execution_stream,'stop_unused_streams',lambda:None)
    scheduler.protection_tick()
    scheduler.protection_tick()
    assert calls==[config]
