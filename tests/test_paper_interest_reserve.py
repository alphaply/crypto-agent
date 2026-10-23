"""Interest reminders stay advisory and survive the model-facing MCP receipts."""
import asyncio
from datetime import datetime

import pytest

from test_mcp_canonical_catalog import call, server_for
from test_mcp_paper import create, ledger
from backend.mcp import canonical_tools, paper, paper_orders, store
from backend.mcp import paper_lifecycle as lifecycle
from backend.utils.trade_summary import compact_trade_receipt, render_trade_receipt


USER = '123456'
DAY = 86400
HOUR = 3600


@pytest.fixture
def clock(ledger, monkeypatch):
    value = [datetime.fromisoformat('2026-10-10T00:00:00+08:00').timestamp()]
    monkeypatch.setattr(lifecycle.time, 'time', lambda: value[0])
    return value


def set_rate(rate):
    lifecycle.update_settings(paper.PaperSettings(**{
        **paper.settings(), 'daily_interest_rate': rate,
    }))


@pytest.fixture
def borrower(ledger, clock):
    caller, _, _ = ledger
    set_rate(.01)
    create(caller)
    paper.bank(caller, USER, 'borrow', 500, 'reserve-borrow-500')
    return caller


def interest_events(caller):
    return [event for event in paper.history(caller, USER) if event['kind'] == 'interest']


@pytest.mark.parametrize('market,action,symbol', [
    ('spot', 'buy', 'BTC/USDT'),
    ('swap', 'open_long', 'BTC/USDT:USDT'),
    ('swap', 'open_short', 'BTC/USDT:USDT'),
])
@pytest.mark.parametrize('order_type', ['market', 'limit'])
def test_entries_keep_requested_size_when_interest_cash_would_be_insufficient(
        borrower, clock, market, action, symbol, order_type):
    kwargs = {'leverage': 1} if market == 'swap' else {}
    if order_type == 'market':
        requested = 14.95
        result = paper_orders.execute_market(
            borrower, USER, market, symbol, action, 'reserve-market-entry',
            amount=requested, **kwargs)
        assert result['status'] == 'completed'
        assert result['fill']['amount'] == requested
        assert result['account']['cash'] == pytest.approx(3.505)
        assert result['account']['reserved_cash'] == 0
    else:
        requested = 16.6
        result = paper_orders.place(
            borrower, USER, market, symbol, action, 'limit', 'reserve-limit-entry',
            amount=requested, price=90, **kwargs)
        assert result['status'] == 'submitted'
        assert result['order']['amount'] == requested
        assert result['order']['state'] == 'open'
        assert result['account']['cash'] == 1500
        assert result['account']['reserved_cash'] == pytest.approx(1495.494)

    reminder = result['interest_reserve']
    assert reminder == result['account']['interest_reserve']
    assert reminder['advisory_only'] is True
    assert reminder['currency'] == 'USDT'
    assert reminder['estimated_daily_interest'] == reminder['estimated_next_interest'] == 5
    assert reminder['next_settlement_at'] == clock[0] + DAY
    assert reminder['available_cash'] == result['account']['available_cash']
    assert 0 <= reminder['available_cash'] < 5
    assert reminder['shortfall'] == pytest.approx(5 - reminder['available_cash'])
    assert '预留' in reminder['message'] and '利息' in reminder['message']
    assert compact_trade_receipt(result)['interest_reserve'] == reminder
    assert reminder['message'] in render_trade_receipt(result)
    assert result['account']['interest_paid'] == 0
    assert not interest_events(borrower)


def test_increasing_limit_order_refreshes_reminder_without_reserving_interest(borrower):
    placed = paper_orders.place(
        borrower, USER, 'spot', 'BTC/USDT', 'buy', 'limit', 'reserve-small-limit',
        amount=1, price=90)
    assert placed['interest_reserve']['shortfall'] == 0
    order_id = placed['order']['order_id']
    edited = paper_orders.edit(
        borrower, USER, order_id, 'reserve-increase-limit', amount=16.6)

    assert edited['status'] == 'completed'
    assert edited['order']['order_id'] == order_id
    assert edited['order']['amount'] == 16.6
    assert edited['account']['cash'] == 1500
    assert edited['account']['reserved_cash'] == pytest.approx(1495.494)
    assert edited['interest_reserve']['available_cash'] == pytest.approx(4.506)
    assert edited['interest_reserve']['shortfall'] == pytest.approx(.494)
    assert edited['account']['interest_paid'] == 0
    assert paper_orders.edit(
        borrower, USER, order_id, 'reserve-increase-limit', amount=16.6) == edited
    assert len(paper_orders.get_orders(borrower, USER)['orders']) == 1
    assert paper.get_account(borrower, USER)['reserved_cash'] == pytest.approx(1495.494)


@pytest.mark.parametrize('debt,rate', [(0, .01), (500, 0)])
def test_no_reminder_without_interest_liability(ledger, clock, debt, rate):
    caller, _, _ = ledger
    set_rate(rate)
    create(caller)
    if debt:
        paper.bank(caller, USER, 'borrow', debt, 'reserve-zero-rate-loan')
    result = paper.trade(caller, USER, 'spot', 'BTC/USDT', 'buy', 1, 'reserve-no-interest-entry')

    assert 'interest_reserve' not in result
    assert result['account']['interest_reserve'] is None
    assert '预留' not in render_trade_receipt(result)
    balances = canonical_tools.account_state(caller, user_id=USER, include=['balances'])['balances']
    assert balances['interest_reserve'] is None


def test_first_day_estimate_uses_only_time_after_borrowing(ledger, clock):
    caller, _, _ = ledger
    set_rate(.01)
    create(caller)
    clock[0] += 12 * HOUR
    borrowed = paper.bank(caller, USER, 'borrow', 500, 'reserve-midday-borrow')
    reminder = borrowed['account']['interest_reserve']
    assert reminder['estimated_daily_interest'] == 5
    assert reminder['estimated_next_interest'] == 2.5
    assert reminder['next_settlement_at'] == clock[0] + 12 * HOUR
    clock[0] += 6 * HOUR
    assert paper.get_account(caller, USER)['interest_reserve']['estimated_next_interest'] == 2.5
    assert not interest_events(caller)

    clock[0] += 6 * HOUR
    lifecycle.maintenance_finances()
    view = paper.get_account(caller, USER)
    assert view['interest_paid'] == 2.5
    assert view['cash'] == 1497.5
    assert view['interest_reserve']['estimated_next_interest'] == 5
    assert view['interest_reserve']['next_settlement_at'] == clock[0] + DAY
    lifecycle.maintenance_finances()
    assert len(interest_events(caller)) == 1
    assert paper.get_account(caller, USER)['cash'] == 1497.5


def test_rate_change_preserves_prior_hours_in_immediate_balance_estimate(ledger, clock):
    caller, _, _ = ledger
    set_rate(.01)
    create(caller)
    clock[0] += 6 * HOUR
    paper.bank(caller, USER, 'borrow', 1000, 'reserve-before-rate-change')
    clock[0] += 6 * HOUR
    set_rate(.02)
    with store.connection() as conn:
        before = paper._get(conn, caller['paper_namespace'], USER)
    view = canonical_tools.account_state(caller, user_id=USER, include=['balances'])
    reminder = view['balances']['interest_reserve']
    assert reminder['daily_interest_rate'] == .02
    assert reminder['estimated_daily_interest'] == 20
    # Six hours at 1%, followed by twelve hours at 2%.
    assert reminder['estimated_next_interest'] == 12.5
    with store.connection() as conn:
        assert paper._get(conn, caller['paper_namespace'], USER) == before
    assert not interest_events(caller)

    clock[0] += 12 * HOUR
    lifecycle.maintenance_finances()
    assert paper.get_account(caller, USER)['interest_paid'] == 12.5


@pytest.mark.parametrize('repaid,expected', [(400, 6.5), (1000, 5)])
def test_partial_or_full_repayment_keeps_already_accrued_interest(ledger, clock, repaid, expected):
    caller, _, _ = ledger
    set_rate(.01)
    create(caller)
    clock[0] += 6 * HOUR
    paper.bank(caller, USER, 'borrow', 1000, 'reserve-before-repayment')
    clock[0] += 12 * HOUR
    receipt = paper.bank(caller, USER, 'repay', repaid, 'reserve-repayment')
    reminder = receipt['account']['interest_reserve']
    assert reminder['estimated_daily_interest'] == pytest.approx((1000 - repaid) * .01)
    assert reminder['estimated_next_interest'] == expected
    assert receipt['account']['interest_paid'] == 0

    clock[0] += 6 * HOUR
    lifecycle.maintenance_finances()
    view = paper.get_account(caller, USER)
    assert view['interest_paid'] == expected
    assert view['debt'] == 1000 - repaid
    assert view['cash'] == 2000 - repaid - expected
    assert len(interest_events(caller)) == 1
    if repaid == 1000:
        assert view['interest_reserve'] is None


def test_zero_rate_does_not_hide_interest_accrued_earlier_in_the_day(ledger, clock):
    caller, _, _ = ledger
    set_rate(.01)
    create(caller)
    clock[0] += 6 * HOUR
    paper.bank(caller, USER, 'borrow', 1000, 'reserve-before-zero-rate')
    clock[0] += 6 * HOUR
    set_rate(0)
    reminder = paper.get_account(caller, USER)['interest_reserve']
    assert reminder is not None
    assert reminder['estimated_daily_interest'] == 0
    assert reminder['estimated_next_interest'] == 2.5
    clock[0] += 12 * HOUR
    lifecycle.maintenance_finances()
    view = paper.get_account(caller, USER)
    assert view['interest_paid'] == 2.5
    assert view['interest_reserve'] is None


def test_read_after_midnight_includes_unsettled_interest_without_charging(borrower, clock):
    paper_orders.place(
        borrower, USER, 'spot', 'BTC/USDT', 'buy', 'limit', 'reserve-before-midnight',
        amount=16.6, price=90)
    with store.connection() as conn:
        before = paper._get(conn, borrower['paper_namespace'], USER)
    clock[0] += DAY + 6 * HOUR
    view = paper.get_account(borrower, USER)
    reminder = view['interest_reserve']
    assert reminder['overdue_interest'] == 5
    assert reminder['estimated_next_interest'] == 5
    assert reminder['estimated_required_cash'] == 10
    assert reminder['next_settlement_at'] == clock[0] + 18 * HOUR
    assert reminder['available_cash'] == pytest.approx(4.506)
    assert reminder['shortfall'] == pytest.approx(5.494)
    assert view['cash'] == 1500 and view['interest_paid'] == 0
    assert view['reserved_cash'] == pytest.approx(1495.494)
    assert not interest_events(borrower)
    with store.connection() as conn:
        assert paper._get(conn, borrower['paper_namespace'], USER) == before


def test_only_balances_facet_includes_interest_reminder_and_context_explains_it(borrower, monkeypatch):
    server = server_for(borrower, monkeypatch)
    context = call(server, 'get_trading_context', {'user_id': USER})
    rule = context['account_rules']['interest_reserve']
    assert 'unreserved USDT' in rule
    assert 'do not change their requested size or enforce a reserve' in rule
    for facet in ('positions', 'inventory', 'orders'):
        view = call(server, 'get_account_state', {'user_id': USER, 'include': [facet]})
        assert 'balances' not in view
        assert 'interest_reserve' not in view
        assert 'estimated_next_interest' not in str(view)
    view = call(server, 'get_account_state', {'user_id': USER, 'include': ['balances']})
    assert view['balances']['interest_reserve']['estimated_next_interest'] == 5
    assert 'interest_reserve' not in view
    assert 'orders' not in view and 'positions' not in view


def test_sdk_compact_and_text_receipts_keep_reminder_and_retries_do_not_charge_or_reserve(
        borrower, clock, monkeypatch):
    server = server_for(borrower, monkeypatch)
    clock[0] += DAY
    request = {
        'user_id': USER, 'operation_id': 'reserve-sdk-entry', 'contract_version': 3,
        'actions': [{
            'action': 'open', 'market_type': 'spot', 'symbol': 'BTC/USDT',
            'side': 'LONG', 'order_type': 'limit', 'price': 90, 'amount': 16.55,
            'reason': 'user requested this exact size',
        }],
    }
    blocks = asyncio.run(server.call_tool('execute_trade_actions', request))
    assert not blocks.isError
    result = blocks.structuredContent
    assert result['status'] == 'submitted'
    child = result['results'][0]
    assert child['status'] == 'submitted'
    receipt = child.get('result', child)
    reminder = receipt['interest_reserve']
    assert reminder['estimated_next_interest'] == 5
    assert reminder['available_cash'] == pytest.approx(4.0105)
    assert reminder['shortfall'] == pytest.approx(.9895)
    assert 'account' not in child and 'account' not in receipt
    assert reminder['message'] in blocks.content[0].text
    assert reminder['message'] in render_trade_receipt(result)
    assert compact_trade_receipt(receipt)['interest_reserve'] == reminder
    before = paper.get_account(borrower, USER)
    assert before['interest_paid'] == 5
    assert before['cash'] == 1495
    assert before['reserved_cash'] == pytest.approx(1490.9895)
    assert len(interest_events(borrower)) == 1

    clock[0] += HOUR
    monkeypatch.setattr(paper, 'market', lambda *args, **kwargs: pytest.fail('Retry fetched a new quote'))
    assert call(server, 'execute_trade_actions', request) == result
    assert paper.get_account(borrower, USER) == before
    assert len(interest_events(borrower)) == 1
    assert len(paper_orders.get_orders(borrower, USER)['orders']) == 1
