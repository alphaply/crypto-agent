"""Task-only statistics, durable reasons, calibration and return regressions."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from backend import database
from backend.database.trade_calls import record_trade_call, list_trade_calls
from backend.utils.decision_journal import decision_journal
from backend.utils.spot_valuation import value_spot_events
from backend.utils.task_performance import summarize_history, task_performance, task_history_context
from backend.app.services.spot_history_service import spot_history_points
from backend.app.services.dashboard_service import _equity_series_metadata
from test_spot_cost_basis_stats import spot
from test_spot_cost_basis_api import api


def fill(oid, when, *, role='entry', pnl=0, fee=.1, currency='USDT', **kw):
    return dict(order_id=oid, timestamp_ms=when, role=role, realized_pnl=pnl, symbol='BTC/USDT',
                side='LONG', amount=1, fees=[dict(cost=fee,currency=currency)] if fee is not None else [], **kw)


def test_partial_and_replacement_orders_count_once_at_first_execution():
    rows=[fill('old',1),fill('old',5),fill('new',6,logical_order_id='old'),fill('add',7)]
    weekly=summarize_history(rows,[],since_ms=4,end_ms=10,currency='USDT')
    all_time=summarize_history(rows,[],since_ms=0,end_ms=10,currency='USDT')
    assert weekly['open_count']==1 and all_time['open_count']==2


def test_metric_diagnostics_no_fills_open_only_and_missing_entry():
    empty = summarize_history([], [], since_ms=0, end_ms=10, currency='USDT')
    assert empty['metrics']['open_count']['value'] == 0
    assert empty['metrics']['win_rate']['reason'] == 'no_trades'
    opened = summarize_history([fill('open', 1)], [], since_ms=0, end_ms=10, currency='USDT')
    assert opened['metrics']['win_rate']['reason'] == 'no_exit_orders'
    missing = summarize_history([fill('exit', 8, role='exit', pnl=-30)], [], since_ms=0, end_ms=10, currency='USDT')
    assert missing['metrics']['open_count']['value'] is None
    assert missing['metrics']['win_rate']['value'] == 0
    assert missing['diagnostics']['missing_entry_history']
    assert missing['realized_pnl_after_fees'] == pytest.approx(-30.1)


def test_close_in_current_window_uses_old_entry_and_detects_missing_fees():
    cycle = dict(closed_at_ms=8, realized_pnl_before_fees=10, fees_by_currency={'USDT': .2})
    rows = [fill('open', 1), fill('close', 8, role='exit', pnl=10)]
    result = summarize_history(rows, [cycle], since_ms=5, end_ms=10, currency='USDT')
    assert result['metrics']['open_count']['value'] == 0
    assert result['metrics']['win_rate']['value'] == 100
    assert result['metrics']['profit_loss_ratio']['reason'] == 'no_loss_samples'
    result = summarize_history([rows[0],{**rows[1],'fees':[]}], [{**cycle, 'missing_fees': True}], since_ms=5, end_ms=10, currency='USDT')
    assert result['metrics']['win_rate']['reason'] == 'incomplete_fees'


def test_explicit_fill_direction_is_shared_and_never_inferred_from_pnl():
    from backend.utils.fill_classification import classify_fill
    assert classify_fill({'position_side': 'SHORT', 'side': 'sell'}, {}) == ('entry', 'SHORT')
    assert classify_fill({'position_side': 'LONG', 'side': 'sell'}, {}) == ('external_unknown_exit', 'LONG')
    assert classify_fill({'side': 'sell', 'realized_pnl': 40}, {}) == (None, None)


def test_missing_order_role_uses_owned_exchange_fill_evidence(pg, monkeypatch):
    from test_real_symbol_performance import seed_cycle
    monkeypatch.setattr('backend.utils.trade_evidence.configured_account_scope', lambda _: 'account')
    now = datetime.now(database.TZ_CN)
    end = int(now.timestamp()*1000)
    seed_cycle('account', 'task', 'BTC/USDT:USDT', end-8*86400000, end-1000, name='old', side='SHORT')
    with pg() as conn:
        conn.execute("DELETE FROM execution_order_links WHERE config_id='task'")
        conn.commit()
    data = task_performance('task', cfg={'mode':'REAL','symbol':'BTC/USDT'}, now=now)
    assert data['all_history']['open_count'] == 1
    assert data['all_history']['short_count'] == 1
    assert data['seven_days']['open_count'] == 0 and data['seven_days']['win_rate'] == 100


def test_cycle_metrics_are_after_fees_and_period_profit_includes_partial_exits():
    cycles=[dict(closed_at_ms=8,realized_pnl_before_fees=p,fees_by_currency={'USDT':f}) for p,f in [(10,1),(-4,1),(1,1)]]
    cycles.append(dict(closed_at_ms=9,realized_pnl_before_fees=20,missing_fees=True))
    rows=[fill('open',1),fill('partial',7,role='exit',pnl=3)]
    data=summarize_history(rows,cycles,since_ms=0,end_ms=10,currency='USDT')
    assert data['win_rate']==100 and data['profit_loss_ratio'] is None
    assert data['winning_cycles']==1 and data['losing_cycles']==1
    assert data['realized_pnl_after_fees']==pytest.approx(2.8)
    assert data['breakeven_cycles']==1 and data['unknown_cycles']==1


@pytest.mark.parametrize('fee,currency', [(None,'USDT'),(.01,'BTC'),(.1,None)])
def test_unknown_or_foreign_currency_fees_are_never_guessed(fee,currency):
    data=summarize_history([fill('x',1,pnl=3,fee=fee,currency=currency)],[],since_ms=0,end_ms=10,currency='USDT')
    assert data['realized_pnl_after_fees'] is None
    assert data['realized_pnl_before_fees']==3


def test_real_statistics_exclude_manual_other_task_and_other_account(pg,monkeypatch):
    from test_real_symbol_performance import seed_cycle
    monkeypatch.setattr('backend.utils.trade_evidence.configured_account_scope',lambda _: 'account')
    now=datetime.now(database.TZ_CN)
    end=int(now.timestamp()*1000)
    seed_cycle('account','task','BTC/USDT:USDT',end-10000,end-9000,name='own',pnl=3)
    seed_cycle('account','other','BTC/USDT:USDT',end-8000,end-7000,name='other',pnl=500)
    seed_cycle('foreign','task','BTC/USDT:USDT',end-6000,end-5000,name='foreign',pnl=500)
    data=task_performance('task',cfg={'mode':'REAL','symbol':'BTC/USDT'},now=now)
    assert data['all_history']['open_count']==1
    assert data['all_history']['realized_pnl_after_fees']==pytest.approx(2.8)
    assert data['all_history']['closed_cycles']==1
    assert 'own-entry' not in task_history_context('task',{'mode':'REAL','symbol':'BTC/USDT'})


def test_full_history_has_no_seven_day_cutoff(pg):
    now=datetime.now(database.TZ_CN)
    with pg() as conn:
        for days in [1,500]:
            conn.execute("INSERT INTO mock_orders(order_id,config_id,symbol,side,is_filled,amount,status,timestamp) VALUES(%s,'t','BTC/USDT','BUY',1,1,'OPEN',%s)",
                         (str(days),(now-timedelta(days=days)).isoformat()))
        conn.commit()
    data=task_performance('t',cfg={'mode':'STRATEGY','symbol':'BTC/USDT'},now=now)
    assert data['seven_days']['open_count']==1 and data['all_history']['open_count']==2


def test_reason_receipt_replay_and_recovery_keep_original_identity(pg):
    args=dict(amount=.1,price=100,reason='等待回踩确认后加仓。\n保留原文。')
    record_trade_call('t','call','open_position',args,'BTC/USDT')
    record_trade_call('t','call','open_position',args,'BTC/USDT',{'status':'unknown'})
    record_trade_call('t','call','open_position',{**args,'reason':'不要覆盖'},'BTC/USDT',{'status':'failed'})
    with pg() as conn:
        conn.execute("INSERT INTO trade_action_runs(config_id,operation_id,symbol,request_hash,status,result,created_at,updated_at) VALUES('t','call','BTC/USDT','h','submitted',%s,1,2)",
                     (json.dumps({'status':'submitted','order_id':'actual'}),))
        conn.commit()
    data=list_trade_calls('t')
    assert data['total']==1 and data['items'][0]['reason']==args['reason']
    assert data['items'][0]['operation_id']=='call' and data['items'][0]['result']['order_id']=='actual'


@pytest.mark.parametrize('status',['failed','rejected','not_executed','awaiting_approval'])
def test_non_submitted_reasons_are_durable_without_financial_operations(pg,status):
    record_trade_call('t',status,'cancel_order',{'order_id':'o','reason':'计划失效'},'BTC/USDT',{'status':status})
    assert list_trade_calls('t',status=status)['items'][0]['reason']=='计划失效'
    with pg() as conn:
        assert conn.execute('SELECT COUNT(*) FROM trade_action_runs').fetchone()[0]==0


def test_journal_keeps_original_reason_separate_from_failure():
    text=decision_journal([AIMessage(content='',tool_calls=[dict(id='x',name='open_position',args={'reason':'回踩确认'})]),
                           ToolMessage(tool_call_id='x',content='{"status":"failed","error":"余额不足"}')])
    assert '操作理由（原文）：回踩确认' in text and '余额不足' in text


def test_investment_jump_is_not_return_and_legacy_valuation_stays_unknown(pg):
    with pg() as conn:
        for day,cost in [('2026-11-01',100),('2026-11-02',1300)]:
            conn.execute("INSERT INTO dca_daily_snapshots(config_id,symbol,snapshot_date,total_invested) VALUES('t','BTC/USDT',%s,%s)",(day,cost))
        points=spot_history_points(conn,'t',['BTC/USDT'])
        assert [p['invested'] for p in points]==[100,1300]
        assert all(p['holding_return_pct'] is None for p in points)
        assert _equity_series_metadata(points)['return_pct'] is None
        conn.execute("UPDATE dca_daily_snapshots SET remaining_cost=1300,market_value=1430,cost_basis_revision=2,valuation_status='ready' WHERE snapshot_date='2026-11-02'")
        conn.execute("INSERT INTO spot_cost_basis_revisions(config_id,symbol,revision,quantity,avg_cost,order_baselines_json,updated_at) VALUES('t','BTC/USDT',2,1,1300,'{}','2026-11-02T02:00:00+00:00')")
        points=spot_history_points(conn,'t',['BTC/USDT'])
        assert points[-1]['holding_return_pct']==10 and points[-1]['calibrated']


def test_buy_sell_weighted_cost_known_fees_and_calibration(spot):
    spot.calibrate(1,100)
    spot.add_order('new',1,200)
    with spot.connect() as conn:
        conn.execute("UPDATE orders SET timestamp='2026-11-01 10:00:00' WHERE order_id='new'")
        conn.execute("INSERT INTO orders(order_id,config_id,symbol,trade_mode,side,status,filled_amount,filled_cost,avg_fill_price,timestamp) VALUES('sell','spot','BTC/USDT','SPOT_DCA','SELL','FILLED',0.5,90,180,'2026-11-01 11:00:00')")
        for tid,oid,side,qty,cost,fee,time in [('b','new','buy',1,200,1,'10:00:00'),('s','sell','sell',.5,90,.9,'11:00:00')]:
            conn.execute("INSERT INTO trade_history(trade_id,order_id,config_id,symbol,side,amount,cost,fee,fee_currency,timestamp) VALUES(%s,%s,'spot','BTC/USDT',%s,%s,%s,%s,'USDT',%s)",(tid,oid,side,qty,cost,fee,'2026-11-01 '+time))
        conn.commit()
    result=spot.stats()
    assert result['remaining_cost']==pytest.approx(225.75)
    assert result['total_qty']==1.5
    assert result['realized_pnl']==pytest.approx(13.85)
    assert result['return_pct']==pytest.approx((180-225.75)/225.75*100,abs=.005)
    assert spot.stats()['remaining_cost']==result['remaining_cost']


def test_base_currency_fee_reduces_quantity_and_clear_position_keeps_roi_unknown():
    result=value_spot_events([dict(side='buy',quantity=1,cost=100,fee=.01,fee_currency='BTC')],0,0,'BTC/USDT')
    assert result['quantity']==.99 and result['remaining_cost']==100
    result=value_spot_events([dict(side='sell',quantity=.99,cost=120,fee=1,fee_currency='USDT')],.99,100,'BTC/USDT')
    assert result['quantity']==0 and result['remaining_cost']==0 and result['realized_pnl']==19


def test_migration_backfills_only_original_requests_and_preserves_reason(pg):
    request={'contract_version':3,'actions':[{'action':'edit','order_id':'regular:o','remaining_amount':.5,'reason':'原单已成交0.2，剩余改为0.5'}]}
    with pg() as conn:
        conn.execute("INSERT INTO trade_action_runs(config_id,operation_id,symbol,request_hash,status,result,created_at,updated_at) VALUES('t','original','BTC/USDT','h','submitted','{\"status\":\"submitted\"}',1,2)")
        conn.execute("INSERT INTO trade_operation_requests(config_id,operation_id,request_json) VALUES('t','original',%s)",(json.dumps(request),))
        sql=Path('backend/storage/migrations/0014_workbench_history.sql').read_text(encoding='utf-8').split('-- Backfill only original stored requests.')[1]
        conn.execute('-- Backfill only original stored requests.'+sql)
        conn.commit()
    item=list_trade_calls('t')['items'][0]
    assert item['reason']==request['actions'][0]['reason'] and item['operation_id']=='original'


def test_covered_recent_summaries_are_not_injected_again(monkeypatch):
    from backend.agent import decision_context, context_projections
    from backend.agent.summary_prompts import MEMORY_BREVITY_POLICY
    memory=[{'source_id':'m','row':{'source_summary_ids':[1,2]},'projection':{'strategy_text':'记忆正文'}}]
    summaries=[{'source_id':str(i),'timestamp':'2026-11-06','projection':{'strategy_text':f'策略{i}：'+('等待收盘确认，不追单。'*18)}} for i in [3,2,1]]
    monkeypatch.setattr(context_projections,'stored_sources',lambda cid,scope,kind,**_: memory if kind=='short_memory' else summaries)
    monkeypatch.setattr('backend.utils.trade_evidence.configured_account_scope',lambda _: 'scope')
    recent=decision_context.format_recent_decisions('t')
    assert '策略3' in recent and '策略1' not in recent and '策略2' not in recent
    rendered=decision_context.render_decision_prompt('{short_memory_text}\n{recent_summaries_text}',
        decision_context.DecisionMemory('记忆正文',recent,task_history_text='唯一历史统计'))
    assert rendered.count('唯一历史统计')==1
    previous_recent = '\n'.join(row['projection']['strategy_text'] for row in summaries)
    assert len(previous_recent) - len(recent) > 350  # Actual prompt builder removes two covered sources.
    assert '300–500字' in MEMORY_BREVITY_POLICY and '约400字' in MEMORY_BREVITY_POLICY


def test_new_history_endpoints_require_authentication(pg,monkeypatch):
    from fastapi.testclient import TestClient
    from backend.app.main import app
    from backend.config import config
    monkeypatch.setattr(config,'get_config_by_id',lambda _: {'mode':'STRATEGY','symbol':'BTC/USDT'})
    client=TestClient(app)
    assert client.get('/api/stats/task-history/t').status_code==401
    assert client.get('/api/history/trade-calls?config_id=t').status_code==401


def test_repeated_calibration_keeps_realized_profit_and_does_not_add_a_trade(api, pg):
    from backend.app.services.spot_cost_basis_service import update_spot_cost_basis
    from backend.database.spot_cost_basis import read_spot_cost_basis
    from backend.utils.spot_valuation import spot_orders, spot_holding_valuation
    first = update_spot_cost_basis('spot', 'BTC/USDT', 1, 100)
    after = datetime.fromisoformat(first['updated_at']) + timedelta(seconds=1)
    with pg() as conn:
        for oid, side, qty, value, fee, when in [
            ('buy', 'BUY', 1, 200, 1, after.isoformat()),
            ('sell', 'SELL', .5, 90, .9, (after+timedelta(seconds=1)).isoformat()),
        ]:
            conn.execute("INSERT INTO orders(order_id,config_id,symbol,trade_mode,side,status,filled_amount,filled_cost,avg_fill_price,timestamp) VALUES(%s,'spot','BTC/USDT','SPOT_DCA',%s,'FILLED',%s,%s,%s,%s)", (oid,side,qty,value,value/qty,when))
            conn.execute("INSERT INTO trade_history(trade_id,order_id,config_id,symbol,side,amount,cost,fee,fee_currency,timestamp) VALUES(%s,%s,'spot','BTC/USDT',%s,%s,%s,%s,'USDT',%s)", (oid,oid,side.lower(),qty,value,fee,when))
        conn.commit()
    for _ in range(2):
        update_spot_cost_basis('spot', 'BTC/USDT', 1.5, 150.5)
        with pg() as conn:
            baseline = read_spot_cost_basis(conn,'spot','BTC/USDT')
            result = spot_holding_valuation(conn,'spot','BTC/USDT',spot_orders(conn,'spot','BTC/USDT'),baseline,0,0)
            assert result['remaining_cost'] == 225.75
            assert result['realized_pnl'] == pytest.approx(13.85)
            assert conn.execute('SELECT COUNT(*) FROM orders').fetchone()[0] == 2
    assert baseline['revision'] == 3
    with pg() as conn:
        assert conn.execute('SELECT COUNT(*) FROM spot_cost_basis_revisions').fetchone()[0] == 3


def test_spot_cycle_closes_with_base_fee_but_net_is_not_converted(pg, monkeypatch):
    from backend.utils.task_performance import _spot_history
    monkeypatch.setattr('backend.utils.spot_valuation.spot_orders', lambda *args: {})
    monkeypatch.setattr('backend.utils.spot_valuation.spot_fill_events', lambda *args: [
        dict(order_id='b',side='buy',quantity=1,cost=100,fee=.01,fee_currency='BTC',timestamp='2026-11-01'),
        dict(order_id='s',side='sell',quantity=.99,cost=120,fee=1,fee_currency='USDT',timestamp='2026-11-02'),
    ])
    fills, cycles, _, _ = _spot_history('t',['BTC/USDT'])
    metrics = summarize_history(fills,cycles,since_ms=0,end_ms=2_000_000_000_000,currency='USDT')
    assert metrics['closed_cycles'] == 1 and metrics['unknown_cycles'] == 1
    assert metrics['fees_by_currency'] == {'BTC':.01, 'USDT':1}
    assert metrics['realized_pnl_after_fees'] is None


def test_approval_updates_one_original_record_and_filters_keep_statuses_separate(pg):
    reason = '原始操作理由，等待条件确认。' * 100
    args = dict(reason=reason, order_id='o')
    record_trade_call('t','operation','cancel_order',args,'BTC/USDT',status='awaiting_approval',tool_call_id='model-call')
    record_trade_call('t','operation','cancel_order',args,'BTC/USDT',{'status':'rejected'})
    record_trade_call('t','operation','cancel_order',args,'BTC/USDT',status='awaiting_approval')
    record_trade_call('t','tail','edit_order',args,'BTC/USDT',{'status':'not_executed'})
    row = list_trade_calls('t',status='rejected')['items'][0]
    assert row['reason'] == reason and row['tool_call_id'] == 'model-call'
    assert list_trade_calls('t',status='unresolved')['total'] == 0
    assert list_trade_calls('t',action='edit')['items'][0]['status'] == 'not_executed'
    assert list_trade_calls('t',per_page=1,page=2)['total'] == 2


def test_full_history_counts_unknown_time_without_guessing_weekly_date():
    row = fill('historical',None)
    assert summarize_history([row],[],since_ms=0,end_ms=10,currency='USDT')['open_count'] == 1
    weekly = summarize_history([row],[],since_ms=1,end_ms=10,currency='USDT')
    assert weekly['open_count'] == 0 and weekly['missing_time_count'] == 1


def test_amendment_chain_includes_intermediate_orders_without_fills(pg,monkeypatch):
    from test_real_symbol_performance import seed_cycle
    from backend.utils.execution_ledger import register_order
    monkeypatch.setattr('backend.utils.trade_evidence.configured_account_scope',lambda _: 'account')
    now=datetime.now(database.TZ_CN)
    end=int(now.timestamp()*1000)
    seed_cycle('account','t','BTC/USDT:USDT',end-5000,end-1000,name='old')
    seed_cycle('account','t','BTC/USDT:USDT',end-4000,end-2000,name='new')
    register_order('account','BTC/USDT:USDT','middle','t','entry',side='LONG',replaces_order_id='old-entry')
    register_order('account','BTC/USDT:USDT','new-entry','t','entry',side='LONG',replaces_order_id='middle')
    result=task_performance('t',cfg={'mode':'REAL','symbol':'BTC/USDT'},now=now)
    assert result['all_history']['open_count']==1


def test_snapshot_upgrade_without_manual_baseline_is_not_a_calibration(pg):
    with pg() as conn:
        conn.execute("INSERT INTO dca_daily_snapshots(config_id,symbol,snapshot_date,total_invested) VALUES('t','BTC/USDT','2026-11-01',100)")
        conn.execute("INSERT INTO dca_daily_snapshots(config_id,symbol,snapshot_date,total_invested,remaining_cost,market_value,cost_basis_revision) VALUES('t','BTC/USDT','2026-11-02',100,100,110,0)")
        points=spot_history_points(conn,'t',['BTC/USDT'])
    assert not any(point['calibrated'] for point in points)


def test_history_exposes_full_canonical_receipt_after_compact_tool_result(pg):
    record_trade_call('t','op','cancel_order',{'order_id':'o','reason':'条件失效'},'BTC/USDT',{'status':'completed'})
    full = {'status':'completed','order_id':'o','exchange_receipt':{'status':'canceled','filled':.2}}
    with pg() as conn:
        conn.execute("INSERT INTO trade_action_runs(config_id,operation_id,symbol,request_hash,status,result,created_at,updated_at) VALUES('t','op','BTC/USDT','h','completed',%s,1,2)",(json.dumps(full),))
        conn.commit()
    assert list_trade_calls('t')['items'][0]['result'] == full
