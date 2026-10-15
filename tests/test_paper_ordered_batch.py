import json
import hashlib
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest

from test_mcp_paper import create, ledger
from backend.mcp import paper, paper_batch, paper_orders, store


def _paper_process_worker(db_path, entered, release, output):
    """Spawn-safe offline worker: the shared volume is the only coordination."""
    from backend import database
    database.DB_NAME = db_path
    def quote(symbol, market_type='swap', *args, **kwargs):
        entered.set()
        if not release.wait(10):
            raise ValueError('Fixture release timed out')
        return {'symbol': symbol, 'market_type': market_type, 'price': 100, 'bid': 100, 'ask': 100,
                'exchange': 'binance', 'observed_at': time.time()}
    paper.market = quote
    caller = {'id': 'key:paper', 'key_type': 'paper', 'paper_namespace': 'qqbot', 'profile_ids': [], 'scopes': ['read', 'trade']}
    output.put(batch(caller, [opening()]))


def opening(market_type='swap', **kwargs):
    return {'action': 'open', 'market_type': market_type, 'symbol': 'BTC/USDT' if market_type == 'spot' else 'BTC/USDT:USDT',
            'side': 'LONG', 'order_type': 'market', 'amount': 1, 'reason': 'requested entry', **kwargs}


def batch(caller, actions, operation_id='paper-parent-0001', **kwargs):
    return paper_batch.execute(caller, '123456', {'contract_version': 3, 'actions': actions, **kwargs}, operation_id)


def test_paper_parent_ids_replay_and_changed_intent(ledger):
    caller, _, quotes = ledger
    create(caller)
    first = batch(caller, [opening()], operation_id='p' * 160)
    assert first['status'] == 'completed'
    child = first['results'][0]['operation_id']
    assert len(child) <= 160 and child != first['operation_id']
    before = len(quotes)
    assert batch({**caller, 'id': 'rotated-key'}, [opening()], operation_id='p' * 160) == first
    assert len(quotes) == before
    with pytest.raises(ValueError, match='different paper batch'):
        batch(caller, [opening(amount=2)], operation_id='p' * 160)
    assert paper.get_account(caller, '123456')['positions'][0]['amount'] == 1


def test_paper_cancel_releases_reservation_before_next_action(ledger):
    caller, _, _ = ledger
    create(caller)
    queued = paper_orders.place(caller, '123456', 'spot', 'BTC', 'buy', 'limit', 'queue-big-buy', amount=9, price=99)['order']
    result = batch(caller, [{'action': 'cancel', 'order_id': queued['order_id'], 'reason': 'replace'}, opening('spot', amount=9)])
    assert [item['status'] for item in result['results']] == ['completed', 'completed']
    assert paper.get_account(caller, '123456')['reserved_cash'] == 0


def test_paper_preflight_invalid_later_action_has_no_effect_and_is_durable(ledger):
    caller, _, _ = ledger
    create(caller)
    result = batch(caller, [opening(), opening(amount=2, margin_amount=10)])
    assert result['status'] == 'failed'
    assert not any(item['attempted'] for item in result['results'])
    assert paper.get_account(caller, '123456')['positions'] == []
    assert paper_batch.operation(caller, '123456', result['operation_id']) == result


def test_paper_limit_acceptance_is_not_fill_or_dependency_completion(ledger):
    caller, _, _ = ledger
    create(caller)
    entry = opening(order_type='limit', price=90)
    close = {'action': 'close', 'market_type': 'swap', 'symbol': entry['symbol'], 'side': 'LONG', 'order_type': 'market',
             'close_all': True, 'reason': 'exit', 'depends_on': [0]}
    result = batch(caller, [entry, close], execution_policy='independent')
    assert result['results'][0]['status'] == 'submitted'
    assert result['results'][0]['order_status'] == 'open'
    assert result['results'][1]['status'] == 'not_executed'
    assert result['results'][1]['blocked_by'] == [0]
    assert paper.get_account(caller, '123456')['positions'] == []


def test_paper_crash_after_child_commit_never_repeats_or_runs_tail(ledger, monkeypatch):
    caller, _, _ = ledger
    create(caller)
    real = paper_orders.execute_market
    def interrupt(*args, **kwargs):
        real(*args, **kwargs)
        raise SystemExit('process ended after commit')
    monkeypatch.setattr(paper_orders, 'execute_market', interrupt)
    with pytest.raises(SystemExit):
        batch(caller, [opening(), opening(amount=2)])
    monkeypatch.setattr(paper_orders, 'execute_market', real)
    recovered = batch(caller, [opening(), opening(amount=2)])
    assert [item['status'] for item in recovered['results']] == ['completed', 'not_executed']
    assert paper.get_account(caller, '123456')['positions'][0]['amount'] == 1


def test_paper_crash_after_parent_registration_never_starts_tail(ledger, monkeypatch):
    from backend.agent import unified_trading
    caller, _, _ = ledger
    create(caller)
    real = unified_trading.validate_for_config
    def interrupt(*args, **kwargs):
        raise SystemExit('worker ended before validation')
    monkeypatch.setattr(unified_trading, 'validate_for_config', interrupt)
    with pytest.raises(SystemExit):
        batch(caller, [opening()])
    monkeypatch.setattr(unified_trading, 'validate_for_config', real)
    recovered = batch(caller, [opening()])
    assert recovered['results'][0]['status'] == 'not_executed'
    assert paper.get_account(caller, '123456')['positions'] == []


def test_paper_same_parent_concurrent_retries_have_one_fill(ledger):
    caller, _, _ = ledger
    create(caller)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: batch(caller, [opening()]), range(8)))
    recovered = paper_batch.operation(caller, '123456', 'paper-parent-0001')
    assert recovered['status'] == 'completed'
    assert paper.get_account(caller, '123456')['positions'][0]['amount'] == 1


def test_paper_quote_network_does_not_hold_write_transaction(ledger, monkeypatch):
    caller, _, _ = ledger
    create(caller)
    real = paper.market
    def quote(*args, **kwargs):
        with store.connection() as conn:
            from backend.storage import lock_scope
            lock_scope(conn, 'paper-account', caller['paper_namespace'], '123456')
            conn.rollback()
        return real(*args, **kwargs)
    monkeypatch.setattr(paper, 'market', quote)
    assert batch(caller, [opening()])['status'] == 'completed'


def test_paper_batch_namespace_and_user_order_isolation(ledger):
    caller, _, _ = ledger
    create(caller)
    create(caller, 'another')
    queued = paper_orders.place(caller, 'another', 'spot', 'BTC', 'buy', 'limit', 'foreign-order', amount=1, price=90)['order']
    result = batch(caller, [{'action': 'cancel', 'order_id': queued['order_id'], 'reason': 'cancel'}])
    assert result['status'] == 'failed'
    assert paper_orders.get_orders(caller, 'another')['orders'][0]['state'] == 'open'
    assert paper_batch.operation({**caller, 'paper_namespace': 'other'}, '123456', 'paper-parent-0001') is None


def test_paper_protection_and_cycle_reopen_remain_independent(ledger):
    caller, _, _ = ledger
    create(caller)
    close = {'action': 'close', 'market_type': 'swap', 'symbol': 'BTC/USDT:USDT', 'side': 'LONG',
             'order_type': 'market', 'close_all': True, 'reason': 'close old cycle'}
    protection = {'action': 'protect', 'market_type': 'swap', 'symbol': close['symbol'], 'side': 'LONG',
                  'stop_loss': 90, 'reason': 'single stop'}
    result = batch(caller, [opening(), protection, close, opening(amount=2)])
    assert result['status'] == 'submitted'
    assert result['results'][1]['status'] == 'submitted'
    assert result['results'][1]['order_status'] == 'waiting_trigger'
    assert paper.get_account(caller, '123456')['positions'][0]['amount'] == 2
    assert paper_orders.get_orders(caller, '123456')['orders'] == []


def test_legacy_paper_fingerprints_and_partial_cancel_receipts(ledger):
    caller, _, quotes = ledger
    create(caller)
    fixture = json.loads((Path(__file__).parent / 'fixtures/paper_operation_compatibility.json').read_text())
    for item in fixture['operations']:
        request = item['request']
        assert store.digest(json.dumps(request, sort_keys=True, separators=(',', ':'), allow_nan=False)) == item['fingerprint']
        if item['name'] == 'cancel':
            paper_orders.place(caller, '123456', 'spot', 'BTC', 'buy', 'limit', 'prepare-legacy-limit',
                               amount=1, price=90, order_id=request['order_id'])
            result = paper_orders.cancel(caller, '123456', request['order_id'], item['operation_id'])
            before = len(quotes)
            assert paper_orders.cancel(caller, '123456', request['order_id'], item['operation_id']) == result
            assert result['order']['state'] == item['expected']['order_status']
        else:
            result = paper.trade(caller, '123456', 'spot', request['symbol'], request['action'],
                                 float(request['amount']), item['operation_id'])
            before = len(quotes)
            assert paper.trade(caller, '123456', 'spot', request['symbol'], request['action'], float(request['amount']), item['operation_id']) == result
            assert result['account']['cash'] == item['expected']['cash']
            assert result['account']['spot'][0]['amount'] == item['expected']['spot_amount']
        assert len(quotes) == before and result['status'] == item['expected']['status']
        with store.connection() as conn:
            saved = conn.execute('SELECT fingerprint FROM paper_operations WHERE operation_id=%s', (item['operation_id'],)).fetchone()
        assert saved['fingerprint'] == item['fingerprint']


def test_paper_expired_budget_preserves_committed_prefix_and_final_query(ledger, monkeypatch):
    from backend.mcp.resilience import Budget, current_budget
    caller, _, _ = ledger
    create(caller)
    deadline = Budget(time.monotonic() + 40, 'budget-fixture', 'execute')
    real = paper_orders.execute_market
    def commit_then_expire(*args, **kwargs):
        result = real(*args, **kwargs)
        deadline.deadline = time.monotonic() - 1
        return result
    monkeypatch.setattr(paper_orders, 'execute_market', commit_then_expire)
    token = current_budget.set(deadline)
    try:
        result = batch(caller, [opening(), opening(amount=2)])
    finally:
        current_budget.reset(token)
    assert [row['status'] for row in result['results']] == ['completed', 'not_executed']
    assert paper_batch.operation(caller, '123456', result['operation_id'])['results'][0]['status'] == 'completed'


def test_paper_failed_quote_and_invalid_fractional_leverage_are_not_unknown(ledger, monkeypatch):
    caller, _, _ = ledger
    create(caller)
    invalid = batch(caller, [opening(), opening(leverage=1.5)], operation_id='invalid-leverage-parent')
    assert invalid['status'] == 'failed' and not any(row['attempted'] for row in invalid['results'])
    def offline(*args, **kwargs):
        raise ValueError('Public quote is stale')
    monkeypatch.setattr(paper, 'market', offline)
    result = batch(caller, [opening()], operation_id='failed-quote-parent')
    assert result['status'] == 'failed'
    assert result['results'][0]['submission'] == 'not_submitted'
    assert paper_batch.operation(caller, '123456', result['operation_id']) == result


def test_paper_twenty_item_model_projection_has_one_summary(ledger):
    from backend.agent.unified_trading import compact_trade_receipt
    caller, _, _ = ledger
    create(caller)
    result = batch(caller, [opening(amount=.01) for _ in range(20)], include=['account'])
    projected = compact_trade_receipt(result)
    assert len(projected['results']) == 20 and 'account_summary' in projected
    assert all('account' not in row and 'request' not in row for row in projected['results'])


def test_paper_storage_failure_before_intent_has_no_financial_effect(ledger, monkeypatch):
    from contextlib import contextmanager
    caller, _, _ = ledger
    create(caller)
    real = store.connection
    @contextmanager
    def unavailable(*args, **kwargs):
        raise ValueError('Storage unavailable')
        yield
    with monkeypatch.context() as patch:
        patch.setattr(store, 'connection', unavailable)
        with pytest.raises(ValueError, match='Storage unavailable'):
            batch(caller, [opening()])
    assert paper.get_account(caller, '123456')['positions'] == []


def test_paper_query_refreshes_attempted_limit_fill_but_never_tail(ledger):
    caller, ticker, _ = ledger
    create(caller)
    result = batch(caller, [opening(order_type='limit', price=90)])
    assert result['results'][0]['status'] == 'submitted'
    with store.connection() as conn:
        conn.execute('BEGIN')
        account = paper._get(conn, 'qqbot', '123456')
        paper_orders.match(conn, account, 'swap', 'BTC/USDT:USDT', {**ticker, 'ask': 85, 'bid': 85, 'price': 85,
                                        'symbol': 'BTC/USDT:USDT', 'market_type': 'swap', 'observed_at': time.time()}, paper.settings())
        paper._save(conn, account)
        conn.commit()
    refreshed = paper_batch.operation(caller, '123456', result['operation_id'])
    assert refreshed['results'][0]['order_status'] == 'filled'
    assert refreshed['results'][0]['filled_amount'] == 1


def test_reserved_child_identity_cannot_be_used_as_an_unrelated_legacy_request(ledger, monkeypatch):
    from backend.agent import unified_trading
    caller, _, _ = ledger
    create(caller)
    def interrupted(*args, **kwargs):
        raise SystemExit('before first action')
    monkeypatch.setattr(unified_trading, 'validate_for_config', interrupted)
    with pytest.raises(SystemExit):
        batch(caller, [opening()])
    saved = paper_batch.operation(caller, '123456', 'paper-parent-0001')
    child_id = saved['results'][0]['operation_id']
    with pytest.raises(ValueError, match='reserved paper batch child'):
        paper.trade(caller, '123456', 'spot', 'BTC/USDT', 'buy', 1, child_id)
    with pytest.raises(ValueError, match='paper batch parent'):
        paper.trade(caller, '123456', 'spot', 'BTC/USDT', 'buy', 1, saved['operation_id'])


def test_commit_evidence_failure_is_unknown_then_original_child_recovers(ledger, monkeypatch):
    from contextlib import contextmanager
    caller, _, _ = ledger
    create(caller)
    real_write, real_connection = paper_orders.execute_market, store.connection
    unreadable = [False]
    def lost_ack(*args, **kwargs):
        real_write(*args, **kwargs)
        unreadable[0] = True
        raise RuntimeError('Commit acknowledgement lost')
    @contextmanager
    def transient_connection(*args, **kwargs):
        if kwargs.get('recovery') and unreadable[0]:
            unreadable[0] = False
            raise ValueError('Cannot verify commit evidence')
        with real_connection(*args, **kwargs) as conn:
            yield conn
    monkeypatch.setattr(paper_orders, 'execute_market', lost_ack)
    monkeypatch.setattr(store, 'connection', transient_connection)
    result = batch(caller, [opening(), opening(amount=2)])
    assert [row['status'] for row in result['results']] == ['unknown', 'not_executed']
    assert result['results'][0]['error_code'] == 'commit_evidence_unavailable'
    recovered = paper_batch.operation(caller, '123456', result['operation_id'])
    assert recovered['results'][0]['status'] == 'completed'
    assert recovered['results'][1]['status'] == 'not_executed'
    assert paper.get_account(caller, '123456')['positions'][0]['amount'] == 1


def test_active_worker_queries_are_pending_and_do_not_duplicate_submission(ledger, monkeypatch):
    import threading
    caller, _, quotes = ledger
    create(caller)
    real = paper.market
    entered, release = threading.Event(), threading.Event()
    def blocked_quote(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return real(*args, **kwargs)
    monkeypatch.setattr(paper, 'market', blocked_quote)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(batch, caller, [opening()])
        assert entered.wait(5)
        queried = paper_batch.operation(caller, '123456', 'paper-parent-0001')
        assert queried['status'] == 'pending'
        assert batch(caller, [opening()])['status'] == 'pending'
        release.set()
        assert future.result(timeout=5)['status'] == 'completed'
    assert len(quotes) == 1


def test_cross_process_retry_tracks_physical_worker_and_has_one_fill(ledger):
    import multiprocessing
    from backend import database
    caller, _, _ = ledger
    create(caller)
    context = multiprocessing.get_context('spawn')
    entered, release = context.Event(), context.Event()
    output = context.Queue()
    process = context.Process(target=_paper_process_worker, args=(database.DB_NAME, entered, release, output))
    process.start()
    try:
        assert entered.wait(10)
        assert paper_batch.operation(caller, '123456', 'paper-parent-0001')['status'] == 'pending'
        assert batch(caller, [opening()])['status'] == 'pending'
        release.set()
        assert output.get(timeout=10)['status'] == 'completed'
        process.join(10)
        assert process.exitcode == 0
        assert paper.get_account(caller, '123456')['positions'][0]['amount'] == 1
    finally:
        release.set()
        if process.is_alive():
            process.terminate()
            process.join(5)
        output.close()


def _venue_quote_fixture(monkeypatch):
    quotes = []
    def quote(symbol, market_type='swap', *args, exchange_name=None, **kwargs):
        venue = exchange_name or 'binance'
        quotes.append((symbol, market_type, venue))
        return {'symbol': symbol, 'market_type': market_type, 'price': 100, 'bid': 100,
                'ask': 100, 'exchange': venue, 'observed_at': time.time()}
    monkeypatch.setattr(paper, 'market', quote)
    return quotes


def _open_venue(caller, market, venue):
    symbol = 'BTC/USDT' if market == 'spot' else 'BTC/USDT:USDT'
    return paper_orders.execute_market(caller, '123456', market, symbol,
        'buy' if market == 'spot' else 'open_long', f'venue-seed-{market}-{venue}',
        amount=1, exchange_name=venue)


def _seed_foreign(caller, market):
    from test_paper_binance_scope import raw_account
    account = raw_account(caller)
    symbol = 'BTC/USDT' if market == 'spot' else 'BTC/USDT:USDT'
    key = 'legacy-bitget'
    item = {'symbol': symbol, 'exchange': 'bitget', 'cycle_id': 'legacy-bitget-cycle',
            'amount': 1, 'entry_price': 100, 'mark_price': 100, 'marked_at': 1}
    if market == 'swap':
        item.update(side='LONG', margin=100, leverage=1)
    account['spot' if market == 'spot' else 'positions'][key] = item
    with store.connection() as conn:
        conn.execute('UPDATE paper_accounts SET payload=%s WHERE namespace=%s AND user_id=%s',
                     (json.dumps(account), caller['paper_namespace'], '123456'))
        conn.commit()
    return item


@pytest.mark.parametrize('market,action', [('spot', 'close'), ('spot', 'protect'), ('swap', 'close'), ('swap', 'protect')])
def test_foreign_explicit_batch_target_rejects_before_any_financial_step(ledger, monkeypatch, market, action):
    caller, _, _ = ledger
    create(caller)
    quotes = _venue_quote_fixture(monkeypatch)
    _open_venue(caller, market, 'binance')
    foreign = _seed_foreign(caller, market)
    from test_paper_binance_scope import raw_account
    before = raw_account(caller)
    symbol = foreign['symbol']
    requested = {'action': action, 'market_type': market, 'symbol': symbol, 'exchange': 'bitget',
                 'side': 'LONG', 'amount': .5, 'reason': 'requested historical target',
                 **({'order_type': 'market'} if action == 'close' else {'stop_loss': 90})}
    count = len(quotes)
    result = batch(caller, [opening(market, amount=.1), requested])
    assert result['status'] == 'failed' and not any(item['attempted'] for item in result['results'])
    assert raw_account(caller) == before and len(quotes) == count
    assert batch(caller, [opening(market, amount=.1), requested]) == result


@pytest.mark.parametrize('market', ['spot', 'swap'])
def test_historical_foreign_only_exit_is_not_repriced_at_binance(ledger, monkeypatch, market):
    caller, _, _ = ledger
    create(caller)
    quotes = _venue_quote_fixture(monkeypatch)
    foreign = _seed_foreign(caller, market)
    result = batch(caller, [{'action': 'close', 'market_type': market, 'symbol': foreign['symbol'], 'side': 'LONG',
                            'order_type': 'market', 'close_all': True, 'reason': 'requested original holding'}])
    assert result['status'] == 'failed' and not any(item['attempted'] for item in result['results'])
    assert quotes == []


@pytest.mark.parametrize('market', ['spot', 'swap'])
def test_binance_default_entry_does_not_merge_foreign_same_symbol(ledger, monkeypatch, market):
    caller, _, _ = ledger
    create(caller)
    _venue_quote_fixture(monkeypatch)
    foreign = _seed_foreign(caller, market)
    result = batch(caller, [opening(market, amount=.1)])
    assert result['status'] == 'completed'
    from test_paper_binance_scope import raw_account
    resources = raw_account(caller)['spot' if market == 'spot' else 'positions']
    assert resources['legacy-bitget'] == foreign
    assert [p['amount'] for p in paper.get_account(caller, '123456')['spot' if market == 'spot' else 'positions']] == [.1]


@pytest.mark.parametrize('previously_committed', [False, True])
def test_sdk_intent_storage_error_keeps_original_id_and_does_not_claim_prior_failure(ledger, monkeypatch, previously_committed):
    import asyncio
    from backend import storage
    from mcp.server.fastmcp import FastMCP
    from backend.mcp import service
    from backend.mcp.tools import register_tools
    from backend.mcp.paper_tools import register_paper_tools
    caller, _, quotes = ledger
    create(caller)
    requested = [opening()]
    parent = 'sdk-storage-original-parent'
    original = batch(caller, requested, operation_id=parent) if previously_committed else None
    before = paper.get_account(caller, '123456')
    quote_count = len(quotes)
    monkeypatch.setattr(service, 'principal', lambda: caller)
    server = FastMCP('Offline paper intent storage boundary')
    register_tools(server)
    register_paper_tools(server)
    def unreadable(*args, **kwargs):
        raise storage.OperationalError('Original parent ledger is unavailable')
    with monkeypatch.context() as patch:
        patch.setattr(paper_batch, '_load', unreadable)
        blocks = asyncio.run(server.call_tool('execute_trade_actions', {
            'user_id': '123456', 'operation_id': parent, 'contract_version': 3, 'actions': requested}))
    assert not blocks.isError, blocks
    result = blocks.structuredContent or json.loads(blocks.content[0].text)
    assert result['operation_id'] == parent and result['status'] == 'unknown'
    assert result['error_code'] == 'intent_storage_unavailable'
    assert result['phase'] == 'persist_intent' and result['submission_state'] == 'not_submitted'
    assert result['submitted'] is False and result['query']['status'] == 'failed'
    assert result['verification'] == {'state': 'background', 'operation_id': parent}
    assert paper.get_account(caller, '123456') == before and len(quotes) == quote_count
    if previously_committed:
        recovered = paper_batch.operation(caller, '123456', parent)
        assert recovered['status'] == 'completed'
        assert recovered['results'][0]['operation_id'] == original['results'][0]['operation_id']
        assert len(quotes) == quote_count
    else:
        assert paper_batch.operation(caller, '123456', parent) is None


def test_postgres_failure_during_intent_insert_rolls_back_without_dispatch(ledger, monkeypatch):
    from backend import storage
    from contextlib import contextmanager
    caller, _, quotes = ledger
    create(caller)
    original_connection = store.connection
    class Connection:
        def __init__(self, conn):
            self.conn = conn
        def execute(self, statement, *args):
            if statement.startswith('INSERT INTO paper_batches'):
                raise storage.OperationalError('Intent insert failed')
            return self.conn.execute(statement, *args)
        def __getattr__(self, name):
            return getattr(self.conn, name)
    @contextmanager
    def failed_intent_connection(*args, **kwargs):
        with original_connection(*args, **kwargs) as conn:
            yield Connection(conn)
    with monkeypatch.context() as patch:
        patch.setattr(store, 'connection', failed_intent_connection)
        result = batch(caller, [opening()])
    assert result['operation_id'] == 'paper-parent-0001'
    assert result['error_code'] == 'intent_storage_unavailable' and result['status'] == 'unknown'
    assert result['submission_state'] == 'not_submitted' and result['query']['status'] == 'failed'
    assert quotes == [] and paper.get_account(caller, '123456')['positions'] == []
    assert paper_batch.operation(caller, '123456', result['operation_id']) is None
