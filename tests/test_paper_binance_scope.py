"""Isolated Binance policy, legacy provenance and financial commitment checks."""
import copy
import json
import time
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from backend.mcp import paper, paper_market, paper_orders, paper_policy, paper_storage, store, transfer
from test_mcp_paper import create, ledger as original_ledger


@pytest.fixture
def ledger(original_ledger):
    caller, quote, effects = original_ledger
    quote['exchange'] = 'binance'
    return caller, quote, effects


def raw_account(caller):
    with store.connection() as conn:
        return json.loads(conn.execute('SELECT payload FROM paper_accounts WHERE namespace=? AND user_id=?',
                                       (caller['paper_namespace'], '123456')).fetchone()[0])


def seed_legacy(caller, *, exchange='okx', symbol='BTC/USDT', reserve=200):
    account = raw_account(caller)
    account['spot'][symbol] = {'symbol': symbol, 'exchange': exchange, 'cycle_id': 'original-legacy-cycle',
                              'amount': 2, 'entry_price': 50, 'mark_price': 70, 'marked_at': 123}
    account['reserved_cash'] = reserve
    order = {'namespace': caller['paper_namespace'], 'user_id': '123456', 'order_id': 'legacy-entry',
             'state': 'open', 'market_type': 'spot', 'symbol': symbol, 'exchange': exchange, 'action': 'buy',
             'order_type': 'limit', 'price': 50, 'amount': 1, 'reserved_cash': reserve,
             'reserved_spot': 0, 'created_at': 1, 'cycle_id': 'original-legacy-cycle'}
    with store.connection() as conn:
        conn.execute('UPDATE paper_accounts SET payload=? WHERE namespace=? AND user_id=?',
                     (json.dumps(account), caller['paper_namespace'], '123456'))
        paper_orders.save(conn, order)
        conn.commit()
    return copy.deepcopy(account), copy.deepcopy(order)


def test_old_default_loads_but_new_non_binance_configuration_rejects(ledger):
    caller, _, _ = ledger
    original = {**paper.settings(), 'exchange': 'okx'}
    store.put('paper_settings', 'global', original)
    assert paper.settings()['exchange'] == 'binance'
    assert store.get('paper_settings', 'global') == original
    create(caller)
    assert paper.bank(caller, '123456', 'borrow', 10, 'legacy-config-bank')['account']['debt'] == 10
    with pytest.raises(ValidationError):
        paper.PaperSettings(exchange='okx')
    with pytest.raises(ValidationError):
        transfer.import_settings({'paper_settings': original})
    assert store.get('paper_settings', 'global') == original


def test_explicit_foreign_venue_is_rejected_before_client_or_quote(ledger, monkeypatch):
    caller, _, _ = ledger
    create(caller)
    constructor = Mock()
    monkeypatch.setattr(paper_market, 'client', constructor)
    with pytest.raises(ValueError, match='only supports Binance'):
        paper_market.search('BTC', exchange_name='okx')
    with pytest.raises(ValueError, match='only supports Binance'):
        paper_market.resolve_quote('BTC', exchange_name='bybit')
    quote = Mock()
    monkeypatch.setattr(paper, 'market', quote)
    with pytest.raises(ValueError, match='only supports Binance'):
        paper.trade(caller, '123456', 'spot', 'BTC', 'buy', 1, 'foreign-market-entry', exchange_name='bitget')
    constructor.assert_not_called()
    quote.assert_not_called()


def test_binance_failure_has_no_fallback(ledger, monkeypatch):
    quote = Mock(side_effect=TimeoutError('Binance unavailable'))
    search = Mock()
    monkeypatch.setattr(paper_market, 'quote', quote)
    monkeypatch.setattr(paper_market, 'search', search)
    with pytest.raises(TimeoutError):
        paper_market.resolve_quote('BTC')
    search.assert_not_called()
    assert quote.call_args.kwargs['exchange_name'] == 'binance'


@pytest.mark.parametrize('exchange,classification', [('okx', 'excluded_venue'), ('binance', 'unresolved_origin'), (None, 'unresolved_origin')])
def test_filtered_legacy_retains_cash_debt_reservation_and_raw_origin(ledger, exchange, classification):
    caller, _, _ = ledger
    create(caller)
    before, _ = seed_legacy(caller, exchange=exchange)
    for _ in range(2):
        view = paper.get_account(caller, '123456')
        assert view['spot'] == [] and view['equity'] is None and view['net_equity'] is None
        assert view['cash'] == 1000 and view['debt'] == 0 and view['available_cash'] == 800
        assert classification in view['completeness']['reason_codes']
        assert raw_account(caller) == before
    with pytest.raises(ValueError, match='Repayment'):
        paper.bank(caller, '123456', 'repay', 900, 'cannot-spend-legacy-reserve')
    assert raw_account(caller) == before


def test_new_binance_same_symbol_never_washes_legacy_origin_or_cycle(ledger):
    caller, _, _ = ledger
    create(caller)
    before, _ = seed_legacy(caller, exchange='binance')
    original = copy.deepcopy(before['spot']['BTC/USDT'])
    paper.trade(caller, '123456', 'spot', 'BTC', 'buy', 1, 'new-independent-binance')
    after = raw_account(caller)
    assert after['spot']['BTC/USDT'] == original
    created = [value for key, value in after['spot'].items() if key != 'BTC/USDT']
    assert len(created) == 1 and created[0]['amount'] == 1
    assert created[0]['origin_version'] == 1 and created[0]['cycle_id'] != original['cycle_id']
    view = paper.get_account(caller, '123456')
    assert len(view['spot']) == 1 and view['spot'][0]['amount'] == 1
    assert view['reserved_cash'] == 200 and view['equity'] is None


def test_new_binance_same_swap_side_keeps_old_margin_and_only_exits_new_cycle(ledger):
    caller, _, _ = ledger
    create(caller)
    account = raw_account(caller)
    old = {'symbol': 'ETH/USDT:USDT', 'side': 'SHORT', 'exchange': 'binance', 'cycle_id': 'legacy-short-cycle',
           'amount': .03, 'entry_price': 100, 'mark_price': 100, 'margin': 50, 'leverage': 2, 'marked_at': 1}
    account['positions']['legacy-short'] = copy.deepcopy(old)
    with store.connection() as conn:
        conn.execute('UPDATE paper_accounts SET payload=?', (json.dumps(account),))
        conn.commit()
    paper.trade(caller, '123456', 'swap', 'ETH', 'open_short', .03, 'new-independent-short', leverage=2)
    paper.trade(caller, '123456', 'swap', 'ETH', 'close_short', .01, 'exit-only-new-short')
    after = raw_account(caller)
    assert after['positions']['legacy-short'] == old
    new = [position for key, position in after['positions'].items() if key != 'legacy-short']
    assert len(new) == 1 and new[0]['amount'] == pytest.approx(.02)
    assert new[0]['margin'] == 1 and new[0]['cycle_id'] != old['cycle_id']
    assert paper.get_account(caller, '123456')['filter']['retained_margin'] == 50


def test_oversized_account_is_checked_before_financial_decode_or_mutation(ledger, monkeypatch):
    caller, _, _ = ledger
    create(caller)
    before = raw_account(caller)
    before['legacy_diagnostic'] = 'x' * 10000
    with store.connection() as conn:
        conn.execute('UPDATE paper_accounts SET payload=?', (json.dumps(before),))
        conn.commit()
    monkeypatch.setenv('PAPER_MAX_PAYLOAD_BYTES', '2048')
    with pytest.raises(paper_storage.PaperPayloadLimit):
        paper.bank(caller, '123456', 'borrow', 100, 'oversized-no-new-effects')
    assert raw_account(caller) == before


@pytest.mark.parametrize('kind,symbol,action', [('spot', 'BTC', 'buy'), ('swap', 'ETH', 'open_long')])
def test_resource_identity_and_cycle_survive_additions(ledger, kind, symbol, action):
    caller, _, _ = ledger
    create(caller)
    first = paper.trade(caller, '123456', kind, symbol, action, 1, 'persistent-resource-first')
    second = paper.trade(caller, '123456', kind, symbol, action, 1, 'persistent-resource-second')
    field = 'spot' if kind == 'spot' else 'positions'
    before, after = first['account'][field][0], second['account'][field][0]
    assert before['resource_id'] == after['resource_id']
    assert before['cycle_id'] == after['cycle_id']
    assert after['amount'] == 2


def test_order_edit_keeps_resource_identity(ledger):
    caller, _, _ = ledger
    create(caller)
    order = paper_orders.place(caller, '123456', 'spot', 'BTC', 'buy', 'limit', 'persistent-order-first',
                               amount=1, price=90)['order']
    changed = paper_orders.edit(caller, '123456', order['order_id'], 'persistent-order-edit', price=85)['order']
    assert changed['resource_id'] == order['resource_id']
    assert changed['order_id'] == order['order_id']


def test_original_replay_precedes_changed_leverage_policy(ledger, monkeypatch):
    caller, _, quotes = ledger
    create(caller)
    original = paper.trade(caller, '123456', 'swap', 'ETH', 'open_long', 1, 'old-leverage-original', leverage=10)
    store.put('paper_settings', 'global', {**paper.settings(), 'max_leverage': 2})
    count = len(quotes)
    assert paper.trade(caller, '123456', 'swap', 'ETH', 'open_long', 1, 'old-leverage-original', leverage=10) == original
    assert len(quotes) == count


def test_canonical_history_is_filtered_and_bounds_storage_before_decode(ledger, monkeypatch):
    from backend.mcp import canonical_tools
    caller, _, _ = ledger
    create(caller)
    with store.connection() as conn:
        for venue in ('binance', 'okx', 'bitget'):
            paper._event(conn, raw_account(caller), 'buy', {'fill': {'exchange': venue, 'symbol': 'BTC/USDT', 'amount': 1}}, 'history-' + venue)
        conn.commit()
    monkeypatch.setenv('PAPER_HISTORY_SCAN_LIMIT', '1')
    page = canonical_tools.records(caller, 'fills', user_id='123456', limit=1, offset=0, detail=True)
    assert page['items'] == [] and page['has_more'] and page['next_offset'] == 1
    second = canonical_tools.records(caller, 'fills', user_id='123456', limit=1, offset=1, detail=True)
    assert second['items'] == [] and second['has_more'] and second['next_offset'] == 2
    third = canonical_tools.records(caller, 'fills', user_id='123456', limit=1, offset=2, detail=True)
    assert third['items'][0]['payload']['fill']['exchange'] == 'binance'
    assert third['items'][0]['operation_id'] == 'history-binance'


def test_balance_only_facet_retains_scope_and_incompleteness(ledger):
    from backend.mcp import canonical_tools
    caller, _, _ = ledger
    create(caller)
    seed_legacy(caller)
    value = canonical_tools.account_state(caller, user_id='123456', include=['balances'])
    assert value['balances']['available_cash'] == 800
    assert value['balances']['equity'] is None
    assert value['filter']['exchange'] == 'binance'
    assert value['completeness']['valuation'] == 'incomplete'
    assert 'positions' not in value and 'spot' not in value


def test_origin_evidence_progress_is_durable_and_never_adopts_later_fills(ledger, monkeypatch):
    caller, _, _ = ledger
    create(caller)
    before, _ = seed_legacy(caller, exchange='binance')
    monkeypatch.setenv('PAPER_SCAN_BATCH_SIZE', '1')
    with store.connection() as conn:
        paper.upgrade_account(conn, copy.deepcopy(before))
        conn.commit()
        first = json.loads(conn.execute("SELECT payload FROM paper_origin_scans WHERE kind='spot'").fetchone()[0])
    # A new independent fill cannot become evidence of the old cycle.
    paper.trade(caller, '123456', 'spot', 'BTC', 'buy', 1, 'later-independent-cycle')
    paper.get_account(caller, '123456')
    with store.connection() as conn:
        state = json.loads(conn.execute("SELECT payload FROM paper_origin_scans WHERE kind='spot'").fetchone()[0])
    assert state['horizon'] == first['horizon']
    assert state['complete'] and state['sources'] == []
    assert raw_account(caller)['spot']['BTC/USDT'] == before['spot']['BTC/USDT']


def test_raw_audit_large_chinese_pages_reconstruct_without_json_decode(ledger, monkeypatch):
    import base64
    caller, _, _ = ledger
    create(caller)
    value = raw_account(caller)
    value['legacy_note'] = '历史中文🙂甲乙丙丁' * 1000
    encoded = json.dumps(value, ensure_ascii=False)
    with store.connection() as conn:
        conn.execute('UPDATE paper_accounts SET payload=?', (encoded,))
        conn.commit()
    monkeypatch.setenv('PAPER_MAX_PAYLOAD_BYTES', '1024')
    pages, offset, version = [], 0, None
    while True:
        with store.connection() as conn:
            page = paper_storage.raw_audit_chunk(conn, 'qqbot', '123456', 'account',
                                                  offset=offset, length=127, version=version, encoding='text')
        assert len(page['text']) <= 127
        version = page['version']
        pages.append(page['text'])
        if not page['has_more']:
            break
        offset = page['next_offset']
    assert ''.join(pages) == encoded
    with store.connection() as conn:
        byte_page = paper_storage.raw_audit_chunk(conn, 'qqbot', '123456', 'account', length=17)
    assert base64.b64decode(byte_page['content']) == encoded.encode('utf-8')[:17]
    with store.connection() as conn:
        with pytest.raises(ValueError, match='not found'):
            paper_storage.raw_audit_chunk(conn, 'other-namespace', '123456', 'account', length=17)


def test_raw_audit_changed_version_cannot_mix_evidence(ledger):
    caller, _, _ = ledger
    create(caller)
    with store.connection() as conn:
        first = paper_storage.raw_audit_chunk(conn, 'qqbot', '123456', 'account', length=31)
    paper.bank(caller, '123456', 'borrow', 5, 'audit-following-change')
    with store.connection() as conn:
        with pytest.raises(ValueError, match='audit_version_changed'):
            paper_storage.raw_audit_chunk(conn, 'qqbot', '123456', 'account', offset=31, length=31, version=first['version'])


def test_background_single_receipt_updates_without_model_query_or_original_change(ledger):
    import base64
    caller, ticker, calls = ledger
    create(caller)
    original = paper_orders.place(caller, '123456', 'spot', 'BTC', 'buy', 'limit', 'background-original-order', amount=1, price=90)
    with store.connection() as conn:
        baseline = conn.execute('SELECT fingerprint,result FROM paper_operations WHERE operation_id=?', ('background-original-order',)).fetchone()
        baseline = tuple(baseline)
    ticker.update(price=85, bid=85, ask=85)
    paper.reconcile()
    count = len(calls)
    for _ in range(3):
        paper_storage.recover_receipts()
    assert len(calls) == count
    with store.connection() as conn:
        after = tuple(conn.execute('SELECT fingerprint,result FROM paper_operations WHERE operation_id=?', ('background-original-order',)).fetchone())
        latest = paper_storage.audit_chunk(conn, 'qqbot', '123456', 'background-original-order', view='latest', length=65536)
        revisions = conn.execute('SELECT COUNT(*) FROM paper_receipt_revisions WHERE operation_id=?', ('background-original-order',)).fetchone()[0]
    result = json.loads(base64.b64decode(latest['content']))
    assert after == baseline and original['order']['state'] == 'open'
    assert result['status'] == 'completed' and result['order']['state'] == 'filled'
    assert result['fill']['amount'] == 1
    assert revisions == 2


def test_admin_audit_requires_jwt_and_reaches_excluded_raw_ledger(ledger):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.app.core.security import create_access_token
    from backend.mcp.management import router
    caller, _, _ = ledger
    create(caller)
    before, order = seed_legacy(caller)
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        url = '/api/mcp/paper/accounts/qqbot/123456/audit'
        assert client.get(url).status_code == 401
        auth = {'Authorization': 'Bearer ' + create_access_token()}
        response = client.get(url, headers=auth, params={'encoding': 'text', 'length': 2048})
        assert response.status_code == 200
        assert json.loads(response.json()['text']) == before
        result = client.get('/api/mcp/paper/ledger/qqbot/123456/order/' + order['order_id'] + '/audit', headers=auth,
                            params={'encoding': 'text', 'length': 2048})
        assert result.status_code == 200
        assert json.loads(result.json()['text'])['exchange'] == 'okx'


def test_paper_sdk_schemas_offer_only_binance(ledger, monkeypatch):
    import asyncio
    from mcp.server.fastmcp import FastMCP
    from backend.mcp import service
    from backend.mcp.tools import register_tools
    from backend.mcp.paper_tools import register_paper_tools
    caller, _, _ = ledger
    monkeypatch.setattr(service, 'principal', lambda: caller)
    server = FastMCP('Binance scope schema')
    register_tools(server)
    register_paper_tools(server)
    offered = asyncio.run(server.list_tools())
    assert len(offered) == 10
    for tool in offered:
        schemas = [tool.inputSchema, *tool.inputSchema.get('$defs', {}).values()]
        for schema in schemas:
            exchange = schema.get('properties', {}).get('exchange')
            if exchange:
                assert 'okx' not in json.dumps(exchange) and 'bybit' not in json.dumps(exchange)
                assert 'binance' in json.dumps(exchange)


def test_known_foreign_order_in_later_step_preflights_before_first_effect(ledger):
    from backend.mcp import paper_batch
    caller, _, quotes = ledger
    create(caller)
    original, order = seed_legacy(caller)
    result = paper_batch.execute(caller, '123456', {'contract_version': 3, 'actions': [
        {'action': 'open', 'market_type': 'spot', 'symbol': 'ETH/USDT', 'side': 'LONG', 'order_type': 'market', 'amount': 1, 'reason': 'entry'},
        {'action': 'cancel', 'order_id': order['order_id'], 'reason': 'legacy target'}]}, 'later-original-target')
    assert result['status'] == 'failed' and not any(item['attempted'] for item in result['results'])
    assert raw_account(caller) == original and quotes == []


def test_maintenance_cursor_reaches_large_accounts_without_repricing_excluded_resources(ledger, monkeypatch):
    caller, _, quotes = ledger
    create(caller)
    original, _ = seed_legacy(caller)
    account = raw_account(caller)
    for index in range(5):
        symbol = f'BN{index}/USDT'
        account['spot'][symbol] = paper_policy.stamp({'symbol': symbol, 'exchange': 'binance', 'cycle_id': 'bn-cycle-' + str(index),
                                                    'amount': 1, 'entry_price': 100, 'mark_price': 100, 'marked_at': 1})
    with store.connection() as conn:
        conn.execute('UPDATE paper_accounts SET payload=? WHERE namespace=? AND user_id=?',
                     (json.dumps(account), 'qqbot', '123456'))
        conn.commit()
    monkeypatch.setenv('PAPER_SCAN_BATCH_SIZE', '2')
    seen = []
    for _ in range(5):
        page = paper.maintenance_plan(max_items=1)
        assert len(page['items']) == 1 and page['scanned_accounts'] <= 2
        seen.append(page['items'][0]['symbol'])
    assert seen == [f'BN{index}/USDT' for index in range(5)]
    assert raw_account(caller) == account and raw_account(caller)['spot']['BTC/USDT'] == original['spot']['BTC/USDT']
    assert quotes == []


def test_large_pure_binance_history_eventually_finishes_performance_scan(ledger, monkeypatch):
    caller, _, _ = ledger
    create(caller)
    with store.connection() as conn:
        account = raw_account(caller)
        for index in range(7):
            paper._event(conn, account, 'buy', {'fill': {'exchange': 'binance', 'symbol': 'BTC/USDT', 'amount': 1}}, f'old-event-{index}')
        conn.commit()
    monkeypatch.setenv('PAPER_SCAN_BATCH_SIZE', '2')
    first = paper.get_account(caller, '123456')
    assert first['performance_pnl'] is None
    for _ in range(4):
        view = paper.get_account(caller, '123456')
    assert view['performance_pnl'] == 0 and view['completeness']['valuation'] == 'complete'


def test_admin_namespace_page_can_reach_later_spaces(ledger, monkeypatch):
    from backend.mcp import paper_competition
    caller, _, _ = ledger
    for namespace in ('aaa', 'bbb', 'ccc'):
        create({**caller, 'paper_namespace': namespace})
    monkeypatch.setenv('PAPER_SCAN_BATCH_SIZE', '2')
    first = paper_competition.admin_data()
    assert first['namespaces']['has_more'] and first['namespaces']['next_cursor'] == 'bbb'
    second = paper_competition.admin_data(namespace_cursor=first['namespaces']['next_cursor'])
    assert [row['namespace'] for row in second['rankings']] == ['ccc']
    assert second['namespaces']['scan_complete']


@pytest.mark.parametrize('source,conflicting,classification', [('binance', False, 'binance'), ('okx', False, 'excluded_venue'), ('okx', True, 'unresolved_origin')])
def test_legacy_source_uses_original_cycle_evidence_instead_of_backfilled_field(ledger, source, conflicting, classification):
    caller, _, _ = ledger
    create(caller)
    paper.trade(caller, '123456', 'spot', 'BTC', 'buy', 1, 'original-cycle-evidence')
    account = raw_account(caller)
    holding = account['spot']['BTC/USDT']
    for name in ('origin_version', 'origin_exchange', 'resource_id'):
        holding.pop(name, None)
    # Recreate the historical original receipt schema, including a durable
    # cycle; the current row's Binance field may be the old upgrade's backfill.
    with store.connection() as conn:
        receipt = json.loads(conn.execute('SELECT result FROM paper_operations WHERE operation_id=?', ('original-cycle-evidence',)).fetchone()[0])
        receipt['fill']['exchange'] = source
        receipt['account']['spot'][0]['exchange'] = 'binance' if conflicting else source
        conn.execute('UPDATE paper_operations SET result=? WHERE operation_id=?', (json.dumps(receipt), 'original-cycle-evidence'))
        conn.execute('UPDATE paper_accounts SET payload=?', (json.dumps(account),))
        conn.commit()
    view = paper.get_account(caller, '123456')
    assert bool(view['spot']) == (classification == 'binance')
    if classification != 'binance':
        assert classification in view['completeness']['reason_codes']
    assert raw_account(caller) == account
    with store.connection() as conn:
        evidence = json.loads(conn.execute("SELECT payload FROM paper_origin_scans WHERE kind='spot'").fetchone()[0])
    assert evidence['complete'] and evidence['evidence'][0]['operation_id'] == 'original-cycle-evidence'


def test_oversized_blob_is_rejected_before_any_payload_read_and_raw_audit_stays_bounded(ledger, monkeypatch):
    caller, _, _ = ledger
    create(caller)
    with store.connection() as conn:
        conn.execute('UPDATE paper_accounts SET payload=zeroblob(?)', (8 * 1024 * 1024,))
        conn.commit()
    reads = []
    class TrackingBlob:
        def __init__(self, blob):
            self.blob = blob
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.blob.close()
        def __len__(self):
            return len(self.blob)
        def seek(self, offset):
            self.blob.seek(offset)
        def read(self, size=-1):
            reads.append(size)
            return self.blob.read(size)
    class Connection:
        def __init__(self, connection):
            self.connection = connection
        def __getattr__(self, name):
            return getattr(self.connection, name)
        def blobopen(self, *args, **kwargs):
            return TrackingBlob(self.connection.blobopen(*args, **kwargs))
    with store.connection() as conn:
        wrapped = Connection(conn)
        with pytest.raises(paper_storage.PaperPayloadLimit):
            paper_storage.one(wrapped, 'paper_accounts', 'payload', 'namespace=? AND user_id=?', ('qqbot', '123456'))
        assert reads == []
        page = paper_storage.raw_audit_chunk(wrapped, 'qqbot', '123456', 'account', length=31)
        assert reads == [31] and page['total_bytes'] == 8 * 1024 * 1024
        assert page['has_more'] and page['next_offset'] == 31


def test_background_round_shares_deadline_and_progresses_across_small_work_budgets(ledger, monkeypatch):
    import asyncio
    caller, _, quotes = ledger
    create(caller)
    account, old_order = seed_legacy(caller)
    for index in range(5):
        symbol = f'BN{index}/USDT'
        account['spot'][symbol] = paper_policy.stamp({'symbol': symbol, 'exchange': 'binance', 'cycle_id': 'cycle-' + str(index),
                                                    'amount': 1, 'entry_price': 100, 'mark_price': 100, 'marked_at': 1})
    original_legacy = copy.deepcopy(account['spot']['BTC/USDT'])
    with store.connection() as conn:
        conn.execute('UPDATE paper_accounts SET payload=?', (json.dumps(account),))
        conn.commit()
    monkeypatch.setenv('PAPER_SCAN_BATCH_SIZE', '2')
    deadlines = []
    async def run(kind, callback, *, label=None, deadline=None):
        deadlines.append(deadline)
        return callback()
    monkeypatch.setattr(paper, 'run', run)
    for expected in (2, 2, 1):
        start = len(deadlines)
        facts = asyncio.run(paper.maintenance_round(asyncio.Event()))
        assert facts['processed_instruments'] == expected and facts['scanned_accounts'] <= 2
        assert all(value == deadlines[start] for value in deadlines[start:])
        assert deadlines[start] > time.monotonic()
    assert {symbol for symbol, _ in quotes} == {f'BN{index}/USDT' for index in range(5)}
    assert raw_account(caller)['spot']['BTC/USDT'] == original_legacy
    with store.connection() as conn:
        raw = paper_storage.one(conn, 'paper_orders', 'payload', 'order_id=?', (old_order['order_id'],))
    assert raw['state'] == 'open' and raw['reserved_cash'] == old_order['reserved_cash']


def test_legacy_foreign_retry_and_lost_parent_recover_only_original_atomic_evidence(ledger):
    import os
    from backend.mcp import paper_batch
    caller, _, quotes = ledger
    create(caller)
    before, _ = seed_legacy(caller)
    request = {'action': 'buy', 'market_type': 'spot', 'symbol': 'BTC/USDT', 'amount': '1',
               'quote_amount': None, 'margin_amount': None, 'close_all': False, 'leverage': None,
               'exchange': 'okx', 'stop_loss': None, 'take_profit': None}
    parent_id = 'legacy-foreign-parent'
    child_id = 'paper-child:' + store.digest('\0'.join(('qqbot', '123456', parent_id, '0')))
    original = {'status': 'completed', 'paper': True, 'operation_id': child_id,
                'account': paper.snapshot(before, project=False),
                'fill': {'exchange': 'okx', 'market_type': 'spot', 'symbol': 'BTC/USDT', 'amount': 1, 'price': 50}}
    original_text = json.dumps(original)
    original_hash = store.digest(json.dumps(request, sort_keys=True, separators=(',', ':'), allow_nan=False))
    actions = [{'action': 'open', 'market_type': 'spot', 'exchange': 'okx', 'symbol': 'BTC/USDT', 'side': 'LONG',
                'order_type': 'market', 'amount': 1, 'reason': 'old request'}] * 3
    parent_request = {'contract_version': 3, 'actions': actions}
    items = [{'index': index, 'action': 'open', 'operation_id': child_id if index == 0 else 'unresolved-child-' + str(index),
              'status': ('pending', 'unknown', 'not_executed')[index], 'attempted': index < 2} for index in range(3)]
    payload = {'status': 'started', 'operation_id': parent_id, 'contract_version': 3, 'paper': True,
               'request': parent_request, 'results': items}
    with store.connection() as conn:
        conn.execute('INSERT INTO paper_operations VALUES(?,?,?,?,?,?)', ('qqbot', '123456', child_id, original_hash, caller['id'], original_text))
        conn.execute('INSERT INTO paper_batches VALUES(?,?,?,?,?,?,?,?)',
                     ('qqbot', '123456', parent_id, store.digest(paper_batch._encode(parent_request)), paper_batch._encode(payload), os.getpid(), 'dead-token', 1))
        conn.commit()
    assert paper.trade(caller, '123456', 'spot', 'BTC', 'buy', 1, child_id, exchange_name='okx') == original
    result = paper_batch.execute(caller, '123456', parent_request, parent_id)
    assert [item['status'] for item in result['results']] == ['completed', 'unknown', 'not_executed']
    assert [item['index'] for item in result['results']] == [0, 1, 2]
    assert raw_account(caller) == before and quotes == []
    with store.connection() as conn:
        assert tuple(conn.execute('SELECT fingerprint,result FROM paper_operations WHERE operation_id=?', (child_id,)).fetchone()) == (original_hash, original_text)


def test_conditional_child_and_parent_update_on_background_round_and_same_id_retry(ledger):
    import asyncio
    from backend.mcp import paper_batch
    caller, ticker, calls = ledger
    create(caller)
    paper.trade(caller, '123456', 'swap', 'ETH', 'open_long', 1, 'entry-before-background-close')
    request = {'contract_version': 3, 'actions': [{'action': 'close', 'market_type': 'swap', 'symbol': 'ETH/USDT:USDT',
               'side': 'LONG', 'order_type': 'take_profit', 'close_all': True, 'take_profit': 110, 'reason': 'original close'}]}
    parent = paper_batch.execute(caller, '123456', request, 'automatic-background-parent')
    child = parent['results'][0]['operation_id']
    assert parent['status'] == 'submitted' and parent['results'][0]['order_status'] == 'waiting_trigger'
    with store.connection() as conn:
        baseline = tuple(conn.execute('SELECT fingerprint,result FROM paper_operations WHERE operation_id=?', (child,)).fetchone())
    ticker.update(price=120, bid=120, ask=120)
    # First ordinary maintenance round matches the accepted original order;
    # the next advances its receipts. No model query or executor retry occurs.
    asyncio.run(paper.maintenance_round(asyncio.Event()))
    asyncio.run(paper.maintenance_round(asyncio.Event()))
    count = len(calls)
    with store.connection() as conn:
        latest = paper_storage.audit_chunk(conn, 'qqbot', '123456', child, view='latest', length=65536)
        import base64
        result = json.loads(base64.b64decode(latest['content']))
        assert tuple(conn.execute('SELECT fingerprint,result FROM paper_operations WHERE operation_id=?', (child,)).fetchone()) == baseline
    assert result['status'] == 'completed' and result['order_status'] == 'filled' and result['filled_amount'] == 1
    replay = paper_orders.place(caller, '123456', 'swap', 'ETH/USDT:USDT', 'close_long', 'take_profit', child,
                                 close_all=True, trigger_price=110.0, exchange_name='binance')
    assert replay['status'] == 'completed' and replay['order_status'] == 'filled'
    assert len(calls) == count
    assert paper_batch.operation(caller, '123456', parent['operation_id'])['status'] == 'completed'


def test_paper_recovery_advances_real_original_receipt_while_ordinary_read_pool_is_full(ledger, monkeypatch):
    import asyncio
    import threading
    from backend.mcp import resilience
    from backend.utils import runtime_capacity
    caller, ticker, calls = ledger
    create(caller)
    original = paper_orders.place(caller, '123456', 'spot', 'BTC', 'buy', 'limit', 'reserved-original-order', amount=1, price=90)
    ticker.update(price=85, bid=85, ask=85)
    paper.reconcile()
    quote_count = len(calls)
    manager = runtime_capacity.CapacityManager(total=4, reserved=1)
    monkeypatch.setattr(runtime_capacity, '_manager', manager)
    monkeypatch.setattr(resilience, '_pools', {})
    monkeypatch.setenv('MCP_READ_CONCURRENCY', '2')
    monkeypatch.setenv('MCP_READ_QUEUE_LIMIT', '0')
    entered, release = [threading.Event(), threading.Event()], threading.Event()

    def blocked(index):
        entered[index].set()
        assert release.wait(10)
        return {}

    async def scenario():
        pending = [asyncio.create_task(resilience.run('read', lambda index=index: blocked(index))) for index in range(2)]
        lease = None
        try:
            for _ in range(1000):
                if all(event.is_set() for event in entered):
                    break
                await asyncio.sleep(.001)
            assert all(event.is_set() for event in entered)
            lease = manager.acquire('agent', timeout=0)
            assert manager.snapshot()['active'] == 3
            result = await asyncio.wait_for(resilience.run('recovery', paper_storage.recover_receipts), timeout=2)
            assert result['refreshed'] >= 1 and not release.is_set()
        finally:
            if lease:
                lease.release()
            release.set()
            await asyncio.gather(*pending)
            for pool in resilience._pools.values():
                pool.executor.shutdown(wait=True)
    asyncio.run(scenario())
    with store.connection() as conn:
        latest = paper_storage.audit_chunk(conn, 'qqbot', '123456', original['operation_id'], view='latest', length=65536)
    import base64
    assert json.loads(base64.b64decode(latest['content']))['order_status'] == 'filled'
    assert len(calls) == quote_count


def test_cancelled_accepted_order_has_terminal_latest_without_invented_fill(ledger):
    import base64
    caller, _, calls = ledger
    create(caller)
    original = paper_orders.place(caller, '123456', 'spot', 'BTC', 'buy', 'limit', 'accepted-then-cancelled', amount=1, price=90)
    paper_orders.cancel(caller, '123456', original['order']['order_id'], 'explicit-original-cancel')
    count = len(calls)
    paper_storage.recover_receipts()
    with store.connection() as conn:
        latest = paper_storage.audit_chunk(conn, 'qqbot', '123456', original['operation_id'], view='latest', length=65536)
        watched = conn.execute('SELECT active FROM paper_receipt_watch WHERE operation_id=?', (original['operation_id'],)).fetchone()[0]
    value = json.loads(base64.b64decode(latest['content']))
    assert value['status'] == 'completed' and value['order_status'] == 'cancelled'
    assert not value.get('fill') and not value.get('filled_amount')
    assert watched == 0 and len(calls) == count
