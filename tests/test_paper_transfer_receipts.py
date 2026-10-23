"""Transfers retain their money, recipient and account-cycle facts through MCP."""
import asyncio
from datetime import datetime

import pytest

from test_mcp_canonical_catalog import call, server_for
from test_mcp_paper import create, ledger
from backend.mcp import canonical_tools, paper, paper_orders
from backend.mcp import paper_lifecycle as lifecycle
from backend.utils.trade_summary import compact_trade_receipt, render_trade_receipt


SENDER = 'sender-' + 'a' * 90
RECIPIENT = 'recipient-' + 'b' * 90


@pytest.fixture
def transfer_accounts(ledger, monkeypatch):
    caller, _, _ = ledger
    now = datetime.fromisoformat('2026-10-10T00:00:00+08:00').timestamp()
    monkeypatch.setattr(lifecycle.time, 'time', lambda: now)
    lifecycle.update_settings(paper.PaperSettings(**{
        **paper.settings(), 'daily_interest_rate': .01,
    }))
    create(caller, SENDER)
    create(caller, RECIPIENT)
    paper.bank(caller, SENDER, 'borrow', 100, 'transfer-receipt-loan')
    paper_orders.place(
        caller, SENDER, 'spot', 'BTC/USDT', 'buy', 'limit', 'transfer-receipt-reserved',
        amount=1.16, price=90)
    return caller


def test_transfer_receipt_reports_exact_recipient_and_remaining_available_cash(transfer_accounts):
    caller = transfer_accounts
    result = lifecycle.transfer(caller, SENDER, RECIPIENT, 995, 'transfer-receipt-complete')
    assert result['status'] == 'completed'
    assert result['action'] == 'transfer'
    assert result['currency'] == 'USDT'
    assert result['sender_user_id'] == SENDER
    assert result['recipient_user_id'] == RECIPIENT
    assert result['amount'] == 995
    assert result['cash_after'] == 105
    assert result['reserved_cash'] == pytest.approx(104.5044)
    assert result['available_cash'] == pytest.approx(.4956)
    assert result['interest_reserve']['available_cash'] == result['available_cash']
    assert result['interest_reserve']['estimated_next_interest'] == 1
    assert result['interest_reserve']['shortfall'] == pytest.approx(.5044)
    assert result['account']['account_status'] == 'active'
    recipient = paper.get_account(caller, RECIPIENT)
    assert recipient['cash'] == 1995
    assert recipient['cash'] + result['cash_after'] == 2100
    assert result['account']['debt'] == 100 and recipient['debt'] == 0
    assert result['account']['reserved_cash'] == result['reserved_cash']
    assert result['account']['interest_paid'] == 0
    assert result['account']['competition_capital'] == 5
    assert recipient['competition_capital'] == 1995

    for user_id, kind, balance in ((SENDER, 'transfer_out', 105), (RECIPIENT, 'transfer_in', 1995)):
        history = canonical_tools.records(caller, 'history', user_id=user_id)
        recorded = next(event['payload'] for event in history['items'] if event['kind'] == kind)
        assert recorded['currency'] == 'USDT'
        assert recorded['sender_user_id'] == SENDER and recorded['recipient_user_id'] == RECIPIENT
        assert recorded['amount'] == 995 and recorded['cash_after'] == balance

    for receipt in (result, compact_trade_receipt(result)):
        text = render_trade_receipt(receipt)
        assert f'付款账户 {SENDER} → 收款账户 {RECIPIENT}' in text
        assert '995.0 USDT' in text
        assert '转后可用现金 0.4956 USDT' in text
        assert '挂单已占用 104.5044 USDT' in text
        assert '占用资金不能用于转账' in text
        assert result['interest_reserve']['message'] in text


def test_sdk_transfer_text_and_compact_receipt_preserve_financial_facts(transfer_accounts, monkeypatch):
    caller = transfer_accounts
    server = server_for(caller, monkeypatch)
    args = {'user_id': SENDER, 'recipient_user_id': RECIPIENT,
            'amount': 995, 'operation_id': 'transfer-sdk-complete'}
    response = asyncio.run(server.call_tool('paper_transfer', args))
    assert not response.isError
    result = response.structuredContent
    assert result['action'] == 'transfer' and result['currency'] == 'USDT'
    assert result['sender_user_id'] == SENDER and result['recipient_user_id'] == RECIPIENT
    assert result['cash_after'] == 105
    assert result['available_cash'] == pytest.approx(.4956)
    assert result['reserved_cash'] == pytest.approx(104.5044)
    assert 'account' not in result
    text = response.content[0].text
    assert SENDER in text and RECIPIENT in text
    assert 'USDT' in text and '转后可用现金' in text and '挂单已占用' in text
    assert result['interest_reserve']['message'] in text
    assert call(server, 'paper_transfer', args) == result
    assert paper.get_account(caller, RECIPIENT)['cash'] == 1995


def test_transfer_still_rejects_spending_order_reservations(transfer_accounts):
    caller = transfer_accounts
    sender_before = paper.get_account(caller, SENDER)
    recipient_before = paper.get_account(caller, RECIPIENT)
    with pytest.raises(ValueError, match='available cash'):
        lifecycle.transfer(caller, SENDER, RECIPIENT, 996, 'transfer-over-reserved-cash')
    assert paper.get_account(caller, SENDER) == sender_before
    assert paper.get_account(caller, RECIPIENT) == recipient_before


def test_completed_transfer_retry_after_reset_returns_original_without_spending_new_funds(transfer_accounts):
    caller = transfer_accounts
    original = lifecycle.transfer(caller, SENDER, RECIPIENT, 995, 'transfer-before-reset')
    paper.admin_update('qqbot', SENDER, paper.AccountUpdate(cash=-1))
    lifecycle.reset(caller, SENDER, 'transfer-sender-reset')
    assert lifecycle.transfer(caller, SENDER, RECIPIENT, 995, 'transfer-before-reset') == original
    sender = paper.get_account(caller, SENDER)
    assert sender['account_epoch'] == 1
    assert sender['cash'] == 1000 and sender['debt'] == 0
    assert sender['reserved_cash'] == 0
    assert paper.get_account(caller, RECIPIENT)['cash'] == 1995
    assert sum(event['kind'] == 'transfer_out' for event in paper.history(caller, SENDER)) == 1
    assert sum(event['kind'] == 'transfer_in' for event in paper.history(caller, RECIPIENT)) == 1


def test_transfer_waiting_for_sender_lock_cannot_spend_reset_cycle_money(ledger, monkeypatch):
    caller, _, _ = ledger
    create(caller, 'a')
    create(caller, 'b')
    original_lock = lifecycle.lock_scope
    reset = []

    def reset_before_account_lock(conn, *scope):
        if scope == ('paper-account', 'qqbot', 'a') and not reset:
            reset.append(True)
            paper.admin_update('qqbot', 'a', paper.AccountUpdate(cash=-1))
            lifecycle.reset(caller, 'a', 'transfer-reset-while-waiting')
        return original_lock(conn, *scope)

    monkeypatch.setattr(lifecycle, 'lock_scope', reset_before_account_lock)
    with pytest.raises(ValueError, match='cycle changed'):
        lifecycle.transfer(caller, 'a', 'b', 100, 'transfer-old-cycle-intent')
    assert reset == [True]
    sender = paper.get_account(caller, 'a')
    assert sender['account_epoch'] == 1 and sender['cash'] == 1000
    assert paper.get_account(caller, 'b')['cash'] == 1000
    assert not any(event['kind'] == 'transfer_out' for event in paper.history(caller, 'a'))
    assert not any(event['kind'] == 'transfer_in' for event in paper.history(caller, 'b'))


def test_transfer_without_loan_does_not_add_interest_reminder(ledger):
    caller, _, _ = ledger
    create(caller, 'a')
    create(caller, 'b')
    result = lifecycle.transfer(caller, 'a', 'b', 100, 'transfer-without-interest')
    assert 'interest_reserve' not in result
    assert result['available_cash'] == 900 and result['reserved_cash'] == 0
    text = render_trade_receipt(result)
    assert '付款账户 a → 收款账户 b' in text
    assert '100.0 USDT' in text
    assert '利息' not in text
