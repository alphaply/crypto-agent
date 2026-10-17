import json
from datetime import datetime
from unittest.mock import patch

import pytest

import backend.database as database
from backend.database_schema import initialize_schema
from backend.utils.execution_ledger import ExecutionLedger, execution_context, register_order


class Exchange:
    id = 'binance'
    markets = {'ETH/USDT:USDT': {'symbol': 'ETH/USDT:USDT', 'contract': True, 'swap': True, 'linear': True}}
    apiKey = 'account-a'

    def __init__(self, trades):
        self.trades = trades
        self.fail = False

    def load_markets(self):
        pass

    def market(self, symbol):
        return dict(symbol='ETH/USDT:USDT', contract=True, swap=True, linear=True)

    def fetch_my_trades(self, symbol, since, limit, params):
        if self.fail:
            raise RuntimeError('network down')
        return [r for r in self.trades if since <= r['timestamp'] <= params['until']][:limit]


@pytest.fixture
def local_db(tmp_path, financial_pg):
    with patch.object(database, 'DB_NAME', str(tmp_path / 'ledger.db')):
        with database.get_db_conn() as conn:
            pass  # financial_pg installs the complete native migration schema.
        yield


def indexed_ledger(*args, **kwargs):
    """Accounting assertions run after the bounded migration has completed."""
    ledger = ExecutionLedger(*args, **kwargs)
    for _ in range(64):
        if ledger.index_state['complete']:
            return ledger
        ledger.index_saved_plans()
    pytest.fail('Ownership index did not finish the isolated accounting fixture')


def fill(i, **kwargs):
    return dict(id=str(i), order='entry', timestamp=100000 + i, price=100, amount=.1,
                side='buy', cost=10, **kwargs)


def test_pages_more_than_ten_fills_and_restart_overlap_are_idempotent(local_db):
    ex = Exchange([fill(i) for i in range(2100)])
    ledger = indexed_ledger(ex, 'cfg', 'ETH/USDT')
    assert ledger.sync(now_ms=200000, start_ms=100000)['complete']
    restarted = indexed_ledger(ex, 'cfg', 'ETH/USDT')
    assert restarted.sync(now_ms=250000, min_interval=0)['complete']
    with database.get_db_conn() as conn:
        assert conn.execute('SELECT COUNT(*) FROM execution_fills').fetchone()[0] == 2100


def test_account_key_isolated_missing_pnl_not_zero_and_late_ownership(local_db):
    ex = Exchange([fill(1)])
    a = indexed_ledger(ex, 'cfg', 'ETH/USDT')
    a.sync(now_ms=200000, start_ms=100000)
    ex.apiKey = 'account-b'
    b = indexed_ledger(ex, 'other', 'ETH/USDT')
    b.sync(now_ms=200000, start_ms=100000)
    register_order(a.scope, a.symbol, 'entry', 'cfg', 'entry')
    with database.get_db_conn() as conn:
        rows = conn.execute('SELECT config_id,payload FROM execution_fills ORDER BY config_id NULLS FIRST').fetchall()
    assert len(rows) == 2
    assert [r['config_id'] for r in rows] == [None, 'cfg']
    assert json.loads(rows[1]['payload'])['realized_pnl'] is None


def test_failed_sync_does_not_advance_checkpoint(local_db):
    ex = Exchange([fill(1)])
    ledger = indexed_ledger(ex, 'cfg', 'ETH/USDT')
    first = ledger.sync(now_ms=200000, start_ms=100000)
    ex.fail = True
    failed = ledger.sync(now_ms=300000, min_interval=0)
    assert not failed['complete'] and failed['through_ms'] == first['through_ms']
    ex.fail = False
    assert ledger.sync(now_ms=400000, min_interval=0)['complete']


def test_independent_algo_namespace_does_not_claim_regular_fill_on_restart(local_db):
    ex = Exchange([])
    ledger = indexed_ledger(ex, 'cfg', 'ETH/USDT')
    register_order(ledger.scope, ledger.symbol, '42', 'other', 'entry')
    plan = {'account_scope': ledger.scope, 'side': 'LONG', 'execution_mode': 'independent_exits',
            'exits': [{'id': '42', 'contract_version': 2, 'order_type': 'stop_limit', 'execution_order_id': '43'},
                      {'id': '44', 'exit_type': 'take_profit_limit'}]}
    with database.get_db_conn() as conn:
        conn.execute('INSERT INTO real_protection_plans(config_id,symbol,side,payload) VALUES(%s,%s,%s,%s)', ('cfg', ledger.symbol, 'LONG', json.dumps(plan)))
        conn.commit()
    indexed_ledger(ex, 'cfg', 'ETH/USDT')
    with database.get_db_conn() as conn:
        links = {row['order_id']: (row['config_id'], row['role']) for row in conn.execute('SELECT * FROM execution_order_links')}
    assert links['42'] == ('other', 'entry')
    assert links['algo:42'] == ('cfg', 'stop_loss')
    assert links['43'] == ('cfg', 'stop_loss')
    assert links['44'] == ('cfg', 'agent_exit')


def _save_ledger_plan(ledger, plan, owner='cfg', event=False):
    with database.get_db_conn() as conn:
        if event:
            conn.execute('INSERT INTO real_protection_events(timestamp,config_id,symbol,payload) VALUES(%s,%s,%s,%s)',
                         ('2026-01-01 00:00:00',owner,ledger.symbol,json.dumps(plan)))
        else:
            conn.execute('INSERT INTO real_protection_plans(config_id,symbol,side,payload) VALUES(%s,%s,%s,%s) ON CONFLICT(config_id,symbol,side) DO UPDATE SET payload=excluded.payload',
                         (owner,ledger.symbol,plan.get('side','LONG'),json.dumps(plan)))
        conn.commit()


def test_upgrade_retires_bare_algo_link_and_unproven_fill_owner_without_losing_fill(local_db):
    ex=Exchange([])
    ledger=indexed_ledger(ex,'cfg','ETH/USDT')
    register_order(ledger.scope,ledger.symbol,'42','cfg','stop_loss',refresh=False,
                   side='LONG',episode_id='legacy-episode',trigger_price=90)
    trade={**fill(9),'order':'42','side':'sell','info':{'positionSide':'LONG','realizedPnl':'-1'}}
    ledger.ingest([trade],refresh=False)
    with database.get_db_conn() as conn:
        before=dict(conn.execute('SELECT * FROM execution_fills').fetchone())
        # The old order log is also ambiguous and must not recreate the bare ID.
        conn.execute("INSERT INTO orders(order_id,config_id,symbol,trade_mode,side,event_type) VALUES('42','cfg',%s,'REAL','CLOSE_LONG','CLOSE_ORDER_CREATED')",(ledger.symbol,))
        conn.commit()
    plan=dict(account_scope=ledger.scope,side='LONG',episode_id='legacy-episode',execution_mode='independent_exits',
              exits=[dict(id='42',exit_type='stop_market',amount=1,filled=0,status='open',trigger_price=90)])
    _save_ledger_plan(ledger,plan)
    restarted=indexed_ledger(ex,'cfg','ETH/USDT')
    with database.get_db_conn() as conn:
        after=dict(conn.execute('SELECT * FROM execution_fills').fetchone())
        assert after=={**before,'config_id':None}
        assert not conn.execute("SELECT 1 FROM execution_order_links WHERE order_id='42'").fetchone()
        parent=conn.execute("SELECT * FROM execution_order_links WHERE order_id='algo:42'").fetchone()
        metadata=json.loads(parent['payload'])
        assert metadata['legacy_alias_retired']=='42'
        assert metadata['legacy_alias_metadata']['episode_id']=='legacy-episode'
    # Re-ingesting the same exchange fact and restarting cannot resurrect ownership.
    restarted.ingest([trade],refresh=False)
    indexed_ledger(ex,'cfg','ETH/USDT')
    with database.get_db_conn() as conn:
        assert conn.execute('SELECT COUNT(*) FROM execution_fills').fetchone()[0]==1
        assert conn.execute('SELECT config_id FROM execution_fills').fetchone()[0] is None


@pytest.mark.parametrize('proof',['entry','ordinary_exit','same_id_child','later_event_child','legacy_entry'])
def test_upgrade_preserves_proven_regular_identity_colliding_with_algo(local_db,proof):
    ex=Exchange([])
    ledger=indexed_ledger(ex,'cfg','ETH/USDT')
    register_order(ledger.scope,ledger.symbol,'42','cfg','stop_loss',refresh=False,side='LONG')
    ledger.ingest([{**fill(1),'order':'42'}],refresh=False)
    plan=dict(account_scope=ledger.scope,side='LONG',execution_mode='independent_exits',
              entries=[],exits=[dict(id='42',exit_type='stop_market',amount=1,filled=0,status='open')])
    if proof=='entry':
        plan['entries']=[dict(id='42',price=100)]
    elif proof=='ordinary_exit':
        plan['exits'].append(dict(id='42',exit_type='take_profit_limit',price=110))
    elif proof=='same_id_child':
        plan['exits'][0]['execution_order_id']='42'
    elif proof=='later_event_child':
        # The current snapshot may be stale; historical confirmed child evidence
        # must be considered before interpreting the earlier parent-only event.
        event={**plan,'exits':[{**plan['exits'][0],'execution_order_id':'42'}]}
        _save_ledger_plan(ledger,event,event=True)
    else:
        with database.get_db_conn() as conn:
            conn.execute("INSERT INTO orders(order_id,config_id,symbol,trade_mode,side,event_type) VALUES('42','cfg',%s,'REAL','BUY','ORDER_CREATED')",(ledger.symbol,))
            conn.commit()
    _save_ledger_plan(ledger,plan)
    indexed_ledger(ex,'cfg','ETH/USDT')
    with database.get_db_conn() as conn:
        links={row['order_id']:row['config_id'] for row in conn.execute('SELECT * FROM execution_order_links')}
        assert links['42']==links['algo:42']=='cfg'
        assert conn.execute('SELECT config_id FROM execution_fills').fetchone()[0]=='cfg'


def test_retire_old_parent_from_history_without_touching_other_account_or_owner(local_db):
    ex=Exchange([])
    ledger=indexed_ledger(ex,'cfg','ETH/USDT')
    register_order(ledger.scope,ledger.symbol,'42','other','stop_loss',refresh=False,side='LONG')
    register_order('different-account',ledger.symbol,'42','other','stop_loss',refresh=False,side='LONG')
    register_order(ledger.scope,ledger.symbol,'43','external','entry',refresh=False,side='SHORT')
    register_order(ledger.scope,ledger.symbol,'44','unscoped','stop_loss',refresh=False,side='LONG')
    ledger.ingest([{**fill(1),'order':'42'},{**fill(2),'order':'43'},{**fill(3),'order':'44'}],refresh=False)
    historic=dict(account_scope=ledger.scope,side='LONG',execution_mode='independent_exits',state='DONE',
                  exits=[dict(id='42',exit_type='stop_market',status='closed'),
                         dict(id='43',exit_type='stop_market',status='canceled')])
    _save_ledger_plan(ledger,historic,owner='other',event=True)
    _save_ledger_plan(ledger,dict(side='LONG',execution_mode='independent_exits',
                                exits=[dict(id='44',exit_type='stop_market')]),owner='unscoped',event=True)
    indexed_ledger(ex,'cfg','ETH/USDT')
    with database.get_db_conn() as conn:
        links={(row['account_scope'],row['order_id']):row['config_id'] for row in conn.execute('SELECT * FROM execution_order_links')}
        assert (ledger.scope,'42') not in links
        assert links[(ledger.scope,'algo:42')]=='other'
        assert links[('different-account','42')]=='other'
        assert links[(ledger.scope,'43')]=='external'
        assert links[(ledger.scope,'44')]=='unscoped'
        owners={row['order_id']:row['config_id'] for row in conn.execute('SELECT * FROM execution_fills')}
        assert owners=={'42':None,'43':'external','44':'unscoped'}


def test_later_verified_child_reclaims_preserved_unknown_execution(local_db):
    ex=Exchange([])
    ledger=indexed_ledger(ex,'cfg','ETH/USDT')
    register_order(ledger.scope,ledger.symbol,'42','cfg','stop_loss',refresh=False)
    ledger.ingest([{**fill(1),'order':'42'}],refresh=False)
    plan=dict(account_scope=ledger.scope,side='LONG',execution_mode='independent_exits',
              exits=[dict(id='42',exit_type='stop_market')])
    _save_ledger_plan(ledger,plan)
    indexed_ledger(ex,'cfg','ETH/USDT')
    with database.get_db_conn() as conn:
        assert conn.execute('SELECT config_id FROM execution_fills').fetchone()[0] is None
    plan['exits'][0]['execution_order_id']='42'
    _save_ledger_plan(ledger,plan)
    indexed_ledger(ex,'cfg','ETH/USDT')
    with database.get_db_conn() as conn:
        assert conn.execute('SELECT config_id FROM execution_fills').fetchone()[0]=='cfg'
        assert conn.execute('SELECT COUNT(*) FROM execution_fills').fetchone()[0]==1


def test_regular_evidence_from_other_config_replaces_old_parent_misattribution(local_db):
    ex=Exchange([])
    ledger=indexed_ledger(ex,'cfg','ETH/USDT')
    register_order(ledger.scope,ledger.symbol,'42','cfg','stop_loss',refresh=False)
    ledger.ingest([{**fill(1),'order':'42'}],refresh=False)
    parent=dict(account_scope=ledger.scope,side='LONG',execution_mode='independent_exits',
                exits=[dict(id='42',exit_type='stop_market')])
    regular=dict(account_scope=ledger.scope,side='SHORT',execution_mode='independent_exits',
                 entries=[dict(id='42',price=100)])
    _save_ledger_plan(ledger,parent,owner='cfg')
    _save_ledger_plan(ledger,regular,owner='other')
    indexed_ledger(ex,'cfg','ETH/USDT')
    with database.get_db_conn() as conn:
        links={row['order_id']:row['config_id'] for row in conn.execute('SELECT * FROM execution_order_links')}
        assert links=={'42':'other','algo:42':'cfg'}
        assert conn.execute('SELECT config_id FROM execution_fills').fetchone()[0]=='other'


def test_timestamp_overflow_is_incomplete(local_db):
    ex = Exchange([{**fill(i), 'timestamp': 100000} for i in range(1001)])
    result = indexed_ledger(ex, 'cfg', 'ETH/USDT').sync(now_ms=100000, start_ms=100000)
    assert not result['complete'] and 'millisecond' in result['error']
    assert 'through_ms' not in result


def test_future_range_cannot_be_marked_complete(local_db):
    ledger = indexed_ledger(Exchange([]), 'cfg', 'ETH/USDT')
    with patch('backend.utils.execution_ledger.time.time', return_value=200):
        result = ledger.sync(now_ms=300000, start_ms=100000)
    assert result['through_ms'] == 200000


def test_long_outage_completes_all_pages_in_one_attempt(local_db):
    ledger = indexed_ledger(Exchange([]), 'cfg', 'ETH/USDT')
    day = 86400000
    result = ledger.sync(now_ms=day * 3, start_ms=0)
    assert result['calls'] > 1
    assert result['complete'] and result['covered_intervals'] == [[0, day * 3]]


def test_existing_one_day_checkpoint_is_upgraded_to_seven_days(local_db):
    day = 86400000
    ledger = indexed_ledger(Exchange([]), 'cfg', 'ETH/USDT')
    ledger._status({
        'attempted_at': 8 * day,
        'from_ms': 7 * day,
        'through_ms': 8 * day,
        'covered_intervals': [[7 * day, 8 * day]],
        'complete': True,
    })

    result = ledger.sync(now_ms=8 * day, min_interval=0)

    assert result['complete']
    assert result['covered_intervals'] == [[day, 8 * day]]


def test_ownership_reindex_preserves_execution_evidence(local_db):
    ledger = indexed_ledger(Exchange([]), 'cfg', 'ETH/USDT')
    register_order(ledger.scope, ledger.symbol, 'entry', 'cfg', 'entry', side='LONG', planned_entry=100)
    register_order(ledger.scope, ledger.symbol, 'entry', 'cfg', 'entry', side='LONG')
    with database.get_db_conn() as conn:
        value = json.loads(conn.execute('SELECT payload FROM execution_order_links').fetchone()[0])
    assert value['planned_entry'] == 100


def test_spot_cannot_enter_futures_ledger(local_db):
    ex = Exchange([])
    ex.market = lambda symbol: dict(symbol=symbol, spot=True)
    with pytest.raises(ValueError, match='perpetual'):
        indexed_ledger(ex, 'spot', 'ETH/USDT')


def test_context_all_totals_and_details_preserve_owned_fills(local_db):
    ex = Exchange([fill(i, fee={'cost': .01, 'currency': 'USDT'}, info={'realizedPnl': '1'}) for i in range(30)])
    ledger = indexed_ledger(ex, 'cfg', 'ETH/USDT')
    register_order(ledger.scope, ledger.symbol, 'entry', 'cfg', 'stop_loss', trigger_price=100)
    ledger.sync(now_ms=200000, start_ms=100000)
    text = execution_context('cfg', now=datetime.fromtimestamp(200), scope=ledger.scope)
    data = json.loads(text[text.index('{'):])
    assert data['fill_count'] == 30 and len(data['recent_fills']) == 30
    assert data['omitted_details'] == 0
    assert data['known_realized_pnl_before_fees'] == 30
    assert data['fees_by_currency']['USDT'] == pytest.approx(.3)
    assert data['recent_fills'][0]['role'] == 'stop_loss'


def test_disjoint_backfill_does_not_claim_gap_covered(local_db):
    ledger = indexed_ledger(Exchange([]), 'cfg', 'ETH/USDT')
    ledger.sync(now_ms=200000, start_ms=100000)
    status = ledger.sync(now_ms=50000, start_ms=0, min_interval=0)
    assert status['covered_intervals'] == [[0, 50000], [100000, 200000]]
    assert status['from_ms'] == 100000


def test_futures_daily_scope_ownership_and_missing_values(local_db):
    from backend.utils.trade_review import daily_exchange_evidence
    start = int(database.TZ_CN.localize(datetime(2026, 9, 1)).timestamp() * 1000)
    ex = Exchange([{**fill(1), 'timestamp': start + 1000}, {**fill(2), 'timestamp': start - 1}])
    ledger = indexed_ledger(ex, 'cfg', 'ETH/USDT')
    register_order(ledger.scope, ledger.symbol, 'entry', 'cfg', 'entry', side='LONG')
    with patch('backend.utils.market_data.MarketTool', return_value=type('MT', (), {'exchange': ex})()):
        text = daily_exchange_evidence({'mode': 'REAL', 'config_id': 'cfg', 'symbol': 'ETH/USDT'}, '2026-09-01')
    data = json.loads(text[text.index('{'):])
    assert data['window_complete']
    assert data['fill_count'] == 1
    assert data['recent_fills'][0]['id'] == '1'
    assert data['recent_fills'][0]['realized_pnl'] is None
    assert not data['sync'][0]['income_sync']['complete']  # Fake has no income API; not invented as zero.


def test_partial_fills_reconstruct_one_cycle_without_counting_fills_as_trades(local_db):
    from backend.utils.execution_ledger import position_cycles
    ex = Exchange([])
    ledger = indexed_ledger(ex, 'cfg', 'ETH/USDT')
    register_order(ledger.scope, ledger.symbol, 'entry', 'cfg', 'entry', side='LONG')
    register_order(ledger.scope, ledger.symbol, 'close', 'cfg', 'stop_loss', side='LONG')
    ledger.ingest([fill(1), fill(2), {**fill(3), 'order': 'close', 'side': 'sell', 'amount': .2, 'price': 90}])
    result = position_cycles('cfg', 0, 200000, ledger.scope)
    assert result['completed_count'] == 1
    assert result['completed'][0]['entry_vwap'] == 100
    assert result['completed'][0]['exit_vwap'] == 90
    assert result['completed'][0]['missing_pnl']


@pytest.mark.parametrize('exit_role', ['take_profit', 'stop_loss', 'agent_exit'])
@pytest.mark.parametrize('add', [False, True])
def test_cycle_scenarios_with_partial_close_and_add(local_db, exit_role, add):
    from backend.utils.execution_ledger import position_cycles
    ledger = indexed_ledger(Exchange([]), 'cfg', 'ETH/USDT')
    for oid, role in [('entry', 'entry'), ('add', 'entry'), ('reduce', 'agent_exit'), ('close', exit_role)]:
        register_order(ledger.scope, ledger.symbol, oid, 'cfg', role, side='LONG')
    trades = [fill(1), {**fill(2), 'order': 'reduce', 'side': 'sell', 'amount': .04}]
    if add:
        trades.append({**fill(3), 'order': 'add'})
    trades.append({**fill(4), 'order': 'close', 'side': 'sell', 'amount': .16 if add else .06})
    ledger.ingest(trades)
    cycle = position_cycles('cfg', 0, 200000)['completed'][0]
    assert cycle['add_count'] == int(add)
    assert cycle['exit_count'] == 2
    assert cycle['final_exit_reason'] == exit_role
    assert cycle['remaining_base'] == 0


def test_unknown_close_ends_cycle_without_claiming_tp_or_poisoning_next(local_db):
    from backend.utils.execution_ledger import position_cycles
    ledger = indexed_ledger(Exchange([]), 'cfg', 'ETH/USDT')
    register_order(ledger.scope, ledger.symbol, 'entry', 'cfg', 'entry', side='LONG', episode_id='same-plan')
    register_order(ledger.scope, ledger.symbol, 'close', 'cfg', 'agent_exit', side='LONG')
    ledger.ingest([fill(1), {**fill(2), 'order': 'external', 'side': 'sell', 'info': {'positionSide': 'LONG'}},
                   fill(3), {**fill(4), 'order': 'close', 'side': 'sell'}])
    result = position_cycles('cfg', 0, 200000)
    assert result['completed_count'] == 2
    assert result['completed'][0]['exit_reasons'] == ['external_unknown_exit']
    assert len({c['episode_id'] for c in result['completed']}) == 2
    assert result['open_cycles'] == []


def test_mixed_unowned_entry_is_not_attributed_to_agent(local_db):
    from backend.utils.execution_ledger import position_cycles
    ledger = indexed_ledger(Exchange([]), 'cfg', 'ETH/USDT')
    register_order(ledger.scope, ledger.symbol, 'entry', 'cfg', 'entry', side='LONG')
    register_order(ledger.scope, ledger.symbol, 'close', 'cfg', 'take_profit', side='LONG')
    ledger.ingest([fill(1), {**fill(2), 'order': 'manual', 'info': {'positionSide': 'LONG'}},
                   {**fill(3), 'order': 'close', 'side': 'sell', 'amount': .2}])
    result = position_cycles('cfg', 0, 200000)
    assert result['completed_count'] == 0
    assert result['excluded_cycle_count'] == 1


def test_external_only_ingest_rebuilds_existing_owned_cycle(local_db):
    from backend.utils.execution_ledger import position_cycles
    ledger = indexed_ledger(Exchange([]), 'cfg', 'ETH/USDT')
    register_order(ledger.scope, ledger.symbol, 'entry', 'cfg', 'entry', side='LONG')
    ledger.ingest([fill(1)])
    ledger.ingest([{**fill(2), 'order': 'manual', 'side': 'sell', 'info': {'positionSide': 'LONG'}}])
    from backend.utils.execution_history_work import drain_history
    drain_history('cfg',ledger.scope,ledger.symbol)
    with database.get_db_conn() as conn:
        assert conn.execute('SELECT COUNT(*) FROM execution_position_history').fetchone()[0] == 1


def test_completed_cycle_is_materialized_with_prices_protection_and_pnl(local_db):
    ex = Exchange([])
    ledger = indexed_ledger(ex, 'cfg', 'ETH/USDT')
    register_order(
        ledger.scope,
        ledger.symbol,
        'entry',
        'cfg',
        'entry',
        side='LONG',
        episode_id='episode-1',
        take_profit=110,
        stop_loss=95,
    )
    register_order(
        ledger.scope,
        ledger.symbol,
        'close',
        'cfg',
        'take_profit',
        side='LONG',
        episode_id='episode-1',
        trigger_price=110,
    )
    entry = fill(1, fee={'cost': .01, 'currency': 'USDT'}, info={'realizedPnl': '0'})
    close = {
        **fill(2, fee={'cost': .011, 'currency': 'USDT'}, info={'realizedPnl': '1'}),
        'order': 'close',
        'side': 'sell',
        'price': 110,
    }

    ledger.ingest([entry, close])
    from backend.utils.execution_history_work import drain_history
    drain_history('cfg',ledger.scope,ledger.symbol)

    with database.get_db_conn() as conn:
        row = conn.execute('SELECT * FROM execution_position_history').fetchone()
    assert json.loads(row['payload'])['plan_episode_id'] == 'episode-1'
    assert len(row['position_id']) == 64  # Identity follows first fill, not a reusable plan.
    assert row['entry_price'] == 100
    assert row['close_price'] == 110
    assert row['amount'] == pytest.approx(.1)
    assert row['take_profit'] == 110
    assert row['stop_loss'] == 95
    assert row['realized_pnl'] == 1
    assert json.loads(row['fees_json'])['USDT'] == pytest.approx(.021)
    assert row['exit_reason'] == 'take_profit'


def test_stream_missing_fields_do_not_erase_rest_pnl(local_db):
    ledger = indexed_ledger(Exchange([]), 'cfg', 'ETH/USDT')
    ledger.ingest([fill(1, info={'realizedPnl': '3'})])
    ledger.ingest([fill(1)])
    with database.get_db_conn() as conn:
        value = json.loads(conn.execute('SELECT payload FROM execution_fills').fetchone()[0])
    assert value['realized_pnl'] == 3


@pytest.mark.parametrize('currency,missing,expected', [('USDT', False, -7.82), ('BNB', False, None), ('USDT', True, None)])
def test_history_net_pnl_deducts_both_fees_once(local_db, currency, missing, expected):
    from datetime import datetime
    ledger = indexed_ledger(Exchange([]), 'cfg', 'ETH/USDT')
    register_order(ledger.scope, ledger.symbol, 'entry', 'cfg', 'entry', side='LONG')
    register_order(ledger.scope, ledger.symbol, 'close', 'cfg', 'stop_loss', side='LONG')
    now = datetime.now(database.TZ_CN)
    timestamp = int(now.timestamp() * 1000) - 10000
    trades = [
        {**fill(1), 'timestamp': timestamp, 'realizedPnl': 0, 'fee': None if missing else {'currency': currency, 'cost': .3}},
        {**fill(2), 'timestamp': timestamp + 1000, 'order': 'close', 'side': 'sell', 'realizedPnl': -7.124, 'fee': {'currency': currency, 'cost': .396}},
    ]
    ledger.ingest(trades)
    ledger.ingest(trades)
    from backend.utils.execution_history_work import drain_history
    drain_history('cfg',ledger.scope,ledger.symbol)
    rows = database.get_closed_positions_7d('cfg', 'ETH/USDT', mode='REAL')
    assert len(rows) == 1
    assert rows[0]['realized_pnl'] == -7.124
    if expected is None:
        assert rows[0]['net_realized_pnl'] is None
    else:
        assert rows[0]['net_realized_pnl'] == pytest.approx(expected)
