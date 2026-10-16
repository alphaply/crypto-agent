"""Finances run against a disposable PostgreSQL schema, never a runtime ledger."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from decimal import Decimal

import pytest

from test_mcp_paper import ledger, create
from backend.mcp import paper, paper_competition, store
from backend.mcp import paper_lifecycle as lifecycle


def test_signed_cash_is_not_a_negative_payment(ledger):
    assert paper.signed_cash(-7) == Decimal('-7')
    for value in (-1, True, float('nan'), float('inf')):
        with pytest.raises(ValueError):
            paper.number(value, positive=True)


@pytest.mark.parametrize('amount', [-10, 0, True, float('nan'), float('inf'), .000000001])
def test_bad_transfer_changes_neither_account(ledger, amount):
    caller, _, _ = ledger
    create(caller, 'a'); create(caller, 'b')
    with pytest.raises(ValueError):
        lifecycle.transfer(caller, 'a', 'b', amount, 'invalid-transfer')
    assert paper.get_account(caller, 'a')['cash'] == paper.get_account(caller, 'b')['cash'] == 1000


def test_transfer_borrowed_funds_and_replay(ledger):
    caller, _, _ = ledger
    create(caller, 'a'); create(caller, 'b')
    paper.bank(caller, 'a', 'borrow', 500, 'borrow-for-transfer')
    result = lifecycle.transfer(caller, 'a', 'b', 200, 'transfer-stable')
    assert lifecycle.transfer(caller, 'a', 'b', 200, 'transfer-stable') == result
    assert paper.get_account(caller, 'a')['debt'] == 500
    assert paper.get_account(caller, 'b')['cash'] == 1200
    assert paper.get_account(caller, 'b')['performance_pnl'] == 0
    with pytest.raises(ValueError, match='different'):
        lifecycle.transfer(caller, 'a', 'b', 201, 'transfer-stable')
    with pytest.raises(ValueError):
        lifecycle.transfer({**caller, 'paper_namespace': 'other'}, 'a', 'b', 1, 'transfer-other')


def test_concurrent_transfers_cannot_overspend(ledger):
    caller, _, _ = ledger
    create(caller, 'a'); create(caller, 'b')
    def send(i):
        try:
            lifecycle.transfer(caller, 'a', 'b', 600, 'concurrent-transfer-' + str(i))
            return True
        except ValueError:
            return False
    with ThreadPoolExecutor(2) as pool:
        assert sum(pool.map(send, range(2))) == 1
    assert paper.get_account(caller, 'a')['cash'] == 400
    assert paper.get_account(caller, 'b')['cash'] == 1600


def test_transfer_transaction_rolls_back_both_sides(ledger, monkeypatch):
    caller, _, _ = ledger
    create(caller, 'a'); create(caller, 'b')
    original = paper._save
    def broken(conn, account):
        if account['user_id'] == 'b':
            raise RuntimeError('injected recipient write failure')
        return original(conn, account)
    monkeypatch.setattr(paper, '_save', broken)
    with pytest.raises(RuntimeError):
        lifecycle.transfer(caller, 'a', 'b', 100, 'transfer-rollback')
    assert paper.get_account(caller, 'a')['cash'] == paper.get_account(caller, 'b')['cash'] == 1000


def test_bankruptcy_allows_receiving_but_only_explicit_reset_unfreezes(ledger):
    caller, _, _ = ledger
    create(caller, 'a'); create(caller, 'b')
    paper.admin_update('qqbot', 'a', paper.AccountUpdate(cash=-7))
    assert paper.get_account(caller, 'a')['account_status'] == 'bankrupt'
    lifecycle.transfer(caller, 'b', 'a', 100, 'help-bankrupt-user')
    assert paper.get_account(caller, 'a')['account_status'] == 'bankrupt'
    for action in ('borrow', 'repay'):
        with pytest.raises(ValueError, match='bankrupt'):
            paper.bank(caller, 'a', action, 1, 'frozen-' + action)
    with pytest.raises(ValueError, match='bankrupt'):
        lifecycle.transfer(caller, 'a', 'b', 1, 'frozen-transfer')
    reset = lifecycle.reset(caller, 'a', 'explicit-reset-a')
    assert reset['account']['cash'] == 1000
    assert reset['reset_count'] == reset['account_epoch'] == 1
    assert lifecycle.reset(caller, 'a', 'explicit-reset-a') == reset
    assert paper.get_account(caller, 'a')['performance_pnl'] == 0
    with pytest.raises(ValueError):
        lifecycle.reset(caller, 'a', 'not-bankrupt-reset')
    assert any(e['kind'] == 'transfer_in' for e in paper.history(caller, 'a'))


def test_daily_interest_negative_cash_replay_and_no_compounding(ledger, monkeypatch):
    caller, _, _ = ledger
    clock = [datetime.fromisoformat('2026-10-10T00:00:00+08:00').timestamp()]
    monkeypatch.setattr(lifecycle.time, 'time', lambda: clock[0])
    lifecycle.update_settings(paper.PaperSettings(initial_balance=1000, loan_limit=5000, daily_interest_rate=.01))
    create(caller, 'a')
    paper.bank(caller, 'a', 'borrow', 1000, 'interest-borrow-a')
    paper.admin_update('qqbot', 'a', paper.AccountUpdate(cash=3))
    clock[0] += 86400
    lifecycle.maintenance_finances()
    result = paper.get_account(caller, 'a')
    assert result['cash'] == -7 and result['account_status'] == 'bankrupt'
    lifecycle.maintenance_finances()
    assert paper.get_account(caller, 'a')['cash'] == -7
    clock[0] += 2 * 86400
    lifecycle.maintenance_finances()
    result = paper.get_account(caller, 'a')
    assert result['cash'] == -27 and result['debt'] == 1000
    assert result['interest_paid'] == 30


def test_first_day_and_midday_rate_change(ledger, monkeypatch):
    caller, _, _ = ledger
    clock = [datetime.fromisoformat('2026-10-10T00:00:00+08:00').timestamp()]
    monkeypatch.setattr(lifecycle.time, 'time', lambda: clock[0])
    lifecycle.update_settings(paper.PaperSettings(initial_balance=1000, loan_limit=5000, daily_interest_rate=.01))
    create(caller, 'a')
    clock[0] += 6 * 3600
    paper.bank(caller, 'a', 'borrow', 1000, 'partial-day-borrow')
    clock[0] += 6 * 3600
    lifecycle.update_settings(paper.PaperSettings(initial_balance=1000, loan_limit=5000, daily_interest_rate=.02))
    clock[0] += 12 * 3600
    lifecycle.maintenance_finances()
    assert paper.get_account(caller, 'a')['interest_paid'] == 12.5


def test_reset_ends_old_orders_and_rejects_old_execution_epoch(ledger):
    from backend.mcp import paper_orders
    caller, ticker, _ = ledger
    create(caller, 'a')
    order = paper_orders.place(caller, 'a', 'spot', 'BTC/USDT', 'buy', 'limit', 'old-limit-order', amount=1, price=50)
    paper.admin_update('qqbot', 'a', paper.AccountUpdate(debt=5000))
    ticker.update(price=40, ask=40, bid=40)
    paper.reconcile()
    assert paper_orders.get_orders(caller, 'a')['orders'][0]['state'] == 'open'
    reset = lifecycle.reset(caller, 'a', 'reset-old-orders')
    assert reset['account']['reserved_cash'] == 0 and reset['account']['positions'] == []
    assert paper_orders.get_orders(caller, 'a')['orders'] == []
    token = lifecycle.execution_epoch.set(0)
    try:
        with pytest.raises(ValueError, match='cycle changed'):
            paper.bank(caller, 'a', 'borrow', 1, 'stale-cycle-action')
    finally:
        lifecycle.execution_epoch.reset(token)


def test_current_cycle_return_and_reset_board(ledger):
    caller, ticker, _ = ledger
    create(caller, 'a')
    paper.admin_update('qqbot', 'a', paper.AccountUpdate(cash=0))
    lifecycle.reset(caller, 'a', 'rank-reset-a')
    paper.trade(caller, 'a', 'spot', 'BTC/USDT', 'buy', 1, 'rank-new-entry')
    ticker.update(price=300, bid=300, ask=300)
    paper.trade(caller, 'a', 'spot', 'BTC/USDT', 'sell', 1, 'rank-new-exit')
    ranking = paper_competition.leaderboard(caller)
    assert ranking['items'][0]['return_pct'] == pytest.approx(19.96)
    assert paper_competition.leaderboard(caller, metric='reset_count')['items'][0]['reset_count'] == 1
    with pytest.raises(ValueError, match='return_pct or reset_count'):
        paper_competition.leaderboard(caller, metric='pnl')


def test_bot_transfers_both_directions_and_cannot_reset(ledger):
    caller, _, _ = ledger
    create(caller, 'a')
    paper_competition.ensure_bot_namespace('qqbot')
    bot = paper.settings()['bot_user_id']
    lifecycle.transfer(caller, 'a', bot, 25, 'user-gives-to-bot')
    lifecycle.transfer(caller, bot, 'a', 50, 'bot-helps-user-a')
    assert paper.get_account(caller, 'a')['cash'] == 1025
    paper.admin_update('qqbot', bot, paper.AccountUpdate(cash=-1))
    with pytest.raises(ValueError, match='bankrupt user'):
        lifecycle.reset(caller, bot, 'bot-cannot-reset')


def test_bank_disable_blocks_new_loans_without_erasing_debt(ledger):
    caller, _, _ = ledger
    create(caller, 'a')
    paper.bank(caller, 'a', 'borrow', 100, 'before-bank-disabled')
    config = paper.settings()
    lifecycle.update_settings(paper.PaperSettings(**{**config, 'bank_enabled': False, 'loan_limit': 1234}))
    with pytest.raises(ValueError, match='borrowing is disabled'):
        paper.bank(caller, 'a', 'borrow', 1, 'after-bank-disabled')
    assert paper.get_account(caller, 'a')['loan_limit'] == 1234
    assert paper.bank(caller, 'a', 'repay', 100, 'repay-bank-disabled')['account']['debt'] == 0


def test_migrations_can_be_reapplied_without_changing_lifecycle_rows(checkpoint_url):
    import psycopg
    from backend.storage.migrate import migrate, load_migrations
    migrate(dsn=checkpoint_url)
    migrate(dsn=checkpoint_url)
    with psycopg.connect(checkpoint_url) as conn:
        assert conn.execute('SELECT COUNT(*) FROM schema_migrations').fetchone()[0] == len(load_migrations())
        assert conn.execute('SELECT COUNT(*) FROM paper_transfers').fetchone()[0] == 0


def test_reset_during_a_legacy_quote_cannot_trade_new_funds(ledger, monkeypatch):
    from backend.mcp import paper_orders
    caller, _, _ = ledger
    create(caller, 'a')
    get_quote = paper_orders.get_quote
    def delayed_quote(*args, **kwargs):
        quote = get_quote(*args, **kwargs)
        paper.admin_update('qqbot', 'a', paper.AccountUpdate(cash=-1))
        lifecycle.reset(caller, 'a', 'reset-during-quote')
        return quote
    monkeypatch.setattr(paper_orders, 'get_quote', delayed_quote)
    with pytest.raises(ValueError, match='cycle changed'):
        paper.trade(caller, 'a', 'spot', 'BTC/USDT', 'buy', 1, 'old-quote-intent')
    result = paper.get_account(caller, 'a')
    assert result['cash'] == 1000 and result['spot'] == [] and result['account_epoch'] == 1


def test_import_records_a_rate_revision_in_the_same_transaction(ledger):
    from backend.mcp.transfer import import_settings, export_settings
    caller, _, _ = ledger
    payload = export_settings(include_secrets=False)
    payload['paper_settings']['daily_interest_rate'] = .03
    import_settings(payload)
    with store.connection() as conn:
        assert conn.execute('SELECT daily_rate FROM paper_bank_rates ORDER BY revision DESC LIMIT 1').fetchone()[0] == Decimal('.03')
    assert paper.settings()['daily_interest_rate'] == .03


def test_complete_lifecycle_from_borrowing_to_new_cycle_rankings(ledger, monkeypatch):
    from backend.mcp import paper_orders, paper_batch
    caller, ticker, _ = ledger
    clock = [datetime.fromisoformat('2026-10-10T00:00:00+08:00').timestamp()]
    monkeypatch.setattr(lifecycle.time, 'time', lambda: clock[0])
    lifecycle.update_settings(paper.PaperSettings(initial_balance=1000, loan_limit=5000, daily_interest_rate=.01))
    create(caller, 'a')
    paper_competition.ensure_bot_namespace('qqbot')
    bot = paper.settings()['bot_user_id']
    paper.bank(caller, 'a', 'borrow', 1000, 'flow-borrow')
    paper.trade(caller, 'a', 'spot', 'BTC/USDT', 'buy', 10, 'flow-old-entry')
    old = paper_orders.place(caller, 'a', 'spot', 'BTC/USDT', 'sell', 'limit', 'flow-old-exit', amount=10, price=300)
    transfer = lifecycle.transfer(caller, 'a', bot, 996, 'flow-a-to-bot')
    assert paper.get_account(caller, 'a')['cash'] == 3
    clock[0] += 86400
    lifecycle.maintenance_finances()
    assert paper.get_account(caller, 'a')['cash'] == -7
    ticker.update(price=300, ask=300, bid=300)
    paper.reconcile()
    assert paper_orders.get_orders(caller, 'a')['orders'][0]['state'] == 'open'
    frozen = paper_batch.execute(caller, 'a', {'contract_version': 3, 'actions': [
        {'action': 'cancel', 'order_id': old['order']['order_id'], 'reason': 'blocked while bankrupt'}]}, 'flow-frozen-batch')
    assert frozen['results'][0]['status'] == 'failed'
    lifecycle.transfer(caller, bot, 'a', 100, 'flow-bot-to-a')
    assert paper.get_account(caller, 'a')['account_status'] == 'bankrupt'
    lifecycle.reset(caller, 'a', 'flow-user-says-xiahai')
    assert lifecycle.transfer(caller, 'a', bot, 996, 'flow-a-to-bot') == transfer
    assert paper.get_account(caller, 'a')['cash'] == 1000
    assert paper.get_account(caller, 'a')['debt'] == 0
    ticker.update(price=100, ask=100, bid=100)
    paper.trade(caller, 'a', 'spot', 'BTC/USDT', 'buy', 1, 'flow-new-entry')
    ticker.update(price=300, ask=300, bid=300)
    paper.trade(caller, 'a', 'spot', 'BTC/USDT', 'sell', 1, 'flow-new-exit')
    board = paper_competition.leaderboard(caller, user_id='a')
    assert board['self']['return_pct'] == pytest.approx(19.96)
    assert paper_competition.leaderboard(caller, metric='reset_count', user_id='a')['self']['reset_count'] == 1
    kinds = {event['kind'] for event in paper.history(caller, 'a')}
    assert {'transfer_in', 'transfer_out', 'interest', 'reset'} <= kinds


def test_both_boards_keep_privacy_and_incomplete_valuation_rules(ledger):
    caller, _, _ = ledger
    create(caller, 'a'); create(caller, 'hidden')
    paper_competition.preferences(caller, 'hidden', 'hide-self', leaderboard_visible=False)
    with store.connection() as conn:
        account = paper._get(conn, 'qqbot', 'a')
        account['spot'] = {'BTC/USDT': {'amount': 1, 'entry_price': 100, 'symbol': 'BTC/USDT', 'exchange': 'okx'}}
        account['reset_count'] = 2
        paper._save(conn, account)
        conn.commit()
    assert paper_competition.leaderboard(caller)['items'] == []
    board = paper_competition.leaderboard(caller, metric='reset_count')
    assert board['total'] == 1 and board['items'][0]['reset_count'] == 2
    assert board['items'][0]['user_id'] is None


def test_context_exposes_real_bankruptcy_policy_and_current_cycle(ledger):
    from backend.mcp.canonical_tools import context
    caller, _, _ = ledger
    create(caller, 'a')
    paper.admin_update('qqbot', 'a', paper.AccountUpdate(cash=-1))
    result = context(caller, 'a')
    assert result['account_capabilities']['account_status'] == 'bankrupt'
    assert result['account_capabilities']['account_epoch'] == 0
    assert result['allowed_actions'] == []
    assert result['account_rules']['bankrupt_can_receive']
    assert not result['account_rules']['bankrupt_can_send']


def test_concurrent_rate_commit_cannot_be_skipped_by_settlement(ledger, monkeypatch):
    import json
    from threading import Event
    caller, _, _ = ledger
    clock = [datetime.fromisoformat('2026-10-10T00:00:00+08:00').timestamp()]
    monkeypatch.setattr(lifecycle.time, 'time', lambda: clock[0])
    lifecycle.update_settings(paper.PaperSettings(initial_balance=1000, loan_limit=5000, daily_interest_rate=.01))
    create(caller, 'a')
    paper.bank(caller, 'a', 'borrow', 1000, 'concurrent-rate-borrow')
    clock[0] += 12 * 3600
    entered, release = Event(), Event()
    def save_rate():
        with store.connection() as conn:
            config = {**paper.settings(conn), 'daily_interest_rate': .02}
            lifecycle.record_rate_change(conn, config)
            conn.execute("UPDATE mcp_records SET payload=%s WHERE kind='paper_settings' AND id='global'", (json.dumps(config),))
            entered.set()
            assert release.wait(10)
            conn.commit()
    with ThreadPoolExecutor(2) as pool:
        edit = pool.submit(save_rate)
        assert entered.wait(10)
        clock[0] += 12 * 3600
        settled = pool.submit(lifecycle.maintenance_finances)
        try:
            assert not settled.done()
        finally:
            release.set()
        edit.result(timeout=10)
        settled.result(timeout=10)
    assert paper.get_account(caller, 'a')['interest_paid'] == 15


def test_populated_preupgrade_schema_preserves_cash_receipts_and_original_manager(financial_pg):
    import json
    import psycopg
    from psycopg import sql
    from uuid import uuid4
    from backend.storage.migrate import load_migrations
    schema = 'lifecycle_upgrade_' + uuid4().hex
    with psycopg.connect(financial_pg) as conn:
        # Roll back the entire temporary predecessor schema after assertions.
        with conn.transaction(force_rollback=True):
            conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
            conn.execute(sql.SQL('SET LOCAL search_path TO {}').format(sql.Identifier(schema)))
            migrations = load_migrations()
            for migration in migrations[:6]:
                conn.execute(migration.sql)
            account = json.dumps({'cash': 1234, 'debt': 100, 'namespace': 'old', 'user_id': 'a'})
            receipt = json.dumps({'status': 'submitted', 'order_id': 'original-order'})
            conn.execute('INSERT INTO paper_accounts(namespace,user_id,payload) VALUES(%s,%s,%s)', ('old', 'a', account))
            conn.execute('INSERT INTO paper_operations(namespace,user_id,operation_id,fingerprint,principal,result) VALUES(%s,%s,%s,%s,%s,%s)',
                         ('old', 'a', 'original', 'unchanged', 'private-principal', receipt))
            conn.execute("INSERT INTO visible_management_claims(claim_id,physical_account,market_type,resource,side,owner,state,version,payload) VALUES('legacy','physical','swap','ETH/USDT:USDT','LONG','mcp:connection-1791515539173','owned',7,'{}')")
            conn.execute("INSERT INTO management_execution_scopes(physical_account,market_type,legacy_scope) VALUES('physical','swap','original-scope')")
            for migration in migrations[6:]:
                conn.execute(migration.sql)
            assert conn.execute('SELECT payload FROM paper_accounts').fetchone()[0] == account
            assert conn.execute('SELECT principal,result FROM paper_operations').fetchone() == ('private-principal', receipt)
            assert conn.execute('SELECT owner,version FROM visible_management_claims').fetchone() == ('mcp:connection-1791515539173', 7)
            assert conn.execute('SELECT actor FROM management_shared_participants').fetchone()[0] == 'mcp:connection-1791515539173'
            assert conn.execute('SELECT legacy_scope FROM management_shared_accounts').fetchone()[0] == 'original-scope'
