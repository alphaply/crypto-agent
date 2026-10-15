"""Offline exchange facts and process boundaries for exclusive management."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import json
import multiprocessing
import time
from types import SimpleNamespace

import ccxt
import pytest

from backend import database
from backend.database_schema import initialize_schema
from backend.utils.independent_exits import IndependentExits
from backend.utils.management_policy import normalize_management
from backend.utils.position_management import action_reservation, ensure_managed, management_snapshot

SYMBOL = 'ETH/USDT:USDT'


class ExclusiveExchange:
    apiKey = 'offline-exclusive-account'

    def __init__(self, exchange_id='binanceusdm', hedged=True):
        self.id, self.hedged = exchange_id, hedged
        self.now = int(time.time() * 1000)
        self.quantities = {'LONG': .02, 'SHORT': 0.0}
        self.contract_size = 1
        self.orders, self.trades, self.writes, self.reads = {}, [], [], []
        self.fail_book = None
        self.fail_trades = False
        self.timeout_create = False
        self.markets = self.load_markets()

    def load_markets(self):
        return {SYMBOL: self.market(SYMBOL)}

    def market(self, symbol):
        assert symbol in {SYMBOL, 'ETH/USDT'}
        return {'symbol': SYMBOL, 'id': 'ETHUSDT' if self.id != 'okx' else 'ETH-USDT-SWAP',
                'contract': True, 'swap': True, 'linear': True, 'contractSize': self.contract_size,
                'base': 'ETH', 'quote': 'USDT', 'settle': 'USDT', 'active': True,
                'limits': {'amount': {'min': .0001}}}

    def fetch_time(self):
        return self.now

    def fetch_position_mode(self, symbol):
        return {'hedged': self.hedged}

    def fetch_positions(self, symbols=None):
        self.reads.append('positions')
        return [{'symbol': SYMBOL, 'side': side.lower(), 'contracts': quantity,
                 'entryPrice': 2415, 'markPrice': 2500, 'contractSize': self.contract_size, 'hedged': self.hedged,
                 'leverage': 1, 'marginMode': 'cross'} for side, quantity in self.quantities.items() if quantity > 1e-12]

    def fetch_ticker(self, symbol):
        return {'symbol': SYMBOL, 'last': 2500}

    def fetch_leverage(self, symbol):
        return {'longLeverage': 1, 'shortLeverage': 1}

    def fetch_my_trades(self, symbol, since=None, limit=None, params=None):
        if self.fail_trades:
            raise ccxt.RequestTimeout('offline fill read timeout')
        until = (params or {}).get('until', float('inf'))
        rows = [row for row in self.trades if (since is None or row['timestamp'] >= since) and row['timestamp'] <= until]
        return deepcopy(rows[:limit] if limit else rows)

    def fetch_open_orders(self, symbol, since=None, limit=None, params=None):
        channel = 'algo' if (params or {}).get('trigger') else 'regular'
        kind = (params or {}).get('ordType')
        self.reads.append(('book', channel, kind))
        if self.fail_book is not None and (self.fail_book == channel or self.fail_book == kind):
            raise ccxt.RequestTimeout('offline order book timeout')
        return deepcopy([row for (book, _), row in self.orders.items() if book == channel and row['status'] == 'open'
                         and (not kind or kind == row['info'].get('ordType', 'conditional'))])

    def fetch_order(self, order_id, symbol, params=None):
        channel = 'algo' if (params or {}).get('trigger') else 'regular'
        client = (params or {}).get('clientAlgoId') or (params or {}).get('clientOrderId')
        rows = [row for (book, identity), row in self.orders.items() if (str(order_id) == identity and book == channel)
                or (client and row.get('clientOrderId') == client)]
        if len(rows) != 1:
            raise ccxt.OrderNotFound('offline target missing')
        return deepcopy(rows[0])

    def amount_to_precision(self, symbol, amount):
        return str(round(amount, 8))

    def price_to_precision(self, symbol, price):
        return str(round(price, 2))

    def external_order(self, order_id, *, side='LONG', role='exit', channel='regular', amount=.02,
                       price=2565, trigger=None, filled=0, dynamic=False, kind=None):
        action = 'buy' if (side == 'LONG') == (role == 'entry') else 'sell'
        info = {'positionSide': side if self.hedged else 'BOTH', 'posSide': side.lower() if self.hedged else 'net',
                'reduceOnly': role == 'exit', 'closePosition': dynamic, 'ordType': 'conditional'}
        row = {'id': str(order_id), 'symbol': SYMBOL, 'side': action, 'type': kind or ('stop_market' if trigger else 'limit'),
               'amount': None if dynamic else amount, 'filled': filled, 'price': price, 'status': 'open',
               'timestamp': self.now - 1000, 'clientOrderId': f'external-{channel}-{order_id}',
               'reduceOnly': role == 'exit', 'info': info}
        if trigger is not None:
            row['triggerPrice'] = trigger
            row['stopLossPrice'] = trigger
        self.orders[channel, str(order_id)] = row
        return row

    def external_fill(self, amount, *, side='LONG', order_id=None, trade_id=None, advance=100):
        self.now += advance
        trade_id = trade_id or f'fill-{len(self.trades)+1}'
        order_id = order_id or f'outside-{trade_id}'
        row = {'id': trade_id, 'order': order_id, 'symbol': SYMBOL, 'amount': abs(amount), 'price': 2500,
               'cost': abs(amount)*2500, 'side': 'buy' if (side == 'LONG') == (amount > 0) else 'sell',
               'timestamp': self.now, 'info': {'positionSide': side if self.hedged else 'BOTH', 'realizedPnl': '0'},
               'fee': {'currency': 'USDT', 'cost': 0}}
        self.quantities[side] = round(self.quantities[side] + amount, 8)
        self.trades.append(row)
        for (_, identity), order in self.orders.items():
            if identity == order_id:
                order['filled'] += abs(amount)
                if order['amount'] is not None and order['filled'] >= order['amount']:
                    order['status'] = 'closed'
        return row

    def create_order(self, symbol, order_type, side, amount, price=None, params=None):
        params = params or {}
        self.writes.append(('create', amount, deepcopy(params)))
        channel = 'algo' if params.get('stopLossPrice') or params.get('takeProfitPrice') else 'regular'
        identity = f'managed-{len(self.writes)}'
        pos_side = params.get('positionSide') or ('LONG' if side == 'sell' else 'SHORT')
        if pos_side == 'BOTH':
            pos_side = 'LONG' if side == 'sell' else 'SHORT'
        record = self.external_order(identity, side=pos_side, channel=channel, amount=amount,
                                     price=price, trigger=params.get('stopLossPrice'), dynamic=bool(params.get('closePosition')))
        record['clientOrderId'] = params['clientOrderId']
        record['type'] = order_type
        record['info'].update(params)
        if order_type == 'market' and channel == 'regular':
            assert amount <= self.quantities[pos_side] + 1e-12
            self.external_fill(-amount, side=pos_side, order_id=identity)
            record['status'] = 'closed'
        if self.timeout_create:
            self.timeout_create = False
            raise ccxt.RequestTimeout('exchange received request, ACK lost')
        return deepcopy(record)

    def edit_order(self, order_id, symbol, order_type, side, amount, price, params=None):
        self.writes.append(('edit', order_id, amount, price))
        row = self.orders['regular', str(order_id)]
        row.update(amount=amount, price=price)
        return deepcopy(row)

    def cancel_order(self, order_id, symbol, params=None):
        channel = 'algo' if (params or {}).get('trigger') else 'regular'
        self.writes.append(('cancel', channel, str(order_id)))
        row = self.orders[channel, str(order_id)]
        row['status'] = 'canceled'
        return deepcopy(row)


@pytest.fixture(params=['binanceusdm', 'okx'])
def managed(tmp_path, monkeypatch, request, financial_pg):
    monkeypatch.setattr(database, 'DB_NAME', str(tmp_path / 'exclusive.sqlite'))
    with database.get_db_conn() as conn:
        pass  # financial_pg installs the complete native migration schema.
    exchange = ExclusiveExchange(request.param)
    monkeypatch.setattr(time, 'time', lambda: exchange.now / 1000)
    service = make_service(exchange)
    return service, exchange


def make_service(exchange, owner='exclusive-test', side='LONG', policy='exclusive_scope'):
    runtime = {'config_id': owner, 'mode': 'REAL', 'market_type': 'swap', 'exit_mode': 'independent_exits',
               'symbol': SYMBOL, 'position_management_policy': policy,
               'management_scopes': [{'symbol': SYMBOL, 'side': side}] if policy == 'exclusive_scope' else []}
    return IndependentExits(SimpleNamespace(exchange=exchange, config_id=owner, market_type='swap', runtime_config=runtime))


def plan(service, side='LONG'):
    return service._load(SYMBOL, side)


def test_startup_baseline_and_three_external_orders_are_readonly_and_deduplicated(managed):
    service, exchange = managed
    exchange.external_order('tp', price=2565)
    exchange.external_order('sl', channel='algo', price=None, trigger=2458)
    exchange.external_order('entry', role='entry', amount=.01, price=2385)
    ensure_managed(service, SYMBOL)
    first = plan(service)
    assert first['state'] == 'ACTIVE'
    assert first['management']['baseline_contracts'] == .02
    assert first['management']['source'] == 'baseline'
    assert {order['id'] for order in first['exits']} == {'tp', 'sl'}
    assert [order['id'] for order in first['entries']] == ['entry']
    assert exchange.writes == []
    ensure_managed(IndependentExits(service.mt), SYMBOL)
    assert len(plan(service)['exits']) == 2 and len(plan(service)['entries']) == 1
    assert plan(service)['episode_id'] == first['episode_id']
    with database.get_db_conn() as conn:
        assert conn.execute('SELECT COUNT(*) FROM execution_fills').fetchone()[0] == 0


@pytest.mark.parametrize('changes', [[.01], [-.01], [.01, -.01]])
def test_verified_external_changes_keep_exit_management_available(managed, changes):
    service, exchange = managed
    ensure_managed(service, SYMBOL)
    for delta in changes:
        exchange.external_fill(delta)
    ensure_managed(service, SYMBOL)
    assert plan(service)['management']['contracts'] == pytest.approx(exchange.quantities['LONG'])
    result = service.close(SYMBOL, 'LONG', .001, 'market', operation_id='after-external')
    assert result['status'] in {'closed', 'completed'}
    assert exchange.quantities['LONG'] == pytest.approx(.02 + sum(changes) - .001)
    assert len([write for write in exchange.writes if write[0] == 'create']) == 1
    replay = service.close(SYMBOL, 'LONG', .001, 'market', operation_id='after-external')
    assert replay == result
    assert len([write for write in exchange.writes if write[0] == 'create']) == 1


def test_manual_quantity_price_and_trigger_changes_replace_observed_values(managed):
    service, exchange = managed
    tp = exchange.external_order('tp', price=2565)
    stop = exchange.external_order('stop', channel='algo', trigger=2458, price=None)
    ensure_managed(service, SYMBOL)
    tp.update(amount=.015, price=2580)
    stop.update(triggerPrice=2460, stopLossPrice=2460)
    ensure_managed(service, SYMBOL)
    records = {order['id']: order for order in plan(service)['exits']}
    assert records['tp']['price'] == 2580
    assert records['tp']['amount'] == .015
    assert records['stop']['trigger_price'] == 2460
    assert exchange.writes == []


def test_price_only_edit_uses_latest_manual_quantity_and_partial_fills(managed):
    service, exchange = managed
    target = exchange.external_order('tp', amount=.02, price=2565)
    ensure_managed(service, SYMBOL)
    exchange.external_fill(-.005, order_id='tp')
    target['amount'] = .015
    result = service.edit(SYMBOL, 'regular:tp', price=2580, reason='Update target after external activity', operation_id='price-only')
    assert result['status'] == 'completed'
    assert target['amount'] == .015
    assert target['filled'] == .005
    assert target['price'] == 2580


def test_external_cancel_is_persisted_and_does_not_recreate_order(managed):
    service, exchange = managed
    target = exchange.external_order('tp')
    ensure_managed(service, SYMBOL)
    target['status'] = 'canceled'
    ensure_managed(service, SYMBOL)
    assert plan(service)['exits'][0]['status'] == 'canceled'
    assert exchange.writes == []


def test_same_numeric_regular_and_algo_id_remain_distinct(managed):
    service, exchange = managed
    exchange.external_order('7')
    exchange.external_order('7', channel='algo', trigger=2458, price=None)
    ensure_managed(service, SYMBOL)
    with pytest.raises(ValueError, match='Ambiguous'):
        service.cancel(SYMBOL, '7')
    service.cancel(SYMBOL, 'algo:7', operation_id='cancel-algo')
    assert exchange.orders['algo', '7']['status'] == 'canceled'
    assert exchange.orders['regular', '7']['status'] == 'open'


def test_incomplete_order_channel_or_fill_read_never_approves_trade(managed):
    service, exchange = managed
    exchange.fail_book = 'algo'
    with pytest.raises(ccxt.RequestTimeout):
        service.close(SYMBOL, 'LONG', .001, 'market', operation_id='partial-books')
    assert exchange.writes == []
    exchange.fail_book = None
    exchange.fail_trades = True
    with pytest.raises(ValueError, match='timeout'):
        ensure_managed(service, SYMBOL)
    assert plan(service)['management']['sync_status'] == 'partial'
    assert exchange.writes == []


def test_unexplained_position_difference_keeps_fact_pending_without_fake_fill(managed):
    service, exchange = managed
    ensure_managed(service, SYMBOL)
    exchange.quantities['LONG'] += .01
    with pytest.raises(ValueError, match='match verified fills'):
        ensure_managed(service, SYMBOL)
    assert plan(service)['management']['sync_status'] == 'partial'
    with database.get_db_conn() as conn:
        assert conn.execute('SELECT COUNT(*) FROM execution_fills').fetchone()[0] == 0
    assert exchange.writes == []


def test_hedge_direction_can_have_separate_manager_but_oneway_cannot(managed):
    service, exchange = managed
    ensure_managed(service, SYMBOL)
    exchange.quantities['SHORT'] = .01
    other = make_service(exchange, owner='other-side', side='SHORT')
    ensure_managed(other, SYMBOL)
    assert plan(other, 'SHORT')['state'] == 'ACTIVE'
    assert management_snapshot(other.config_id)['scopes'][0]['side'] == 'SHORT'


def test_oneway_scope_blocks_opposite_owner_before_any_writes(managed):
    service, exchange = managed
    exchange.hedged = False
    ensure_managed(service, SYMBOL)
    with pytest.raises(ValueError, match='Another manager'):
        ensure_managed(make_service(exchange, 'other-side', 'SHORT'), SYMBOL)
    assert exchange.writes == []


def test_same_direction_owner_cannot_be_stolen(managed):
    service, exchange = managed
    ensure_managed(service, SYMBOL)
    with pytest.raises(ValueError, match='Another manager'):
        ensure_managed(make_service(exchange, 'competitor'), SYMBOL)
    assert exchange.writes == []


def test_strict_mode_does_not_implicitly_claim_startup_exposure(managed):
    _, exchange = managed
    service = make_service(exchange, policy='strict_owned')
    with pytest.raises(ValueError):
        service.close(SYMBOL, 'LONG', .001, 'market', operation_id='strict-close')
    assert exchange.writes == [] and plan(service) is None


def test_scope_policy_rejects_implicit_all_and_invalid_direction():
    base = {'mode': 'REAL', 'market_type': 'swap', 'exit_mode': 'independent_exits',
            'position_management_policy': 'exclusive_scope', 'symbol_scope': 'all'}
    with pytest.raises(ValueError, match='finite'):
        normalize_management({**base, 'management_scopes': []})
    with pytest.raises(ValueError):
        normalize_management({**base, 'management_scopes': [{'symbol': SYMBOL, 'side': 'BOTH'}]})


def test_timeout_reservation_cannot_be_overwritten_even_after_long_delay(managed):
    service, exchange = managed
    with pytest.raises(ccxt.RequestTimeout):
        with action_reservation(service, SYMBOL, 'LONG', 'original-unknown'):
            raise ccxt.RequestTimeout('write outcome unknown')
    exchange.now += 3600000
    with pytest.raises(ValueError):
        with action_reservation(service, SYMBOL, 'LONG', 'new-request'):
            pytest.fail('Unresolved financial reservation was overwritten')
    with database.get_db_conn() as conn:
        row = conn.execute('SELECT * FROM position_management_actions').fetchone()
        assert row['operation_id'] == 'original-unknown'


def _hold_reservation(db_path, entered, release):
    database.DB_NAME = db_path
    service = make_service(ExclusiveExchange())
    with action_reservation(service, SYMBOL, 'LONG', 'child-held-operation'):
        entered.set()
        release.wait(10)


def test_separate_process_cannot_submit_inside_active_reservation(managed):
    service, _ = managed
    if service.ex.id == 'okx':
        pytest.skip('Same PostgreSQL reservation and shared-directory lock already exercised with Binance scope')
    context = multiprocessing.get_context('spawn')
    entered, release = context.Event(), context.Event()
    process = context.Process(target=_hold_reservation, args=(database.DB_NAME, entered, release))
    process.start()
    try:
        assert entered.wait(10), 'Child did not acquire persisted reservation'
        with pytest.raises(ValueError, match='running'):
            with action_reservation(service, SYMBOL, 'LONG', 'parent-operation'):
                pytest.fail('Concurrent process passed persisted financial reservation')
    finally:
        release.set()
        process.join(10)
        if process.is_alive():
            process.terminate()
        assert process.exitcode == 0


def test_external_fullclose_cleans_old_orders_and_later_close_skips(managed):
    service, exchange = managed
    exchange.external_order('tp')
    exchange.external_order('sl', channel='algo', trigger=2458, price=None)
    ensure_managed(service, SYMBOL)
    exchange.external_fill(-.02)
    ensure_managed(service, SYMBOL)
    service.reconcile_all()
    exchange.now += 3000
    service.reconcile_all()
    assert plan(service)['state'] == 'DONE', (plan(service).get('error'), plan(service).get('management'), plan(service).get('exits'))
    assert all(order['status'] == 'canceled' for order in exchange.orders.values())
    writes = deepcopy(exchange.writes)
    result = service.close(SYMBOL, 'LONG', .02, 'market', operation_id='already-flat')
    assert result['status'] == 'skipped'
    assert exchange.writes == writes


def test_verified_external_flatten_reopen_creates_successor_boundary(managed):
    service, exchange = managed
    exchange.external_order('old-all', channel='algo', trigger=2458, price=None, dynamic=True)
    ensure_managed(service, SYMBOL)
    original = plan(service)['episode_id']
    exchange.external_fill(-.02)
    exchange.external_fill(.01)
    exchange.external_order('successor-entry', role='entry', price=2385, amount=.01)['timestamp'] = exchange.now
    ensure_managed(service, SYMBOL)
    current = plan(service)
    assert current['episode_id'] != original
    assert current['state'] == 'ACTIVE'
    assert exchange.quantities['LONG'] == .01
    assert exchange.orders['regular', 'successor-entry']['status'] == 'open'
    assert exchange.orders['algo', 'old-all']['status'] == 'canceled' or current['management'].get('pending_orders')
    assert not [write for write in exchange.writes if write[0] == 'create']


def test_paginated_external_fills_are_complete_deduplicated_and_checkpointed(managed):
    service, exchange = managed
    ensure_managed(service, SYMBOL)
    fill_count = 101 if exchange.id == 'okx' else 1001
    for _ in range(fill_count):
        exchange.external_fill(.0001, advance=100)
    ensure_managed(service, SYMBOL)
    assert plan(service)['management']['contracts'] == pytest.approx(.02 + fill_count * .0001)
    ensure_managed(service, SYMBOL)
    with database.get_db_conn() as conn:
        assert conn.execute('SELECT COUNT(*) FROM execution_fills').fetchone()[0] == fill_count
        state = json.loads(conn.execute('SELECT payload FROM execution_sync').fetchone()[0])
        assert state['complete'] and state['through_ms'] == exchange.now
    assert exchange.writes == []


def test_same_millisecond_baseline_overlap_uses_trade_identity_not_quantity_guess(managed):
    service, exchange = managed
    exchange.quantities['LONG'] = 0
    exchange.external_fill(.02, trade_id='already-in-baseline', advance=0)
    ensure_managed(service, SYMBOL)
    exchange.external_fill(.01, trade_id='new-in-same-millisecond', advance=0)
    ensure_managed(service, SYMBOL)
    assert plan(service)['management']['contracts'] == .03
    assert plan(service)['management']['baseline_contracts'] == .02
    assert exchange.writes == []


def test_fill_failure_keeps_successful_checkpoint_and_can_resume(managed):
    service, exchange = managed
    ensure_managed(service, SYMBOL)
    with database.get_db_conn() as conn:
        checkpoint = json.loads(conn.execute('SELECT payload FROM execution_sync').fetchone()[0])['through_ms']
    exchange.external_fill(.01)
    exchange.fail_trades = True
    with pytest.raises(ValueError):
        ensure_managed(service, SYMBOL)
    with database.get_db_conn() as conn:
        failed = json.loads(conn.execute('SELECT payload FROM execution_sync').fetchone()[0])
        assert failed['through_ms'] == checkpoint
        assert not failed['complete']
    exchange.fail_trades = False
    ensure_managed(service, SYMBOL)
    assert plan(service)['management']['contracts'] == .03
    assert exchange.writes == []


def test_unknown_hedge_direction_stays_unsupported_instead_of_guessed(managed):
    service, exchange = managed
    unsupported = exchange.external_order('unknown-direction', role='entry')
    unsupported['info'].pop('positionSide')
    unsupported['info'].pop('posSide')
    ensure_managed(service, SYMBOL)
    assert plan(service)['entries'] == []
    assert 'regular:unknown-direction' in plan(service)['management']['unsupported_orders']
    assert exchange.writes == []


def test_orders_outside_authorized_direction_are_not_claimed_or_cancelled(managed):
    service, exchange = managed
    exchange.external_order('short-order', side='SHORT', price=2300)
    ensure_managed(service, SYMBOL)
    assert plan(service)['exits'] == []
    with pytest.raises(ValueError, match='owned'):
        service.cancel(SYMBOL, 'regular:short-order', operation_id='wrong-direction')
    assert exchange.writes == []


def test_external_order_linked_to_other_owner_is_never_reassigned(managed):
    service, exchange = managed
    from backend.utils.execution_ledger import register_order
    exchange.external_order('someone-else', role='entry')
    register_order(service.account_scope, SYMBOL, 'someone-else', 'another-owner', 'entry', refresh=False, side='LONG')
    with pytest.raises(ValueError, match='another manager'):
        ensure_managed(service, SYMBOL)
    with database.get_db_conn() as conn:
        assert conn.execute("SELECT config_id FROM execution_order_links WHERE order_id='someone-else'").fetchone()[0] == 'another-owner'
    assert exchange.writes == []


def test_lost_ack_is_matched_to_original_agent_order_not_second_external_record(managed):
    service, exchange = managed
    ensure_managed(service, SYMBOL)
    exchange.timeout_create = True
    with pytest.raises(ccxt.RequestTimeout):
        service.close(SYMBOL, 'LONG', .01, 'stop_market', trigger_price=2458,
                      reason='Protect exposure', operation_id='unknown-stop')
    assert len([write for write in exchange.writes if write[0] == 'create']) == 1
    ensure_managed(IndependentExits(service.mt), SYMBOL)
    records = plan(service)['exits']
    assert len(records) == 1
    assert records[0]['operation_id'] == 'unknown-stop'
    assert records[0]['id'] == 'managed-1'
    assert records[0].get('source') != 'external'
    assert len([write for write in exchange.writes if write[0] == 'create']) == 1


def test_late_external_entry_fill_during_cleanup_preserves_residual_without_autoclose(managed):
    service, exchange = managed
    exchange.external_order('old-entry', role='entry', price=2385, amount=.01)
    ensure_managed(service, SYMBOL)
    exchange.external_fill(-.02)
    original = exchange.cancel_order

    def cancel_with_fill(order_id, symbol, params=None):
        if str(order_id) == 'old-entry':
            exchange.external_fill(.005, order_id='old-entry')
        return original(order_id, symbol, params)

    exchange.cancel_order = cancel_with_fill
    ensure_managed(service, SYMBOL)
    service.reconcile_all()
    exchange.now += 3000
    service.reconcile_all()
    ensure_managed(service, SYMBOL)
    assert exchange.quantities['LONG'] == .005
    assert plan(service)['state'] == 'ACTIVE'
    assert not [write for write in exchange.writes if write[0] == 'create']


@pytest.mark.parametrize('scope', ['read', 'trade'])
def test_mcp_cancel_scope_and_principal_isolation_survive_enrollment(managed, monkeypatch, scope):
    service, exchange = managed
    from backend.mcp import service as gateway, store
    profile = {'profile_id': 'exclusive-profile', 'enabled': True, 'market_type': 'swap',
               'symbol_scope': 'selected', 'symbols': [SYMBOL], 'exit_mode': 'independent_exits',
               'position_management_policy': 'exclusive_scope', 'management_scopes': [{'symbol': SYMBOL, 'side': 'LONG'}]}
    store.put('profile', 'exclusive-profile', profile)
    monkeypatch.setattr(gateway, 'settings', lambda: {'enabled': True})
    exchange.external_order('tp')
    ensure_managed(service, SYMBOL)
    caller = {'id': 'key:read-only', 'scopes': [scope], 'profile_ids': ['exclusive-profile']}
    result = gateway.execute(caller, 'exclusive-profile', SYMBOL, 'cancel', {'order_id': 'regular:tp', 'reason': 'Cancel protection'}, 'unauthorized-cancel')
    assert result['status'] == 'failed', result
    assert 'cancel' in result.get('message', result.get('error', '')) or 'scope' in str(result)
    assert exchange.writes == []
    with pytest.raises(PermissionError):
        gateway.require({**caller, 'scopes': ['read', 'trade', 'cancel'], 'profile_ids': ['different-profile']}, 'cancel', 'exclusive-profile')


def _configuration_snapshot(service, exchange, enabled=True):
    return {'agents': [{**service.mt.runtime_config, 'exchange_profile_id': 'saved', 'enabled': enabled}],
            'exchange_profiles': [{'profile_id': 'saved', 'market_type': 'swap', 'api_key': exchange.apiKey,
                                   'exchange': 'okx' if exchange.id == 'okx' else 'binance'}]}


def test_paused_decision_retains_scope_and_active_removal_or_key_change_is_rejected(managed):
    service, exchange = managed
    from backend.utils.position_management import synchronize_configuration
    snapshot = _configuration_snapshot(service, exchange)
    with database.get_db_conn() as conn:
        synchronize_configuration(conn, snapshot)
        conn.commit()
    ensure_managed(service, SYMBOL)
    paused = _configuration_snapshot(service, exchange, enabled=False)
    with database.get_db_conn() as conn:
        synchronize_configuration(conn, paused)
        conn.commit()
    assert management_snapshot(service.config_id)['scopes']
    with database.get_db_conn() as conn:
        with pytest.raises(ValueError, match='active'):
            synchronize_configuration(conn, {**paused, 'agents': []})
    changed = deepcopy(paused)
    changed['exchange_profiles'][0]['api_key'] = 'new-credential-identity'
    with database.get_db_conn() as conn:
        with pytest.raises(ValueError, match='active'):
            synchronize_configuration(conn, changed)


def test_unknown_action_without_visible_inventory_prevents_scope_removal(managed):
    service, exchange = managed
    from backend.utils.position_management import synchronize_configuration
    exchange.quantities['LONG'] = 0
    snapshot = _configuration_snapshot(service, exchange)
    with database.get_db_conn() as conn:
        synchronize_configuration(conn, snapshot)
        conn.commit()
    with pytest.raises(ccxt.RequestTimeout):
        with action_reservation(service, SYMBOL, 'LONG', 'unresolved-empty-scope'):
            raise ccxt.RequestTimeout('Possible incoming entry')
    with database.get_db_conn() as conn:
        with pytest.raises(ValueError, match='active|unresolved'):
            synchronize_configuration(conn, {**snapshot, 'agents': []})


def test_same_credentials_under_account_aliases_cannot_create_competing_owner(managed):
    service, exchange = managed
    from backend.utils.position_management import synchronize_configuration
    snapshot = _configuration_snapshot(service, exchange)
    snapshot['exchange_profiles'].append({**snapshot['exchange_profiles'][0], 'profile_id': 'alias'})
    snapshot['agents'].append({**snapshot['agents'][0], 'config_id': 'alias-owner', 'exchange_profile_id': 'alias'})
    with database.get_db_conn() as conn:
        with pytest.raises(ValueError, match='overlapping'):
            synchronize_configuration(conn, snapshot)


def test_oneway_opposite_directions_share_financial_reservation(managed):
    service, exchange = managed
    exchange.hedged = False
    other = make_service(exchange, 'short-owner', 'SHORT')
    def competing_action():
        with pytest.raises(ValueError, match='running'):
            with action_reservation(other, SYMBOL, 'SHORT', 'oneway-short'):
                pytest.fail('Opposite one-way action bypassed BOTH reservation')
    with action_reservation(service, SYMBOL, 'LONG', 'oneway-long'):
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(competing_action).result(timeout=5)


def test_history_budget_exhaustion_eventually_resumes_instead_of_restarting_same_pages(managed):
    service, exchange = managed
    if exchange.id != 'okx':
        return
    ensure_managed(service, SYMBOL)
    # OKX pages are 100; this exceeds one discovery's 20-page call budget.
    for _ in range(1001):
        exchange.external_fill(.0001, advance=1)
    for _ in range(4):
        try:
            ensure_managed(IndependentExits(service.mt), SYMBOL)
        except ValueError as error:
            assert 'page budget' in str(error), str(error)
        else:
            break
    assert plan(service)['management']['sync_status'] == 'verified'
    assert plan(service)['management']['contracts'] == pytest.approx(.1201)
    # A delayed stream/REST fill arrives inside previously covered history.
    exchange.quantities['LONG'] += .0001
    exchange.trades.append({'id': 'late-covered-fill', 'order': 'late-covered-order', 'symbol': SYMBOL,
        'amount': .0001, 'price': 2500, 'cost': .25, 'side': 'buy', 'timestamp': exchange.now - 500,
        'info': {'positionSide': 'LONG'}, 'fee': {'currency': 'USDT', 'cost': 0}})
    exchange.now += 100
    for _ in range(4):
        try:
            ensure_managed(IndependentExits(service.mt), SYMBOL)
        except ValueError as error:
            assert 'page budget' in str(error)
        else:
            break
    assert plan(service)['management']['contracts'] == pytest.approx(.1202)
    assert exchange.writes == []


def test_fixed_exit_never_silently_shrinks_after_manual_reduction(managed):
    service, exchange = managed
    ensure_managed(service, SYMBOL)
    exchange.external_fill(-.01)
    with pytest.raises(ValueError, match='exceeds'):
        service.close(SYMBOL, 'LONG', .02, 'market', operation_id='oversized-fixed')
    assert exchange.quantities['LONG'] == .01
    assert exchange.writes == []


def test_close_all_uses_current_reduced_quantity_with_real_close_only_semantics(managed):
    service, exchange = managed
    exchange.hedged = False
    ensure_managed(service, SYMBOL)
    exchange.external_fill(-.01)
    result = service.close(SYMBOL, 'LONG', None, 'market', close_all=True,
                           contract_version=2, reason='Close current exposure', operation_id='latest-close-all')
    assert result['status'] == 'completed', result
    submitted = [write for write in exchange.writes if write[0] == 'create']
    assert len(submitted) == 1
    assert submitted[0][1] == .01
    assert submitted[0][2]['reduceOnly'] is True
    assert exchange.quantities['LONG'] == 0


def _stats_runtime(monkeypatch, service, exchange):
    from backend.app.services import stats_service
    monkeypatch.setattr(stats_service.global_config, 'get_config_by_id', lambda _: service.mt.runtime_config)
    monkeypatch.setattr(stats_service, 'MarketTool', lambda **_: service.mt)
    monkeypatch.setattr(exchange, 'fetch_ohlcv', lambda *args, **kwargs: [[exchange.now-3600000, 2500, 2565, 2415, 2500, 10]], raising=False)
    return stats_service


def test_stats_local_projection_exposes_sources_and_real_observations_without_discovery(managed, monkeypatch):
    service, exchange = managed
    exchange.external_order('tp')
    ensure_managed(service, SYMBOL)
    stats = _stats_runtime(monkeypatch, service, exchange)
    monkeypatch.setattr(stats, '_fetch_real_position_data', lambda *args: (
        [{'symbol': SYMBOL, 'side': 'LONG', 'amount': .02, 'unrealized_pnl': 0}], 589, [], {}, []))
    from backend.utils import position_management

    def forbidden(*args):
        pytest.fail('Read-only UI projection attempted enrollment/discovery')

    monkeypatch.setattr(position_management, '_discover', forbidden)
    payload = stats.get_position_stats_payload(service.config_id)
    assert payload['management']['policy'] == 'exclusive_scope'
    assert payload['management']['scopes'][0]['owner'] == service.config_id
    assert payload['positions'][0]['amount'] == .02
    assert payload['positions'][0]['source'] == 'baseline'
    assert payload['positions'][0]['managed'] is True
    assert payload['positions'][0]['observed_at'] == plan(service)['management']['observed_at']
    assert payload['exit_management']['exits'][0]['source'] == 'external'
    assert payload['exit_management']['exits'][0]['supported_actions'] == ['cancel', 'edit']
    assert exchange.writes == []


def test_kline_preserves_qualified_same_numeric_id_and_partial_channel_status(managed, monkeypatch):
    service, exchange = managed
    exchange.external_order('7')
    exchange.external_order('7', channel='algo', trigger=2458, price=None)
    ensure_managed(service, SYMBOL)
    stats = _stats_runtime(monkeypatch, service, exchange)
    payload = stats.get_kline_payload(service.config_id)
    rows = {row['order_id']: row for row in payload['pending_orders']}
    assert set(rows) == {'regular:7', 'algo:7'}
    assert rows['regular:7']['source'] == rows['algo:7']['source'] == 'external'
    assert all(row['managed'] and row['observed_at'] for row in rows.values())
    assert payload['management']['scopes'][0]['side'] == 'LONG'
    assert payload['orders_complete'] is True
    exchange.fail_book = 'algo'
    partial = stats.get_kline_payload(service.config_id)
    assert partial['orders_complete'] is False
    assert len(partial['pending_orders']) == 1
    assert partial['pending_orders'][0]['order_id'] == 'regular:7'
    assert partial['errors']
    assert exchange.writes == []


def test_unenrolled_exchange_order_keeps_capability_separate_from_management(managed, monkeypatch):
    service, exchange = managed
    exchange.external_order('not-enrolled', role='entry', price=2385)
    stats = _stats_runtime(monkeypatch, service, exchange)
    from backend.utils import position_management
    monkeypatch.setattr(position_management, '_discover', lambda *args: pytest.fail('Query enrolled exposure'))
    payload = stats.get_kline_payload(service.config_id)
    assert payload['positions'][0]['amount'] == .02
    assert payload['positions'][0]['management_state'] in {'owned', 'eligible_unclaimed'}
    assert payload['pending_orders'][0]['order_id'] == 'regular:not-enrolled'
    assert payload['pending_orders'][0]['management_state'] in {'owned', 'eligible_unclaimed'}
    assert payload['pending_orders'][0]['supported_actions'] == ['cancel', 'edit']
    assert exchange.writes == []


def _account_snapshot(service, exchange, monkeypatch):
    from backend.utils.market_data import MarketTool
    exchange.options = {}
    monkeypatch.setattr(exchange, 'fetch_balance', lambda: {'USDT': {'total': 589, 'free': 100}}, raising=False)
    return MarketTool.get_account_status(service.mt, SYMBOL, is_real=True, config_id=service.config_id)


def test_agent_and_bound_chat_queries_project_resources_without_enrollment(managed, monkeypatch):
    service, exchange = managed
    exchange.external_order('tp', price=2565)
    exchange.external_order('sl', channel='algo', trigger=2458, price=None)
    exchange.external_order('entry', role='entry', price=2385, amount=.01)
    from backend.utils import position_management
    monkeypatch.setattr(position_management, 'ensure_managed', lambda *args: pytest.fail('Query enrolled exposure'))
    monkeypatch.setattr(position_management, '_discover', lambda *args: pytest.fail('Query enrolled exposure'))
    account = _account_snapshot(service, exchange, monkeypatch)
    assert account['real_positions'][0]['amount'] == .02
    assert account['real_positions'][0]['management_state'] in {'owned', 'eligible_unclaimed'}
    assert len(account['real_open_orders']) == 3
    assert all(order['visible'] and order['source'] == 'exchange_snapshot' for order in account['real_open_orders'])
    from backend.utils.trade_context import load_trade_plans, project_trade_state, render_orders, render_positions
    state = project_trade_state(account, config_id=service.config_id, account_scope=service.account_scope,
                                symbol=SYMBOL, plans=load_trade_plans(service.config_id, service.account_scope, SYMBOL), contract_size=1)
    assert state.positions[0]['supported_actions'] == ['close']
    assert all(order['source'] == 'exchange_snapshot' and 'cancel' in order['supported_actions'] for order in state.orders), state.orders
    assert 'edit' in next(order for order in state.orders if order['order_id'] == 'regular:entry')['supported_actions']
    assert '0.02' in render_positions(state)
    assert not state.issues
    if exchange.id == 'okx':
        from backend.utils.position_adoption import OKX_ALGO_ORDER_TYPES
        assert {entry[2] for entry in exchange.reads if isinstance(entry, tuple) and entry[:2] == ('book', 'algo')} == set(OKX_ALGO_ORDER_TYPES)
    assert exchange.writes == []


def test_partial_query_preserves_actual_positions_without_changing_management_scope(managed, monkeypatch):
    service, exchange = managed
    exchange.external_order('entry', role='entry', price=2385, amount=.01)
    exchange.fail_book = 'algo'
    account = _account_snapshot(service, exchange, monkeypatch)
    assert account['real_positions'][0]['amount'] == .02
    assert account['management']['complete'] is False
    assert account['management']['scopes'][0]['sync_status'] == 'pending'
    assert account['orders_complete'] is False
    assert account['real_open_orders'][0]['order_id'] == 'regular:entry'
    assert account['evidence_state']['positions']['observed_at']
    assert account['evidence_state']['trigger_orders']['error']
    assert exchange.writes == []


def test_balance_failure_does_not_suppress_independent_position_reads(managed, monkeypatch):
    service, exchange = managed
    exchange.options = {}
    from backend.utils.market_data import MarketTool

    def balance_failure():
        raise ccxt.RequestTimeout('Balance dependency unavailable')

    monkeypatch.setattr(exchange, 'fetch_balance', balance_failure, raising=False)
    account = MarketTool.get_account_status(service.mt, SYMBOL, is_real=True, config_id=service.config_id)
    assert account['balance'] is None and account['available_balance'] is None
    assert account['evidence_state']['balance']['error']
    assert account['real_positions'][0]['amount'] == .02
    assert account['evidence_state']['positions']['observed_at']
    assert exchange.writes == []


def test_old_unavailable_null_and_tool_call_references_do_not_block_current_state():
    from backend.utils.trade_context import project_trade_state
    account = {'hedged': True, 'real_positions': [{'side': 'LONG', 'amount': .02}], 'real_open_orders': []}
    plan = {'symbol': SYMBOL, 'side': 'LONG', 'account_scope': 'account', 'episode_id': 'active',
            'execution_mode': 'independent_exits', 'state': 'ACTIVE', 'entries': [], 'exits': [],
            'position_observation': {'status': 'verified', 'amount': .02}, 'contract_size': 1}
    historical = {None: {'resolution': 'unresolved', 'affects_current_action': True},
                  'null': {'resolution': 'unresolved', 'affects_current_action': True},
                  'toolu_legacy_edit': {'resolution': 'unavailable', 'affects_current_action': True},
                  'old-rejected-edit': {'resolution': 'unavailable', 'affects_current_action': False}}
    state = project_trade_state(account, config_id='cfg', account_scope='account', symbol=SYMBOL,
                                plans=[plan], referenced_evidence=historical)
    assert state.positions[0]['amount'] == .02
    assert state.issues == []


def test_actual_pending_order_without_operation_reference_remains_current_dependency():
    from backend.utils.trade_context import project_trade_state
    account = {'hedged': True, 'real_positions': [{'side': 'LONG', 'amount': .02}], 'real_open_orders': []}
    plan = {'symbol': SYMBOL, 'side': 'LONG', 'account_scope': 'account', 'episode_id': 'active',
            'execution_mode': 'independent_exits', 'state': 'ACTIVE', 'entries': [],
            'exits': [{'id': None, 'client_id': 'original-submission-identity', 'operation_id': None,
                       'status': 'submitting', 'amount': .02, 'filled': 0, 'order_type': 'stop_market', 'trigger_price': 2458}],
            'position_observation': {'status': 'verified', 'amount': .02}, 'contract_size': 1}
    state = project_trade_state(account, config_id='cfg', account_scope='account', symbol=SYMBOL,
                                plans=[plan], referenced_evidence={})
    assert state.positions[0]['amount'] == .02
    assert any(issue['missing_evidence'] == 'verified_execution_outcome' for issue in state.issues)
    assert all(issue['operation_id'] is None for issue in state.issues)


def test_authoritative_matched_pending_operation_is_not_dropped_by_tool_id_prefix():
    from backend.utils.trade_context import project_trade_state
    account = {'hedged': True, 'real_positions': [], 'real_open_orders': []}
    evidence = {'toolu_saved_operation': {'resolution': 'matched', 'latest': {
        'operation_id': 'toolu_saved_operation', 'status': 'unknown', 'side': 'LONG'}}}
    state = project_trade_state(account, config_id='cfg', account_scope='account', symbol=SYMBOL,
                                plans=[], referenced_evidence=evidence)
    assert any(issue['operation_id'] == 'toolu_saved_operation' and issue['reason_code'] == 'execution_unverified' for issue in state.issues)


def test_discovery_http_deadline_is_bounded_and_restores_outer_budget_and_transport(managed, monkeypatch):
    service, exchange = managed
    from backend.mcp.resilience import Budget, current_budget
    from backend.utils import position_management
    from backend.utils.market_data import _refresh_exclusive_management
    clock, timeouts = {'now': 100000.0}, []
    monkeypatch.setattr(time, 'monotonic', lambda: clock['now'])
    exchange.timeout = 15000

    def original_fetch(*args, **kwargs):
        timeouts.append(exchange.timeout)
        clock['now'] += 2.1
        return {}

    exchange.fetch = original_fetch
    monkeypatch.setattr(position_management, 'ensure_managed', lambda service, symbol: service.ex.fetch('offline-read'))
    monkeypatch.setenv('MCP_MANAGEMENT_DISCOVERY_BUDGET_SECONDS', '5')
    outer = Budget(clock['now'] + 2, 'outer-original-operation', 'execute')
    token = current_budget.set(outer)
    try:
        projection = _refresh_exclusive_management(service.mt, SYMBOL)
        assert projection['complete'] is False
        assert 'budget exhausted' in projection['error']
        assert timeouts == [2000]
        assert exchange.timeout == 15000
        assert exchange.fetch is original_fetch
        assert current_budget.get() is outer
    finally:
        current_budget.reset(token)


def test_failed_stats_balance_and_positions_are_unknown_instead_of_numeric_zero(managed, monkeypatch):
    service, exchange = managed
    stats = _stats_runtime(monkeypatch, service, exchange)
    monkeypatch.setattr(stats, '_fetch_real_position_data', lambda *args: (
        [], 0, [], {}, ['fetch_balance_failed(RequestTimeout)', 'fetch_positions_failed(RequestTimeout)']))
    payload = stats.get_position_stats_payload(service.config_id)
    assert payload['balance'] is None and payload['margin_balance'] is None
    assert payload['unrealized_pnl'] is None
    assert payload['balance_complete'] is False and payload['position_complete'] is False
    assert exchange.writes == []


def test_unexpected_other_market_order_is_not_enrolled_under_current_symbol(managed):
    service, exchange = managed
    foreign = exchange.external_order('foreign-market', role='entry')
    foreign['symbol'] = 'BTC/USDT:USDT'
    try:
        ensure_managed(service, SYMBOL)
    except ValueError as error:
        assert 'symbol' in str(error).lower() or 'scope' in str(error).lower()
    current = plan(service)
    if current:
        assert not [record for record in current['entries'] + current['exits'] if record.get('id') == 'foreign-market']
    assert exchange.writes == []


def test_chart_and_account_current_quantities_use_base_units_and_all_is_not_zero(managed, monkeypatch):
    service, exchange = managed
    exchange.contract_size = .01
    exchange.hedged = False
    entry = exchange.external_order('entry', role='entry', amount=.02, price=2385)
    entry['remaining'] = .02
    exchange.external_order('all-stop', channel='algo', dynamic=True, trigger=2458, price=None)
    ensure_managed(service, SYMBOL)
    stats = _stats_runtime(monkeypatch, service, exchange)
    payload = stats.get_kline_payload(service.config_id)
    assert payload['positions'][0]['amount'] == pytest.approx(.0002)
    rows = {row['order_id']: row for row in payload['pending_orders']}
    assert rows['regular:entry']['amount'] == pytest.approx(.0002)
    assert rows['regular:entry']['remaining'] == pytest.approx(.0002)
    assert rows['algo:all-stop']['quantity_mode'] == 'all'
    assert rows['algo:all-stop']['amount'] is None
    assert rows['algo:all-stop']['coverage_estimate'] == pytest.approx(.0002)
    account = _account_snapshot(service, exchange, monkeypatch)
    assert account['real_positions'][0]['amount'] == pytest.approx(.0002)
    assert exchange.writes == []
