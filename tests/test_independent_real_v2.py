"""Offline lifecycle coverage with distinct regular and algo exchange books."""
from copy import deepcopy
from types import SimpleNamespace

import ccxt
import pytest

from backend import database
from backend.database_schema import initialize_schema
from backend.utils.independent_exits import IndependentExits

SYMBOL = 'ETH/USDT:USDT'


def entry(amount=1, side='LONG', price=100):
    return SimpleNamespace(action='BUY_LIMIT' if side == 'LONG' else 'SELL_LIMIT',
                           amount=amount, entry_price=price, stop_loss=None, take_profit=None, reason='entry')


class Exchange:
    id = 'binanceusdm'
    apiKey = 'offline-v2-only'

    def __init__(self):
        self.regular, self.algo, self.calls, self.trades = {}, {}, [], []
        self.positions = {'LONG': 0., 'SHORT': 0.}
        self.hedged, self.contract_size = True, 1
        self.timeout_next = False
        self.hidden = False
        self.before_cancel = None
        self.before_edit = None

    def load_markets(self):
        pass

    def market(self, symbol):
        return dict(symbol=SYMBOL, contract=True, swap=True, linear=True, contractSize=self.contract_size,
                    limits={'amount': {'min': .0001}})

    def price_to_precision(self, symbol, value):
        return str(round(value, 2))

    def amount_to_precision(self, symbol, value):
        return str(round(value, 4))

    def fetch_position_mode(self, symbol):
        return {'hedged': self.hedged}

    def fetch_positions(self, symbols):
        return [dict(symbol=SYMBOL, side=side.lower(), contracts=quantity, hedged=self.hedged)
                for side, quantity in self.positions.items() if quantity]

    def fetch_my_trades(self, symbol, since=None, limit=None):
        return deepcopy(self.trades)

    def fetch_open_orders(self, symbol, params=None):
        book = self.algo if (params or {}).get('trigger') else self.regular
        return [deepcopy(row) for row in book.values() if row['status'] == 'open']

    def fetch_ticker(self, symbol):
        return {'last': 100}

    def create_order(self, symbol, order_type, side, amount, price, params):
        conditional = 'stopLossPrice' in params or 'takeProfitPrice' in params
        book = self.algo if conditional else self.regular
        identity = str(len(book) + 1)
        row = dict(id=identity, symbol=SYMBOL, type=order_type, side=side, amount=amount,
                   price=price, filled=None if conditional else 0, status='open',
                   clientOrderId=params['clientOrderId'], params=deepcopy(params),
                   reduceOnly=params.get('reduceOnly', False), info={})
        if conditional:
            row['info'] = {'algoId': identity, 'algoStatus': 'NEW', 'actualOrderId': '0'}
        book[identity] = row
        self.calls.append(('create', 'algo' if conditional else 'regular', deepcopy(row)))
        if not conditional and order_type == 'market':
            self.fill(identity, amount)
        if self.timeout_next:
            self.timeout_next = False
            raise ccxt.RequestTimeout('offline lost ACK')
        return deepcopy(row)

    def fetch_order(self, identity, symbol, params=None):
        params = params or {}
        book = self.algo if params.get('trigger') else self.regular
        if self.hidden:
            raise ccxt.OrderNotFound('offline hidden')
        client = params.get('clientAlgoId') or params.get('clientOrderId')
        for row in book.values():
            if (identity is not None and str(identity) == row['id']) or (client and client == row['clientOrderId']):
                return deepcopy(row)
        raise ccxt.OrderNotFound('offline missing')

    def cancel_order(self, identity, symbol, params=None):
        conditional = bool((params or {}).get('trigger'))
        if self.before_cancel:
            callback, self.before_cancel = self.before_cancel, None
            callback(identity, conditional)
        row = (self.algo if conditional else self.regular)[str(identity)]
        self.calls.append(('cancel', 'algo' if conditional else 'regular', str(identity)))
        if conditional and row.get('info', {}).get('actualOrderId') not in {None, '0'}:
            return deepcopy(row)
        row['status'] = 'canceled'
        if conditional:
            row['info']['algoStatus'] = 'CANCELED'
            row['filled'] = 0
        return deepcopy(row)

    def edit_order(self, identity, symbol, order_type, side, amount, price):
        if self.before_edit:
            callback, self.before_edit = self.before_edit, None
            callback(identity)
        row = self.regular[str(identity)]
        row.update(amount=amount, price=price)
        self.calls.append(('edit', 'regular', deepcopy(row)))
        return deepcopy(row)

    def fill(self, identity, quantity):
        row = self.regular[str(identity)]
        params = row['params']
        side = params.get('positionSide')
        if side == 'BOTH':
            side = 'LONG' if (row['side'] == 'buy') != bool(params.get('reduceOnly')) else 'SHORT'
        opening = (side == 'LONG' and row['side'] == 'buy') or (side == 'SHORT' and row['side'] == 'sell')
        quantity = quantity if opening else min(quantity, self.positions[side])
        row['filled'] += quantity
        self.positions[side] += quantity if opening else -quantity
        row['status'] = 'closed' if row['filled'] + 1e-12 >= row['amount'] else 'open'
        self.trades.append(dict(id=str(len(self.trades) + 1), order=row['id'], amount=quantity))

    def trigger(self, identity, fill=False):
        parent = self.algo[str(identity)]
        side = parent['params']['positionSide']
        quantity = self.positions[side] if parent['amount'] is None else parent['amount']
        child_id = str(len(self.regular) + 1)
        child = deepcopy(parent)
        child.update(id=child_id, amount=quantity, filled=0, status='open', info={})
        self.regular[child_id] = child
        parent['info'].update(algoStatus='FINISHED', actualOrderId=child_id)
        parent['status'] = 'closed'
        if fill:
            self.fill(child_id, quantity)
        return child_id


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setattr(database, 'DB_NAME', str(tmp_path / 'v2-real.db'))
    with database.get_db_conn() as conn:
        initialize_schema(conn)
    exchange = Exchange()
    service = IndependentExits(SimpleNamespace(exchange=exchange, config_id='cfg'))
    opened = service.open(SYMBOL, entry(), operation_id='entry', contract_version=2)
    exchange.fill(opened['id'], 1)
    service.reconcile_all()
    return service, exchange


def close(service, kind='limit', amount=.2, **kwargs):
    return service.close(SYMBOL, 'LONG', amount, kind, contract_version=2, **kwargs)


def test_fixed_stages_dynamic_remainder_and_addition(service):
    svc, ex = service
    first = close(svc, price=110)
    second = close(svc, amount=.3, price=120)
    final = close(svc, 'take_profit_market', None, trigger_price=130, close_all=True, operation_id='final')
    assert final['order_id'].startswith('algo:') and final['amount'] is None
    assert final['remaining_amount'] is None and final['order_status'] == 'waiting_trigger'
    assert not final['pending'] and ex.algo[final['id']]['params']['closePosition']
    add = svc.open(SYMBOL, entry(.4), contract_version=2)
    ex.fill(add['id'], .4)
    svc.reconcile_all()
    ex.fill(first['id'], .2)
    ex.fill(second['id'], .3)
    svc.reconcile_all()
    child = ex.trigger(final['id'], fill=True)
    assert ex.regular[child]['filled'] == pytest.approx(.9)
    svc.reconcile_all()
    assert svc._load(SYMBOL, 'LONG')['state'] == 'DONE'
    replay = svc.close(SYMBOL, 'LONG', None, 'take_profit_market', trigger_price=130,
                       close_all=True, operation_id='final', contract_version=2)
    assert replay['filled_amount'] == pytest.approx(.9)
    assert replay['amount'] is None and replay['remaining'] is None
    assert replay['actual_remaining'] == 0 and replay['resolved_amount'] == pytest.approx(.9)
    assert ex.positions['LONG'] == 0


def test_overlapping_fixed_quantities_survive_reduction(service):
    svc, ex = service
    close(svc, 'stop_market', .7, trigger_price=90)
    close(svc, 'stop_market', .6, trigger_price=80)
    count = len(ex.calls)
    close(svc, 'market', .4)
    state = svc.snapshot(SYMBOL)
    assert sorted(row['remaining'] for row in state['exits']) == [.6, .7]
    assert state['quantity_conflict'] and not state['pending']
    assert len(ex.calls) == count + 1
    with pytest.raises(ValueError, match='exceeds'):
        close(svc, amount=.7, price=110)


def test_native_edit_remaining_quantity_accounts_for_racing_fills(service):
    svc, ex = service
    added = svc.open(SYMBOL, entry(1), contract_version=2)
    ex.fill(added['id'], .2)
    ex.before_edit = lambda identity: ex.fill(identity, .1)
    edited = svc.edit(SYMBOL, added['order_id'], remaining_amount=.5, price=99, reason='resize', operation_id='edit')
    assert edited['edit_method'] == 'native'
    assert edited['amount'] == pytest.approx(.7) and edited['remaining_amount'] == pytest.approx(.4)
    count = len(ex.calls)
    svc.edit(SYMBOL, added['order_id'], remaining_amount=.5, price=99, reason='resize', operation_id='edit')
    assert len(ex.calls) == count


def test_algo_and_regular_id_collision_and_parent_child_edit_cancel(service):
    svc, ex = service
    conditional = close(svc, 'stop_limit', .5, price=89, trigger_price=90)
    # Entry regular:1 and algo:1 are both managed, so bare 1 is ambiguous.
    with pytest.raises(ValueError, match='Ambiguous'):
        svc.describe_order(SYMBOL, conditional['id'])
    child = ex.trigger(conditional['id'])
    result = svc.edit(SYMBOL, conditional['order_id'], trigger_price=91, reason='move')
    assert result['error_code'] == 'order_already_triggered'
    assert result['order_id'] == 'regular:' + child
    assert result['order_status'] == 'triggered' and result['filled_amount'] == 0
    with pytest.raises(ValueError, match='trigger_price'):
        svc.edit(SYMBOL, result['order_id'], trigger_price=91, reason='invalid')
    edited = svc.edit(SYMBOL, result['order_id'], price=88, remaining_amount=.4, reason='child edit')
    assert edited['edit_method'] == 'native' and ex.regular[child]['amount'] == .4
    ex.fill(child, .1)
    canceled = svc.cancel(SYMBOL, conditional['order_id'])
    assert canceled['filled_amount'] == .1 and canceled['actual_remaining'] == pytest.approx(.3)
    assert ex.regular[child]['status'] == 'canceled'


def test_cancel_parent_follows_trigger_race_but_edit_does_not(service):
    svc, ex = service
    conditional = close(svc, 'stop_limit', .5, price=89, trigger_price=90)
    ex.before_cancel = lambda identity, _: ex.trigger(identity)
    edited = svc.edit(SYMBOL, conditional['order_id'], trigger_price=92, reason='move', operation_id='trigger-race')
    assert edited['error_code'] == 'order_already_triggered'
    assert ex.regular[edited['exchange_order_id']]['status'] == 'open'
    count = len(ex.calls)
    replay = IndependentExits(svc.mt).edit(SYMBOL, conditional['order_id'], trigger_price=92,
                                         reason='move', operation_id='trigger-race')
    assert replay['status'] == 'failed' and replay['error_code'] == 'order_already_triggered'
    assert len(ex.calls) == count
    svc.cancel(SYMBOL, conditional['order_id'])
    assert ex.regular[edited['exchange_order_id']]['status'] == 'canceled'


def test_all_edit_retains_dynamic_quantity_and_timeout_recovers(service):
    svc, ex = service
    original = close(svc, 'stop_market', None, trigger_price=90, close_all=True)
    ex.timeout_next = True
    with pytest.raises(ccxt.RequestTimeout):
        svc.edit(SYMBOL, original['order_id'], trigger_price=91, reason='revise', operation_id='revise')
    count = len(ex.calls)
    restarted = IndependentExits(svc.mt)
    restarted.reconcile_all()
    result = restarted.edit(SYMBOL, original['order_id'], trigger_price=91, reason='revise', operation_id='revise')
    assert len(ex.calls) == count
    assert result['quantity_mode'] == 'all' and result['amount'] is None
    assert result['order_id'] != original['order_id'] and result['edit_method'] == 'cancel_replace'


def test_one_way_rejects_opposite_pending_and_hedge_all_is_directional(service):
    svc, ex = service
    ex.hedged = False
    with pytest.raises(ValueError, match='opposite exposure'):
        svc.open(SYMBOL, entry(side='SHORT'), contract_version=2)
    close(svc, 'market', 1)
    svc.open(SYMBOL, entry(), contract_version=2)
    with pytest.raises(ValueError, match='Opposite entry'):
        svc.open(SYMBOL, entry(side='SHORT'), contract_version=2)


@pytest.mark.parametrize('kind', ['limit', 'stop_limit', 'take_profit_limit'])
def test_unsupported_all_is_preflight_only(service, kind):
    svc, ex = service
    count = len(ex.calls)
    with pytest.raises(ValueError, match='unsupported_combination'):
        close(svc, kind, None, close_all=True, price=110, trigger_price=120)
    assert len(ex.calls) == count


def test_legacy_zero_snapshot_does_not_become_dynamic_on_replay(service):
    svc, ex = service
    legacy = svc.close(SYMBOL, 'LONG', 0, 'take_profit_limit', price=110, operation_id='legacy')
    add = svc.open(SYMBOL, entry(.4))
    ex.fill(add['id'], .4)
    svc.reconcile_all()
    restarted = IndependentExits(svc.mt)
    replay = restarted.close(SYMBOL, 'LONG', 0, 'take_profit_limit', price=110, operation_id='legacy')
    assert replay['amount'] == 1 and replay['quantity_mode'] == 'fixed'
    assert replay['order_type'] == 'limit' and replay['order_id'].startswith('regular:')
    assert len(ex.algo) == 0


def test_unknown_child_never_reports_fill_or_resubmits(service):
    svc, ex = service
    final = close(svc, 'take_profit_market', None, trigger_price=120, close_all=True)
    ex.algo[final['id']]['info'].update(algoStatus='FINISHED', actualOrderId='0')
    count = len(ex.calls)
    assert svc.reconcile_all()[0]['error']
    assert len(ex.calls) == count and svc.snapshot(SYMBOL)['pending']
    observed = svc.snapshot(SYMBOL)['exits'][0]
    assert observed['order_status'] == 'triggered' and observed['filled_amount'] is None
    assert observed['amount'] is None and observed['actual_remaining'] is None


def test_legacy_pending_resize_resumes_once_without_new_resize(service):
    svc, ex = service
    stop = svc.close(SYMBOL, 'LONG', .7, 'stop_market', trigger_price=90)
    plan, record = svc._find_order(SYMBOL, stop['order_id'])
    record['replacement'] = dict(remaining=.4, original_filled=0, price=None, trigger_price=90,
                                 reason='position reduced; proportional quantity correction',
                                 operation_id='old-resize', client_id=svc._client_id('old-resize'))
    svc._save(plan)
    restarted = IndependentExits(svc.mt)
    restarted.reconcile_all()
    count = len(ex.calls)
    restarted.reconcile_all()
    assert len(ex.calls) == count
    assert restarted.snapshot(SYMBOL)['exits'][0]['remaining'] == .4


def test_all_is_directional_and_contract_size_results_are_base_units(service):
    svc, ex = service
    # Start a new cycle in a contract market where one contract means 10 base.
    close(svc, 'market', 1)
    ex.contract_size = 10
    long_entry = svc.open(SYMBOL, entry(10), contract_version=2)
    short_entry = svc.open(SYMBOL, entry(5, side='SHORT'), contract_version=2)
    assert long_entry['amount'] == 10 and short_entry['amount'] == 5
    assert svc._load(SYMBOL, 'LONG')['contract_size'] == 10
    assert svc._load(SYMBOL, 'SHORT')['contract_size'] == 10
    ex.fill(long_entry['id'], 1)
    ex.fill(short_entry['id'], .5)
    svc.reconcile_all()
    partial = close(svc, 'market', 2)
    assert partial['filled_amount'] == 2 and ex.positions['LONG'] == .8
    final = close(svc, 'take_profit_market', None, trigger_price=120, close_all=True)
    state = svc.snapshot(SYMBOL)
    assert state['exits'][0]['coverage_estimate'] == 8
    ex.trigger(final['id'], fill=True)
    svc.reconcile_all()
    receipt = svc.describe_order(SYMBOL, final['order_id'])
    assert receipt['resolved_amount'] == 8 and receipt['filled_amount'] == 8
    assert receipt['amount'] is None and receipt['remaining'] is None
    assert receipt['actual_remaining'] == 0
    assert ex.positions['SHORT'] == .5 and svc._load(SYMBOL, 'SHORT')['state'] == 'ACTIVE'


def test_all_fill_reconciles_child_ownership_before_cleaning_other_all(service):
    svc, ex = service
    other = svc.open(SYMBOL, entry(.5, side='SHORT'), contract_version=2)
    ex.fill(other['id'], .5)
    svc.reconcile_all()
    stop = close(svc, 'stop_market', None, trigger_price=90, close_all=True)
    profit = close(svc, 'take_profit_market', None, trigger_price=120, close_all=True)
    ex.trigger(profit['id'], fill=True)
    svc.reconcile_all()
    assert svc._load(SYMBOL, 'LONG')['state'] == 'DONE'
    assert ex.algo[stop['id']]['status'] == 'canceled'
    assert ex.positions['SHORT'] == .5 and svc._load(SYMBOL, 'SHORT')['state'] == 'ACTIVE'


def test_legacy_resize_successor_lost_ack_recovers_by_client_id(service):
    svc, ex = service
    stop = svc.close(SYMBOL, 'LONG', .7, 'stop_market', trigger_price=90)
    plan, record = svc._find_order(SYMBOL, stop['order_id'])
    record['replacement'] = dict(remaining=.4, original_filled=0, price=None, trigger_price=90,
                                 reason='position reduced; proportional quantity correction',
                                 operation_id='old-resize-lost', client_id=svc._client_id('old-resize-lost'))
    svc._save(plan)
    ex.timeout_next = True
    assert IndependentExits(svc.mt).reconcile_all()[0]['error']
    count = len(ex.calls)
    restarted = IndependentExits(svc.mt)
    restarted.reconcile_all()
    restarted.reconcile_all()
    assert len(ex.calls) == count and len(ex.algo) == 2
    current = restarted.snapshot(SYMBOL)['exits'][0]
    assert current['remaining'] == .4 and not current['pending']


def test_market_all_is_snapshot_and_does_not_flatten_late_entry(service, monkeypatch):
    svc, ex = service
    pending = svc.open(SYMBOL, entry(.1), contract_version=2)
    original = ex.create_order

    def create(*args, **kwargs):
        result = original(*args, **kwargs)
        if args[1] == 'market':
            ex.fill(pending['id'], .1)
        return result

    monkeypatch.setattr(ex, 'create_order', create)
    result = close(svc, 'market', None, close_all=True)
    assert result['quantity_mode'] == 'all'
    assert result['amount'] is None and result['remaining'] is None
    assert result['resolved_amount'] == 1 and result['filled_amount'] == 1
    assert ex.positions['LONG'] == .1 and svc._load(SYMBOL, 'LONG')['state'] == 'ACTIVE'
    assert sum(row[0] == 'create' and row[2]['type'] == 'market' for row in ex.calls) == 1


def test_native_all_refuses_untracked_quantity_before_submission(service):
    svc, ex = service
    ex.positions['LONG'] += .2
    count = len(ex.calls)
    with pytest.raises(ValueError, match='solely owned fills'):
        close(svc, 'stop_market', None, trigger_price=90, close_all=True)
    assert len(ex.calls) == count


@pytest.mark.parametrize('all_quantity', [False, True])
def test_exchange_rejects_competing_exit_without_clearing_existing(service, monkeypatch, all_quantity):
    svc, ex = service
    existing = close(svc, 'stop_market', .7, trigger_price=90)
    writes = []

    def reject(*args, **kwargs):
        writes.append((args, kwargs))
        raise ccxt.InvalidOrder('Exchange competing close-position order rejected')

    count = len(ex.calls)
    monkeypatch.setattr(ex, 'create_order', reject)
    with pytest.raises(ValueError, match='Exchange rejected order:.*competing'):
        close(svc, 'stop_market', None if all_quantity else .6,
              trigger_price=80, close_all=all_quantity, operation_id='rejected-close')
    assert len(writes) == 1 and len(ex.calls) == count
    assert ex.algo[existing['id']]['status'] == 'open'
    restarted = IndependentExits(svc.mt)
    replay = close(restarted, 'stop_market', None if all_quantity else .6,
                   trigger_price=80, close_all=all_quantity, operation_id='rejected-close')
    assert replay['status'] == 'failed' and len(writes) == 1
    assert len(ex.calls) == count


def test_unsupported_dynamic_all_adapter_rejects_before_order_write(service):
    svc, ex = service
    # This adapter is valid for independent trading, but does not implement
    # native conditional all in Hedge mode.
    ex.id = 'okx'
    count = len(ex.calls)
    with pytest.raises(ValueError, match='unsupported_combination'):
        close(svc, 'stop_market', None, trigger_price=90, close_all=True)
    assert len(ex.calls) == count and len(ex.algo) == 0


def test_regular_marketable_limit_does_not_have_tp_price_restriction(service):
    svc, ex = service
    result = close(svc, 'limit', .2, price=99)
    assert result['order_type'] == 'limit' and result['order_id'].startswith('regular:')
    assert ex.regular[result['id']]['price'] == 99


def test_native_edit_rejection_replay_is_not_success(service, monkeypatch):
    svc, ex = service
    pending = close(svc, 'limit', .2, price=110)

    def reject(*args, **kwargs):
        raise ccxt.InvalidOrder('native amendment rejected')

    monkeypatch.setattr(ex, 'edit_order', reject)
    with pytest.raises(ccxt.InvalidOrder):
        svc.edit(SYMBOL, pending['order_id'], price=120, reason='move', operation_id='rejected-edit')
    replay = svc.edit(SYMBOL, pending['order_id'], price=120, reason='move', operation_id='rejected-edit')
    assert replay['status'] == 'failed' and replay['order_status'] == 'open'


def test_conditional_replacement_conflict_does_not_resize_or_block_cycle(service):
    svc, ex = service
    stop = close(svc, 'stop_market', .7, trigger_price=90)
    market_exit = close(svc, 'limit', .6, price=110)
    ex.before_cancel = lambda identity, conditional: ex.fill(market_exit['id'], .6)
    result = svc.edit(SYMBOL, stop['order_id'], trigger_price=91, reason='move', operation_id='conflict')
    assert result['status'] == 'failed' and result['error_code'] == 'quantity_conflict'
    assert not result['pending'] and len(ex.algo) == 1
    assert svc._load(SYMBOL, 'LONG')['error'] is None
    count = len(ex.calls)
    replay = IndependentExits(svc.mt).edit(SYMBOL, stop['order_id'], trigger_price=91,
                                         reason='move', operation_id='conflict')
    assert replay['status'] == 'failed' and replay['error_code'] == 'quantity_conflict'
    assert len(ex.calls) == count
    # The agent can now choose a new explicit quantity; no synthetic resize occurs.
    next_stop = close(svc, 'stop_market', .4, trigger_price=91)
    assert next_stop['amount'] == .4
