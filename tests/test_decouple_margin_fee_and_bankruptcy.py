import pytest
from test_mcp_paper import create, ledger
from backend.mcp import paper, paper_orders as orders, store


def test_margin_amount_budget_includes_fee(ledger):
    caller, ticker, _ = ledger
    create(caller, 'budget-user')
    # Cash starts at 1000
    res = orders.execute_market(caller, 'budget-user', 'swap', 'BTC', 'open_long', 'budget-001', margin_amount=500, leverage=10)
    # Total outlay (used_margin + fee) must strictly equal 500
    used_margin = res['account']['used_margin']
    fee = res['fill']['fee']
    assert round(used_margin + fee, 4) == 500
    # Available cash was reduced by exactly 500
    assert round(res['account']['cash'], 4) == 500


def test_negative_net_equity_with_open_positions_does_not_bankrupt_and_allows_tp(ledger):
    caller, ticker, _ = ledger
    create(caller, 'neg-equity-user')
    # Borrow 4000 USDT -> cash 5000, debt 4000, net equity 1000
    paper.bank(caller, 'neg-equity-user', 'borrow', 4000, 'borrow-001')
    
    # Open LONG position with TP at 110: margin_amount 4800, leverage 10 -> notional ~47500
    res = orders.execute_market(
        caller, 'neg-equity-user', 'swap', 'BTC', 'open_long', 'open-heavy-001',
        margin_amount=4800, leverage=10, take_profit=110
    )
    assert res['account']['account_status'] == 'active'
    
    # Price drops to 95 -> float loss, net equity becomes negative! (above liquidation at ~91.8)
    ticker.update(price=95, bid=95, ask=95)
    paper.reconcile()
    
    acc = paper.get_account(caller, 'neg-equity-user')
    assert acc['net_equity'] < 0
    # Crucial: Account MUST NOT be bankrupt because positions are still open and healthy!
    assert acc['account_status'] == 'active'
    
    # Cannot open new positions or borrow while net equity <= 0
    with pytest.raises(ValueError, match='Cannot open new positions when net equity is non-positive'):
        orders.execute_market(caller, 'neg-equity-user', 'swap', 'ETH', 'open_long', 'open-blocked', amount=1, leverage=5)
    with pytest.raises(ValueError, match='Cannot borrow additional funds when net equity is non-positive'):
        paper.bank(caller, 'neg-equity-user', 'borrow', 100, 'borrow-blocked')
        
    # Price rises to 110 -> hits Take Profit!
    ticker.update(price=110, bid=110, ask=110)
    paper.reconcile()
    
    # Take profit order should have matched and closed the position!
    acc = paper.get_account(caller, 'neg-equity-user')
    assert acc['positions'] == []
    # Position closed with profit, cash and net equity are positive
    assert acc['cash'] > 5000
    assert acc['net_equity'] > 1000
    assert acc['account_status'] == 'active'


def test_auto_heal_recovers_bankrupt_account_with_open_positions(ledger):
    caller, ticker, _ = ledger
    create(caller, 'stuck-user')
    # Open a position
    orders.execute_market(caller, 'stuck-user', 'swap', 'BTC', 'open_long', 'stuck-open', amount=1, leverage=10)
    
    # Manually simulate historical bankrupt state on an account with open positions
    with store.connection() as conn:
        acc = paper._get(conn, 'qqbot', 'stuck-user')
        acc['account_status'] = 'bankrupt'
        paper._save(conn, acc)
        conn.commit()
        
    # When account is retrieved or upgraded, it should auto-heal to active
    healed = paper.get_account(caller, 'stuck-user')
    assert healed['account_status'] == 'active'
    assert len(healed['positions']) == 1
    
    # Event should be recorded
    events = paper.history(caller, 'stuck-user')
    assert any(e.get('kind') == 'bankruptcy_recovered' for e in events)


def test_closed_positions_with_zero_cash_and_debt_triggers_bankruptcy(ledger):
    caller, ticker, _ = ledger
    create(caller, 'wipeout-user')
    # Borrow 1000 -> cash 2000, debt 1000
    paper.bank(caller, 'wipeout-user', 'borrow', 1000, 'borrow-wipeout')
    
    # Open position with 2000 margin (all cash), 10x leverage
    orders.execute_market(caller, 'wipeout-user', 'swap', 'BTC', 'open_long', 'open-wipeout', margin_amount=2000, leverage=10)
    
    # Price drops to 80, triggering liquidation (maintenance margin rate is 0.02, 10x gets liquidated around 90)
    ticker.update(price=80, bid=80, ask=80)
    paper.reconcile()
    
    # After liquidation, position is closed. Cash is zero/payout minimal, debt is 1000.
    acc = paper.get_account(caller, 'wipeout-user')
    assert acc['positions'] == []
    assert acc['debt'] == 1000
    # Now that positions are empty, net_equity <= 0 and cash <= 0, account is officially bankrupt
    assert acc['account_status'] == 'bankrupt'
