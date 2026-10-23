"""Current-position adjustments move cash atomically without changing exposure."""
from copy import deepcopy
import time

import pytest
from pydantic import ValidationError

from test_mcp_paper import create, ledger
from backend.mcp import paper, paper_lifecycle as lifecycle, paper_orders as orders, paper_positions as positions, store
from backend.storage import lock_scope, try_lock_scope


SYMBOL = 'BTC/USDT:USDT'
USER = '123456'


def _open(caller, *, side='LONG', protection=False):
    create(caller)
    result = orders.execute_market(
        caller, USER, 'swap', SYMBOL, 'open_' + side.lower(), 'position-open',
        amount=10, leverage=10,
        **({'stop_loss': 90, 'take_profit': 120} if protection else {}),
    )
    return result['account']['positions'][0]


def _manage(caller, position, action, operation_id, **kwargs):
    return positions.manage(caller, USER, position['resource_id'], operation_id, action, **kwargs)


def _raw():
    with store.connection() as conn:
        return deepcopy(paper._get(conn, 'qqbot', USER))


def _change_raw(change):
    with store.connection() as conn:
        lock_scope(conn, 'paper-account', 'qqbot', USER)
        account = paper._get(conn, 'qqbot', USER)
        change(account)
        paper._save(conn, account)
        conn.commit()


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
def test_leverage_moves_only_base_margin_preserving_manual_margin_and_position(ledger, side):
    caller, _, _ = ledger
    original = _open(caller, side=side)
    before = paper.get_account(caller, USER)
    first = _manage(caller, original, 'add_margin', 'position-add-manual', amount=20)
    assert first['margin_after'] == 120
    assert first['added_margin'] == 20
    assert first['cash_change'] == -20
    higher = _manage(caller, original, 'set_leverage', 'position-higher-lev', leverage=20)
    assert (higher['leverage_before'], higher['leverage_after']) == (10, 20)
    assert (higher['margin_before'], higher['margin_after'], higher['base_margin']) == (120, 70, 50)
    assert (higher['cash_change'], higher['added_margin'], higher['available_cash']) == (50, 20, 929)
    lower = _manage(caller, original, 'set_leverage', 'position-lower-lev', leverage=5)
    assert (lower['margin_before'], lower['margin_after'], lower['cash_change']) == (70, 220, -150)
    assert lower['position_id'] == lower['resource_id'] == original['resource_id']
    assert lower['currency'] == 'USDT'
    for key in ('amount', 'entry_price', 'resource_id', 'cycle_id', 'symbol', 'side'):
        assert lower['position'][key] == original[key]
    assert lower['account']['leverage'] == before['leverage'] == 1
    assert lower['account']['fees'] == before['fees'] == 1
    assert lower['account']['net_equity'] == before['net_equity'] == 999
    assert lower['account']['realized_pnl'] == before['realized_pnl'] == 0
    events = paper.history(caller, USER)
    assert {event['kind'] for event in events} >= {'add_margin', 'set_leverage'}


def test_adjustments_leave_protection_orders_unchanged_and_still_executable(ledger):
    caller, ticker, _ = ledger
    original = _open(caller, protection=True)
    protection = orders.get_orders(caller, USER)['orders']
    _manage(caller, original, 'add_margin', 'protection-add-margin', amount=50)
    _manage(caller, original, 'set_leverage', 'protection-change-lev', leverage=20)
    assert orders.get_orders(caller, USER)['orders'] == protection
    ticker.update(price=120, bid=120, ask=120)
    paper.reconcile()
    assert paper.get_account(caller, USER)['positions'] == []
    assert len(orders.get_orders(caller, USER, 'filled')['orders']) == 1


def test_manual_reduction_releases_cash_and_cannot_reduce_leverage_base(ledger):
    caller, _, _ = ledger
    original = _open(caller)
    _manage(caller, original, 'add_margin', 'manual-add-20', amount=20)
    result = _manage(caller, original, 'reduce_margin', 'manual-reduce-15', amount=15)
    assert (result['margin_after'], result['added_margin'], result['cash_change']) == (105, 5, 15)
    before = _raw()
    with pytest.raises(ValueError, match='base'):
        _manage(caller, original, 'reduce_margin', 'manual-reduce-too-much', amount=5.00000001)
    assert _raw() == before
    result = _manage(caller, original, 'reduce_margin', 'manual-reduce-rest', amount=5)
    assert (result['margin_after'], result['added_margin']) == (100, 0)


@pytest.mark.parametrize('action,kwargs', [('add_margin', {'amount': 900}), ('set_leverage', {'leverage': 1})])
def test_insufficient_cash_rejects_without_changing_position_or_cash(ledger, action, kwargs):
    caller, _, _ = ledger
    original = _open(caller)
    before = _raw()
    with pytest.raises(ValueError, match='Insufficient unreserved cash'):
        _manage(caller, original, action, 'position-insufficient', **kwargs)
    assert _raw() == before
    assert len(paper.history(caller, USER)) == 2


def test_entry_reservations_cannot_fund_added_margin_but_refunds_can_cover_a_deficit(ledger):
    caller, _, _ = ledger
    original = _open(caller)
    pending = orders.place(caller, USER, 'spot', 'ETH/USDT', 'buy', 'limit',
                           'reserve-for-entry', quote_amount=890, price=50)['order']
    before = _raw()
    with pytest.raises(ValueError, match='Insufficient unreserved cash'):
        _manage(caller, original, 'add_margin', 'reserve-add-rejected', amount=10)
    assert _raw() == before
    paper.admin_update('qqbot', USER, paper.AccountUpdate(cash=880))
    result = _manage(caller, original, 'set_leverage', 'reserve-refund-safe', leverage=20)
    assert result['cash_change'] == 50
    assert result['available_cash'] == 40
    assert result['account']['reserved_cash'] == 890
    assert orders.get_orders(caller, USER)['orders'] == [pending]


@pytest.mark.parametrize('side,mark', [('LONG', 94), ('SHORT', 106)])
def test_fresh_mark_prevents_immediate_liquidation_after_leverage_increase(ledger, side, mark):
    caller, ticker, _ = ledger
    original = _open(caller, side=side)
    ticker.update(price=mark, bid=mark, ask=mark)
    before = _raw()
    with pytest.raises(ValueError, match='immediately trigger liquidation'):
        _manage(caller, original, 'set_leverage', 'dangerous-leverage', leverage=20)
    assert _raw() == before
    result = _manage(caller, original, 'add_margin', 'safe-margin-add', amount=25)
    assert result['position']['unrealized_pnl'] == -60
    assert result['position']['margin_equity'] == 65


def test_reducing_manual_margin_must_remain_above_current_maintenance(ledger):
    caller, ticker, _ = ledger
    original = _open(caller)
    _manage(caller, original, 'add_margin', 'safety-extra-margin', amount=50)
    ticker.update(price=90, bid=90, ask=90)
    before = _raw()
    with pytest.raises(ValueError, match='immediately trigger liquidation'):
        _manage(caller, original, 'reduce_margin', 'safety-remove-all', amount=50)
    assert _raw() == before
    result = _manage(caller, original, 'reduce_margin', 'safety-remove-part', amount=40)
    assert result['position']['margin_equity'] == 10
    assert result['added_margin'] == 10


def test_unchanged_leverage_and_legacy_manual_margin_default_are_safe(ledger):
    caller, _, _ = ledger
    original = _open(caller)
    _change_raw(lambda account: next(iter(account['positions'].values())).pop('added_margin'))
    before = paper.get_account(caller, USER)
    result = _manage(caller, original, 'set_leverage', 'same-position-leverage', leverage=10)
    assert result['cash_change'] == result['added_margin'] == 0
    assert result['account']['cash'] == before['cash']
    assert result['margin_after'] == result['margin_before'] == 100


def test_policy_cap_does_not_stop_adding_margin_to_existing_high_leverage_position(ledger):
    caller, _, _ = ledger
    original = _open(caller)
    store.put('paper_settings', 'global', {'initial_balance': 1000, 'max_leverage': 5})
    result = _manage(caller, original, 'add_margin', 'old-position-add', amount=5)
    assert result['leverage_after'] == 10
    assert result['margin_after'] == 105
    with pytest.raises(ValueError, match='maximum'):
        _manage(caller, original, 'set_leverage', 'over-new-policy', leverage=20)
    result = _manage(caller, original, 'set_leverage', 'meet-new-policy', leverage=5)
    assert result['margin_after'] == 205


def test_same_operation_replays_without_quote_and_changed_arguments_are_rejected(ledger, monkeypatch):
    caller, _, _ = ledger
    original = _open(caller)
    result = _manage(caller, original, 'add_margin', 'adjustment-retry', amount=20)
    monkeypatch.setattr(paper, 'market', lambda *args, **kwargs: pytest.fail('Receipt retries cannot fetch quotes'))
    assert _manage(caller, original, 'add_margin', 'adjustment-retry', amount=20) == result
    with pytest.raises(ValueError, match='different'):
        _manage(caller, original, 'add_margin', 'adjustment-retry', amount=21)
    assert len([event for event in paper.history(caller, USER) if event['kind'] == 'add_margin']) == 1


@pytest.mark.parametrize('fields', [
    {'action': 'set_leverage'}, {'action': 'set_leverage', 'leverage': True},
    {'action': 'set_leverage', 'leverage': 2.5}, {'action': 'set_leverage', 'leverage': 0},
    {'action': 'set_leverage', 'leverage': 126}, {'action': 'set_leverage', 'leverage': 5, 'amount': 1},
    {'action': 'add_margin'}, {'action': 'add_margin', 'amount': True},
    {'action': 'add_margin', 'amount': 0}, {'action': 'add_margin', 'amount': float('nan')},
    {'action': 'reduce_margin', 'amount': 1e-9}, {'action': 'reduce_margin', 'amount': 1, 'leverage': 5},
    {'action': 'unknown', 'amount': 1}, {'action': 'add_margin', 'amount': 1, 'unexpected': True},
])
def test_adjustment_payload_rejects_ambiguous_or_invalid_parameters(fields):
    with pytest.raises(ValidationError):
        positions.PositionAdjustment(operation_id='invalid-payload', **fields)


def test_position_is_scoped_to_current_user_namespace_and_trade_permission(ledger):
    caller, _, _ = ledger
    original = _open(caller)
    before = _raw()
    with pytest.raises(PermissionError, match='trade'):
        _manage({**caller, 'scopes': ['read']}, original, 'add_margin', 'adjust-read-only', amount=1)
    create(caller, 'other-user')
    with pytest.raises(ValueError, match='position_id'):
        positions.manage(caller, 'other-user', original['resource_id'], 'adjust-other-user', 'add_margin', amount=1)
    other = {**caller, 'paper_namespace': 'another-bot'}
    create(other)
    with pytest.raises(ValueError, match='position_id'):
        _manage(other, original, 'add_margin', 'adjust-other-bot', amount=1)
    assert _raw() == before


@pytest.mark.parametrize('untrusted', ['foreign', 'unverified', 'missing_cycle'])
def test_only_trusted_current_binance_positions_can_be_adjusted(ledger, untrusted):
    caller, _, calls = ledger
    original = _open(caller)
    def untrust(account):
        position = next(iter(account['positions'].values()))
        if untrusted == 'foreign':
            position.update(exchange='okx', origin_exchange='okx')
        elif untrusted == 'unverified':
            position.update(resource_id='unknown-resource', cycle_id='unknown-cycle')
            for key in ('origin_version', 'origin_exchange', 'origin_operation_id'):
                position.pop(key, None)
        else:
            position.pop('cycle_id')
    _change_raw(untrust)
    target = 'unknown-resource' if untrusted == 'unverified' else original['resource_id']
    before_calls = len(calls)
    with pytest.raises(ValueError):
        positions.manage(caller, USER, target, 'adjust-untrusted', 'add_margin', amount=1)
    assert len(calls) == before_calls


@pytest.mark.parametrize('condition', ['disabled', 'spot_only', 'bankrupt'])
def test_disabled_and_bankrupt_accounts_cannot_adjust_positions(ledger, condition):
    caller, _, _ = ledger
    original = _open(caller)
    if condition == 'bankrupt':
        _change_raw(lambda account: account.update(account_status='bankrupt', bankruptcy_reason='insufficient_cash_for_interest'))
    else:
        paper.admin_update('qqbot', USER, paper.AccountUpdate(**({'enabled': False} if condition == 'disabled' else {'markets': ['spot']})))
    before = _raw()
    with pytest.raises(ValueError, match='bankrupt|disabled'):
        _manage(caller, original, 'add_margin', 'adjust-inactive', amount=1)
    after = _raw()
    for field in ('cash', 'debt', 'positions', 'fees', 'account_status', 'reserved_cash'):
        assert after[field] == before[field]


@pytest.mark.parametrize('delay_stage', ['account_write', 'pending_order_scan'])
def test_stale_quote_is_rechecked_after_account_lock_before_any_adjustment(ledger, monkeypatch, delay_stage):
    caller, _, _ = ledger
    original = _open(caller)
    before = _raw()
    now = [time.time()]
    monkeypatch.setattr(paper.time, 'time', lambda: now[0])
    if delay_stage == 'account_write':
        original_write = paper._write
        def delayed_write(*args, **kwargs):
            now[0] += 16
            return original_write(*args, **kwargs)
        monkeypatch.setattr(paper, '_write', delayed_write)
    else:
        original_scan = positions._check_pending_entries
        def delayed_scan(*args, **kwargs):
            original_scan(*args, **kwargs)
            now[0] += 16
        monkeypatch.setattr(positions, '_check_pending_entries', delayed_scan)
    with pytest.raises(orders.PaperQuoteExpired):
        _manage(caller, original, 'set_leverage', 'adjust-delayed-quote', leverage=20)
    assert _raw() == before


def test_quote_io_is_outside_account_lock_and_commit_uses_latest_cash(ledger, monkeypatch):
    caller, _, _ = ledger
    original = _open(caller)
    original_market = paper.market
    def concurrent_debit(*args, **kwargs):
        with store.connection() as conn:
            assert try_lock_scope(conn, 'paper-account', 'qqbot', USER), 'Quote IO held an account lock'
            account = paper._get(conn, 'qqbot', USER)
            account['cash'] = 10
            paper._save(conn, account)
            conn.commit()
        return original_market(*args, **kwargs)
    monkeypatch.setattr(paper, 'market', concurrent_debit)
    with pytest.raises(ValueError, match='Insufficient unreserved cash'):
        _manage(caller, original, 'add_margin', 'adjust-concurrent-cash', amount=20)
    account = paper.get_account(caller, USER)
    assert account['cash'] == 10
    assert account['positions'][0]['margin'] == 100


def test_closed_reopened_resource_during_quote_cannot_receive_old_adjustment(ledger, monkeypatch):
    caller, _, _ = ledger
    original = _open(caller)
    original_market = paper.market
    def reopen(*args, **kwargs):
        monkeypatch.setattr(paper, 'market', original_market)
        orders.execute_market(caller, USER, 'swap', SYMBOL, 'close_long', 'quote-close-position', close_all=True)
        orders.execute_market(caller, USER, 'swap', SYMBOL, 'open_long', 'quote-reopen-position', amount=10, leverage=10)
        return original_market(*args, **kwargs)
    monkeypatch.setattr(paper, 'market', reopen)
    with pytest.raises(ValueError, match='position_id'):
        _manage(caller, original, 'add_margin', 'adjust-ended-cycle', amount=20)
    current = paper.get_account(caller, USER)['positions'][0]
    assert current['resource_id'] != original['resource_id']
    assert current['cycle_id'] != original['cycle_id']
    assert current['margin'] == 100
    assert current['added_margin'] == 0


def test_account_reset_during_quote_rejects_old_intent(ledger, monkeypatch):
    caller, _, _ = ledger
    original = _open(caller)
    original_market = paper.market
    def reset_account(*args, **kwargs):
        _change_raw(lambda account: account.update(account_status='bankrupt', bankruptcy_reason='insufficient_cash_for_interest'))
        lifecycle.reset(caller, USER, 'quote-account-reset')
        return original_market(*args, **kwargs)
    monkeypatch.setattr(paper, 'market', reset_account)
    with pytest.raises(ValueError, match='cycle changed'):
        _manage(caller, original, 'add_margin', 'adjust-pre-reset', amount=20)
    current = paper.get_account(caller, USER)
    assert current['cash'] == 1000
    assert current['positions'] == []
    assert current['reset_count'] == 1


def test_conflicting_same_position_entry_order_blocks_only_actual_leverage_change(ledger):
    caller, _, _ = ledger
    original = _open(caller)
    entry = orders.place(caller, USER, 'swap', SYMBOL, 'open_long', 'limit',
                         'pending-same-position', amount=1, leverage=10, price=90)['order']
    with pytest.raises(ValueError, match=entry['order_id']):
        _manage(caller, original, 'set_leverage', 'pending-blocks-change', leverage=20)
    _manage(caller, original, 'set_leverage', 'pending-allows-same', leverage=10)
    _manage(caller, original, 'add_margin', 'pending-allows-margin', amount=10)
    assert orders.get_orders(caller, USER)['orders'] == [entry]
    orders.cancel(caller, USER, entry['order_id'], 'pending-entry-cancel')
    result = _manage(caller, original, 'set_leverage', 'pending-then-change', leverage=20)
    assert result['margin_after'] == 60


def test_opposite_side_pending_entry_does_not_block_position_leverage_change(ledger):
    caller, _, _ = ledger
    original = _open(caller)
    entry = orders.place(caller, USER, 'swap', SYMBOL, 'open_short', 'limit',
                         'pending-other-side', amount=1, leverage=10, price=110)['order']
    result = _manage(caller, original, 'set_leverage', 'opposite-side-change', leverage=20)
    assert result['leverage_after'] == 20
    assert orders.get_orders(caller, USER)['orders'] == [entry]


def test_increase_and_partial_close_keep_manual_margin_consistent(ledger):
    caller, _, _ = ledger
    original = _open(caller)
    _manage(caller, original, 'add_margin', 'scale-manual-margin', amount=40)
    larger = orders.execute_market(caller, USER, 'swap', SYMBOL, 'open_long', 'scale-position-up', amount=10, leverage=10)
    assert larger['account']['positions'][0]['added_margin'] == 40
    assert larger['account']['positions'][0]['margin'] == 240
    partial = orders.execute_market(caller, USER, 'swap', SYMBOL, 'close_long', 'scale-position-down', amount=5)
    remaining = partial['account']['positions'][0]
    assert (remaining['amount'], remaining['margin'], remaining['added_margin']) == (15, 180, 30)
    result = _manage(caller, original, 'set_leverage', 'scale-remaining-leverage', leverage=20)
    assert (result['base_margin'], result['margin_after'], result['added_margin']) == (75, 105, 30)
    orders.execute_market(caller, USER, 'swap', SYMBOL, 'close_long', 'scale-position-full-close', close_all=True)
    reopened = orders.execute_market(caller, USER, 'swap', SYMBOL, 'open_long', 'scale-position-reopen', amount=10, leverage=10)
    assert reopened['account']['positions'][0]['added_margin'] == 0


def test_eight_decimal_margin_adjustment_is_exact_and_reversible(ledger):
    caller, _, _ = ledger
    original = _open(caller)
    before = paper.get_account(caller, USER)
    added = _manage(caller, original, 'add_margin', 'precision-add-margin', amount=.00000001)
    assert added['margin_after'] == 100.00000001
    assert added['cash_change'] == -.00000001
    removed = _manage(caller, original, 'reduce_margin', 'precision-remove-margin', amount=.00000001)
    assert removed['margin_after'] == 100
    assert removed['added_margin'] == 0
    assert removed['account']['cash'] == before['cash']


def test_weighted_entry_rounding_does_not_move_cash_on_same_leverage_or_block_adding(ledger):
    caller, ticker, _ = ledger
    create(caller)
    ticker.update(price=.10000001, bid=.10000001, ask=.10000001)
    orders.execute_market(caller, USER, 'swap', SYMBOL, 'open_long', 'rounded-first-entry', amount=10000, leverage=10)
    ticker.update(price=.10000002, bid=.10000002, ask=.10000002)
    opened = orders.execute_market(caller, USER, 'swap', SYMBOL, 'open_long', 'rounded-second-entry', amount=20000, leverage=10)
    original = opened['account']['positions'][0]
    assert original['margin'] == 300.00005
    assert original['entry_price'] == .10000002
    unchanged = _manage(caller, original, 'set_leverage', 'rounded-same-leverage', leverage=10)
    assert unchanged['cash_change'] == 0
    assert unchanged['margin_after'] == original['margin']
    assert unchanged['account']['cash'] == opened['account']['cash']
    added = _manage(caller, original, 'add_margin', 'rounded-add-minimum', amount=.00000001)
    assert added['margin_after'] == 300.00005001
    assert added['added_margin'] == .00000001
