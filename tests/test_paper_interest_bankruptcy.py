"""Interest cash shortfalls and historical recovery use an isolated Paper ledger."""
from copy import deepcopy
from datetime import datetime
from decimal import Decimal
import json

import pytest

from test_mcp_paper import create, ledger
from backend.mcp import paper, paper_lifecycle as lifecycle, paper_orders as orders, store


@pytest.fixture
def clock(ledger, monkeypatch):
    now = [datetime.fromisoformat('2026-10-20T00:00:00+08:00').timestamp()]
    monkeypatch.setattr(lifecycle.time, 'time', lambda: now[0])
    lifecycle.update_settings(paper.PaperSettings(
        initial_balance=1000, loan_limit=5000, daily_interest_rate=.01,
    ))
    return now


def _eight_pending_entries(caller):
    lifecycle.update_settings(paper.PaperSettings(
        initial_balance=66660, loan_limit=1000000, daily_interest_rate=.01,
    ))
    create(caller, 'eight-orders')
    paper.bank(caller, 'eight-orders', 'borrow', 1000000, 'eight-orders-borrow')
    pending = []
    for index in range(8):
        receipt = orders.place(
            caller, 'eight-orders', 'spot', 'BTC/USDT', 'buy', 'limit',
            f'eight-orders-entry-{index}', quote_amount=132329.375, price=50,
        )
        pending.append(receipt['order'])
    return pending


def _old_interest_failure(user_id, amount, period_end, *, event_epoch=None):
    """Persist the old settlement format, which did not record a bankruptcy cause."""
    with store.connection() as conn:
        account = json.loads(conn.execute(
            'SELECT payload FROM paper_accounts WHERE namespace=%s AND user_id=%s',
            ('qqbot', user_id),
        ).fetchone()[0])
        account.update(
            cash=paper.money(paper.signed_cash(account['cash']) - Decimal(str(amount))),
            interest_paid=paper.money(Decimal(str(account['interest_paid'])) + Decimal(str(amount))),
            interest_cursor=period_end, interest_accrued='0', account_status='bankrupt',
        )
        account.pop('bankruptcy_reason', None)
        epoch = account['account_epoch'] if event_epoch is None else event_epoch
        conn.execute(
            'INSERT INTO paper_interest_settlements(namespace,user_id,account_epoch,period_end,amount) '
            'VALUES(%s,%s,%s,%s,%s)', ('qqbot', user_id, epoch, period_end, amount),
        )
        paper._event(conn, account, 'interest', {
            'interest': amount, 'period_end': period_end, 'account_epoch': epoch,
        })
        paper._save(conn, account)
        saved = deepcopy(account)
        conn.commit()
    return saved


def test_eight_reserved_orders_do_not_bankrupt_after_interest_and_can_cancel(ledger, clock):
    caller, _, _ = ledger
    pending = _eight_pending_entries(caller)
    before = paper.get_account(caller, 'eight-orders')
    assert before['cash'] == 1066660
    assert before['reserved_cash'] == 1058635

    clock[0] += 86400
    lifecycle.maintenance_finances()
    account = paper.get_account(caller, 'eight-orders')
    assert account['cash'] == 1056660
    assert account['net_equity'] == 56660
    assert account['available_cash'] == 0
    assert account['reserved_cash'] == 1058635
    assert account['account_status'] == 'active'
    assert len(orders.get_orders(caller, 'eight-orders')['orders']) == 8

    cancelled = orders.cancel(caller, 'eight-orders', pending[0]['order_id'], 'eight-orders-cancel')
    assert cancelled['order']['state'] == 'cancelled'
    assert cancelled['account']['cash'] == 1056660
    assert cancelled['account']['available_cash'] == 130354.375
    assert cancelled['account']['reserved_cash'] == 926305.625
    assert orders.cancel(caller, 'eight-orders', pending[0]['order_id'], 'eight-orders-cancel') == cancelled

    lifecycle.maintenance_finances()
    replayed = paper.get_account(caller, 'eight-orders')
    assert replayed['cash'] == 1056660
    assert replayed['interest_paid'] == 10000
    events = paper.history(caller, 'eight-orders')
    assert sum(event['kind'] == 'interest' for event in events) == 1
    assert not any(event['kind'] == 'bankruptcy' for event in events)


def test_interest_reservation_shortfall_does_not_allow_an_unfunded_fill(ledger, clock):
    caller, ticker, _ = ledger
    create(caller, 'fill-shortfall')
    paper.bank(caller, 'fill-shortfall', 'borrow', 1000, 'fill-shortfall-borrow')
    pending = orders.place(
        caller, 'fill-shortfall', 'spot', 'BTC/USDT', 'buy', 'limit',
        'fill-shortfall-entry', quote_amount=1995, price=50,
    )
    clock[0] += 86400
    lifecycle.maintenance_finances()
    before_fill = paper.get_account(caller, 'fill-shortfall')
    assert before_fill['cash'] == 1990
    assert before_fill['reserved_cash'] == 1995
    assert before_fill['account_status'] == 'active'

    ticker.update(price=50, bid=50, ask=50)
    paper.reconcile()
    account = paper.get_account(caller, 'fill-shortfall')
    order = orders.records_page(
        caller, 'fill-shortfall', order_id=pending['order']['order_id'],
    )['order']
    assert order['state'] == 'rejected'
    assert 'Insufficient' in order['reason']
    assert account['cash'] == 1990
    assert account['spot'] == []
    assert account['reserved_cash'] == 0
    assert account['account_status'] == 'active'


@pytest.mark.parametrize('market_type', ['spot', 'swap'])
def test_actual_cash_shortfall_stays_frozen_with_holdings_and_after_receiving(ledger, clock, market_type):
    caller, ticker, _ = ledger
    create(caller, 'cash-shortfall')
    create(caller, 'cash-helper')
    paper.bank(caller, 'cash-shortfall', 'borrow', 1000, 'cash-shortfall-borrow')
    symbol = 'BTC/USDT' if market_type == 'spot' else 'BTC/USDT:USDT'
    entry_action = 'buy' if market_type == 'spot' else 'open_long'
    exit_action = 'sell' if market_type == 'spot' else 'close_long'
    orders.execute_market(
        caller, 'cash-shortfall', market_type, symbol, entry_action,
        'cash-shortfall-entry', amount=1, **({'leverage': 10} if market_type == 'swap' else {}),
    )
    pending = orders.place(
        caller, 'cash-shortfall', market_type, symbol, exit_action, 'limit',
        'cash-shortfall-exit', amount=1, price=300,
    )
    paper.admin_update('qqbot', 'cash-shortfall', paper.AccountUpdate(cash=3))
    clock[0] += 86400
    lifecycle.maintenance_finances()
    for _ in range(2):
        frozen = paper.get_account(caller, 'cash-shortfall')
        assert frozen['cash'] == -7
        assert frozen['account_status'] == 'bankrupt'
        assert frozen['bankruptcy_reason'] == 'insufficient_cash_for_interest'

    lifecycle.transfer(caller, 'cash-helper', 'cash-shortfall', 100, 'cash-shortfall-help')
    assert paper.get_account(caller, 'cash-shortfall')['cash'] == 93
    ticker.update(price=300, bid=300, ask=300)
    paper.reconcile()
    frozen = paper.get_account(caller, 'cash-shortfall')
    assert frozen['account_status'] == 'bankrupt'
    assert len(frozen['spot' if market_type == 'spot' else 'positions']) == 1
    assert orders.get_orders(caller, 'cash-shortfall')['orders'][0]['state'] == 'open'
    with pytest.raises(ValueError, match='bankrupt'):
        orders.cancel(caller, 'cash-shortfall', pending['order']['order_id'], 'cash-shortfall-cancel')
    with pytest.raises(ValueError, match='bankrupt'):
        orders.execute_market(
            caller, 'cash-shortfall', market_type, symbol, exit_action,
            'cash-shortfall-close', close_all=True,
        )
    for action in ('borrow', 'repay'):
        with pytest.raises(ValueError, match='bankrupt'):
            paper.bank(caller, 'cash-shortfall', action, 1, f'cash-shortfall-{action}-blocked')
    assert not any(event['kind'] == 'bankruptcy_recovered' for event in paper.history(caller, 'cash-shortfall'))
    reset = lifecycle.reset(caller, 'cash-shortfall', 'cash-shortfall-reset')
    assert reset['account']['account_status'] == 'active'
    assert reset['account'].get('bankruptcy_reason') is None
    assert paper.get_account(caller, 'cash-shortfall').get('bankruptcy_reason') is None


@pytest.mark.parametrize(('cash', 'expected_cash', 'status'), [
    (10, 0, 'active'),
    (10.00000001, .00000001, 'active'),
    (9.99999999, -.00000001, 'bankrupt'),
])
def test_interest_cash_boundary_uses_ledger_precision(ledger, clock, cash, expected_cash, status):
    caller, _, _ = ledger
    create(caller, 'cash-boundary')
    paper.bank(caller, 'cash-boundary', 'borrow', 1000, 'cash-boundary-borrow')
    orders.execute_market(caller, 'cash-boundary', 'spot', 'BTC/USDT', 'buy', 'cash-boundary-entry', amount=1)
    paper.admin_update('qqbot', 'cash-boundary', paper.AccountUpdate(cash=cash))
    clock[0] += 86400
    lifecycle.maintenance_finances()
    lifecycle.maintenance_finances()
    account = paper.get_account(caller, 'cash-boundary')
    assert account['cash'] == expected_cash
    assert account['interest_paid'] == 10
    assert account['account_status'] == status
    assert len(account['spot']) == 1
    assert sum(event['kind'] == 'interest' for event in paper.history(caller, 'cash-boundary')) == 1


@pytest.mark.parametrize('holding', [None, 'spot', 'swap'])
def test_historical_reserved_cash_false_bankruptcy_recovers_without_reset(ledger, clock, holding):
    caller, _, _ = ledger
    _eight_pending_entries(caller)
    if holding:
        orders.execute_market(
            caller, 'eight-orders', holding,
            'ETH/USDT' if holding == 'spot' else 'ETH/USDT:USDT',
            'buy' if holding == 'spot' else 'open_long',
            'old-false-freeze-holding', amount=1,
            **({'leverage': 10} if holding == 'swap' else {}),
        )
    before_orders = orders.get_orders(caller, 'eight-orders')['orders']
    clock[0] += 86400
    saved = _old_interest_failure('eight-orders', 10000, clock[0])
    recovered = paper.get_account(caller, 'eight-orders')
    assert recovered['account_status'] == 'active'
    for key in ('cash', 'debt', 'reserved_cash', 'interest_paid', 'account_epoch', 'reset_count', 'fees'):
        assert recovered[key] == saved[key]
    with store.connection() as conn:
        recovered_raw = json.loads(conn.execute(
            'SELECT payload FROM paper_accounts WHERE namespace=%s AND user_id=%s',
            ('qqbot', 'eight-orders'),
        ).fetchone()[0])
    assert recovered_raw['spot'] == saved['spot']
    assert recovered_raw['positions'] == saved['positions']
    assert orders.get_orders(caller, 'eight-orders')['orders'] == before_orders
    if holding is None:
        assert recovered['cash'] == 1056660
    assert recovered['debt'] == 1000000
    assert recovered['account_epoch'] == recovered['reset_count'] == 0

    paper.get_account(caller, 'eight-orders')
    lifecycle.maintenance_finances()
    assert paper.get_account(caller, 'eight-orders')['interest_paid'] == 10000
    events = paper.history(caller, 'eight-orders')
    assert sum(event['kind'] == 'bankruptcy_recovered' for event in events) == 1
    assert not any(event['kind'] == 'reset' for event in events)


@pytest.mark.parametrize('evidence', ['missing', 'previous_epoch'])
def test_historical_freeze_without_current_cycle_interest_proof_does_not_recover(ledger, clock, evidence):
    caller, _, _ = ledger
    _eight_pending_entries(caller)
    clock[0] += 86400
    if evidence == 'previous_epoch':
        with store.connection() as conn:
            account = paper._get(conn, 'qqbot', 'eight-orders')
            account.update(account_epoch=1, reset_count=1)
            paper._save(conn, account)
            conn.commit()
        _old_interest_failure('eight-orders', 10000, clock[0], event_epoch=0)
    else:
        with store.connection() as conn:
            account = paper._get(conn, 'qqbot', 'eight-orders')
            account.update(account_status='bankrupt', interest_cursor=clock[0])
            paper._save(conn, account)
            conn.commit()
    assert paper.get_account(caller, 'eight-orders')['account_status'] == 'bankrupt'
    assert len(orders.get_orders(caller, 'eight-orders')['orders']) == 8
    assert not any(event['kind'] == 'bankruptcy_recovered' for event in paper.history(caller, 'eight-orders'))


def test_historical_real_interest_shortfall_does_not_heal_when_money_arrives(ledger, clock):
    caller, _, _ = ledger
    create(caller, 'old-shortfall')
    create(caller, 'old-helper')
    paper.bank(caller, 'old-shortfall', 'borrow', 1000, 'old-shortfall-borrow')
    orders.execute_market(caller, 'old-shortfall', 'spot', 'BTC/USDT', 'buy', 'old-shortfall-entry', amount=1)
    paper.admin_update('qqbot', 'old-shortfall', paper.AccountUpdate(cash=3))
    clock[0] += 86400
    _old_interest_failure('old-shortfall', 10, clock[0])
    assert paper.get_account(caller, 'old-shortfall')['account_status'] == 'bankrupt'
    lifecycle.transfer(caller, 'old-helper', 'old-shortfall', 100, 'old-shortfall-help')
    account = paper.get_account(caller, 'old-shortfall')
    assert account['cash'] == 93
    assert account['debt'] == 1000
    assert account['interest_paid'] == 10
    assert len(account['spot']) == 1
    assert account['account_status'] == 'bankrupt'
    assert not any(event['kind'] == 'bankruptcy_recovered' for event in paper.history(caller, 'old-shortfall'))


def test_later_positive_interest_settlement_does_not_hide_earlier_real_bankruptcy(ledger, clock):
    caller, _, _ = ledger
    create(caller, 'older-shortfall')
    paper.bank(caller, 'older-shortfall', 'borrow', 1000, 'older-shortfall-borrow')
    orders.execute_market(caller, 'older-shortfall', 'spot', 'BTC/USDT', 'buy', 'older-shortfall-spot', amount=1)
    orders.place(
        caller, 'older-shortfall', 'spot', 'ETH/USDT', 'buy', 'limit',
        'older-shortfall-pending', quote_amount=1000, price=50,
    )
    paper.admin_update('qqbot', 'older-shortfall', paper.AccountUpdate(cash=3))
    clock[0] += 86400
    _old_interest_failure('older-shortfall', 10, clock[0])

    # Model a receipt from the old implementation without running today's
    # recovery code while constructing its historical ledger evidence.
    with store.connection() as conn:
        account = json.loads(conn.execute(
            'SELECT payload FROM paper_accounts WHERE namespace=%s AND user_id=%s',
            ('qqbot', 'older-shortfall'),
        ).fetchone()[0])
        account['cash'] = 93
        paper._event(conn, account, 'transfer_in', {'amount': 100})
        paper._save(conn, account)
        conn.commit()
    clock[0] += 86400
    _old_interest_failure('older-shortfall', 10, clock[0])

    account = paper.get_account(caller, 'older-shortfall')
    assert account['cash'] == 83
    assert account['reserved_cash'] == 1000
    assert account['interest_paid'] == 20
    assert account['account_status'] == 'bankrupt'
    assert len(account['spot']) == 1
    assert not any(event['kind'] == 'bankruptcy_recovered' for event in paper.history(caller, 'older-shortfall'))


def test_incomplete_interest_history_cannot_recover_before_hidden_cash_shortfall(ledger, clock, monkeypatch):
    caller, _, _ = ledger
    create(caller, 'limited-history')
    paper.bank(caller, 'limited-history', 'borrow', 1000, 'limited-history-borrow')
    orders.execute_market(caller, 'limited-history', 'spot', 'BTC/USDT', 'buy', 'limited-history-spot', amount=1)
    orders.place(
        caller, 'limited-history', 'spot', 'ETH/USDT', 'buy', 'limit',
        'limited-history-order', quote_amount=1895, price=50,
    )
    clock[0] += 86400
    _old_interest_failure('limited-history', 10, clock[0])
    with store.connection() as conn:
        account = json.loads(conn.execute(
            'SELECT payload FROM paper_accounts WHERE namespace=%s AND user_id=%s',
            ('qqbot', 'limited-history'),
        ).fetchone()[0])
        account['cash'] = 3
        paper._event(conn, account, 'admin_adjustment', {'cash': 3})
        paper._save(conn, account)
        conn.commit()
    clock[0] += 86400
    _old_interest_failure('limited-history', 10, clock[0])

    # The first interest event has nonnegative cash; the later genuine failure
    # lies beyond the allowed evidence scan. Incomplete proof must stay frozen.
    monkeypatch.setenv('PAPER_HISTORY_SCAN_LIMIT', '1')
    account = paper.get_account(caller, 'limited-history')
    assert account['cash'] == -7
    assert account['account_status'] == 'bankrupt'
    with store.connection() as conn:
        recovered = conn.execute(
            "SELECT COUNT(*) FROM paper_events WHERE namespace=%s AND user_id=%s AND kind='bankruptcy_recovered'",
            ('qqbot', 'limited-history'),
        ).fetchone()[0]
    assert recovered == 0


def test_explicit_equity_bankruptcy_is_not_reclassified_after_funding_and_interest(ledger, clock):
    caller, _, _ = ledger
    create(caller, 'equity-frozen')
    create(caller, 'equity-helper')
    for user_id in ('equity-frozen', 'equity-helper'):
        paper.bank(caller, user_id, 'borrow', 1000, user_id + '-borrow')
    orders.place(
        caller, 'equity-frozen', 'spot', 'BTC/USDT', 'buy', 'limit',
        'equity-frozen-order', quote_amount=1995, price=50,
    )
    paper.admin_update('qqbot', 'equity-frozen', paper.AccountUpdate(cash=0))
    assert paper.get_account(caller, 'equity-frozen')['bankruptcy_reason'] == 'nonpositive_net_equity'
    lifecycle.transfer(caller, 'equity-helper', 'equity-frozen', 2000, 'equity-frozen-help')
    clock[0] += 86400
    # Old accounts can lack the new reason field but retain their explicit
    # bankruptcy audit event. A later positive settlement cannot override it.
    _old_interest_failure('equity-frozen', 10, clock[0])
    account = paper.get_account(caller, 'equity-frozen')
    assert account['cash'] == 1990
    assert account['net_equity'] == 990
    assert account['reserved_cash'] == 1995
    assert account['spot'] == account['positions'] == []
    assert account['account_status'] == 'bankrupt'
    assert not any(event['kind'] == 'bankruptcy_recovered' for event in paper.history(caller, 'equity-frozen'))


def test_recovery_reloads_latest_balance_before_saving_a_stale_account(ledger, clock):
    from backend.storage import lock_scope

    caller, _, _ = ledger
    _eight_pending_entries(caller)
    clock[0] += 86400
    stale = _old_interest_failure('eight-orders', 10000, clock[0])
    with store.connection() as conn:
        lock_scope(conn, 'paper-account', 'qqbot', 'eight-orders')
        account = json.loads(conn.execute(
            'SELECT payload FROM paper_accounts WHERE namespace=%s AND user_id=%s',
            ('qqbot', 'eight-orders'),
        ).fetchone()[0])
        account['cash'] = paper.money(paper.signed_cash(account['cash']) + Decimal(100))
        paper._event(conn, account, 'transfer_in', {'amount': 100})
        paper._save(conn, account)
        conn.commit()
    assert stale['cash'] == 1056660
    with store.connection() as conn:
        repaired = paper.upgrade_account(conn, stale)
        assert repaired['cash'] == 1056760
        assert repaired['account_status'] == 'active'
        conn.commit()
    account = paper.get_account(caller, 'eight-orders')
    assert account['cash'] == 1056760
    assert account['debt'] == 1000000
    assert account['interest_paid'] == 10000
    assert account['account_status'] == 'active'


def test_resolved_equity_bankruptcy_does_not_block_a_later_reservation_recovery(ledger, clock):
    caller, _, _ = ledger
    _eight_pending_entries(caller)
    # Preserve a completed, older freeze/recovery episode in this same cycle.
    # The new freeze below is caused by interest consuming order reservations.
    with store.connection() as conn:
        account = paper._get(conn, 'qqbot', 'eight-orders')
        paper._event(conn, account, 'bankruptcy', {
            'reason': 'nonpositive_net_equity', 'account_epoch': account['account_epoch'],
        })
        paper._event(conn, account, 'bankruptcy_recovered', {
            'reason': 'active_positions_decoupled', 'account_epoch': account['account_epoch'],
        })
        conn.commit()
    clock[0] += 86400
    _old_interest_failure('eight-orders', 10000, clock[0])

    account = paper.get_account(caller, 'eight-orders')
    assert account['cash'] == 1056660
    assert account['net_equity'] == 56660
    assert account['account_status'] == 'active'
    assert len(orders.get_orders(caller, 'eight-orders')['orders']) == 8
    assert sum(event['kind'] == 'bankruptcy_recovered' for event in paper.history(caller, 'eight-orders')) == 2
