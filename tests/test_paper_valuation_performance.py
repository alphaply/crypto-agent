"""Current-cycle valuation and bounded ledger read regressions."""
import json

import pytest

from backend.mcp import paper, paper_competition, paper_lifecycle, paper_orders, paper_policy, paper_storage, store
from test_mcp_paper import create, ledger


@pytest.mark.parametrize('exchange', ['okx', None])
def test_reset_excludes_prior_cycle_unverified_performance_but_preserves_audit(ledger, exchange):
    caller, _, _ = ledger
    create(caller)
    with store.connection() as conn:
        account = paper._get(conn, 'qqbot', '123456')
        paper._event(conn, account, 'buy', {'fill': {'exchange': exchange, 'symbol': 'BTC/USDT', 'amount': 1}})
        account.update(account_status='bankrupt', bankruptcy_reason='insufficient_cash_for_interest', cash=-1, debt=1)
        paper._save(conn, account)
        conn.commit()
    assert paper.get_account(caller, '123456')['completeness']['valuation'] == 'incomplete'

    paper_lifecycle.reset(caller, '123456', 'reset-current-cycle-proof')
    view = paper.get_account(caller, '123456')
    assert view['completeness']['valuation'] == 'complete'
    assert view['net_equity'] == 1000
    assert view['performance_pnl'] == 0
    assert paper_competition.leaderboard(caller, user_id='123456')['self']['return_pct'] == 0
    with store.connection() as conn:
        events = conn.execute("SELECT payload FROM paper_events WHERE kind='buy'").fetchall()
    assert len(events) == 1 and json.loads(events[0][0])['fill']['exchange'] == exchange


def test_reset_still_requires_complete_current_cycle_scan(ledger, monkeypatch):
    caller, _, _ = ledger
    create(caller)
    with store.connection() as conn:
        account = paper._get(conn, 'qqbot', '123456')
        for _ in range(8):
            paper._event(conn, account, 'buy', {'fill': {'exchange': 'okx'}})
        account.update(account_status='bankrupt', bankruptcy_reason='insufficient_cash_for_interest', cash=-1, debt=1)
        paper._save(conn, account)
        conn.commit()
    paper_lifecycle.reset(caller, '123456', 'reset-before-paged-proof')
    with store.connection() as conn:
        account = paper._get(conn, 'qqbot', '123456')
        for _ in range(5):
            paper._event(conn, account, 'buy', {'fill': {'exchange': 'binance'}})
        conn.commit()
    monkeypatch.setenv('PAPER_SCAN_BATCH_SIZE', '2')
    first = paper.get_account(caller, '123456')
    assert first['completeness']['reason_codes'] == ['paper_performance_scan_incomplete']
    paper.get_account(caller, '123456')
    complete = paper.get_account(caller, '123456')
    assert complete['completeness']['valuation'] == 'complete'
    with store.connection() as conn:
        paper._event(conn, account, 'buy', {'fill': {'exchange': None}})
        conn.commit()
    incomplete = paper.get_account(caller, '123456')
    assert incomplete['completeness']['valuation'] == 'incomplete'
    assert incomplete['net_equity'] is None
    assert 'paper_historical_performance_unverified' in incomplete['completeness']['reason_codes']


def test_ledger_page_batches_metadata_without_reading_lookahead_payload(ledger):
    caller, _, _ = ledger
    create(caller)
    with store.connection() as conn:
        account = paper._get(conn, 'qqbot', '123456')
        for index in range(4):
            paper._event(conn, account, 'preferences', {'name': str(index)})
        conn.commit()

        class TrackedConnection:
            calls = 0

            def execute(self, *args, **kwargs):
                self.calls += 1
                return conn.execute(*args, **kwargs)

        tracked = TrackedConnection()
        with paper_storage.payload_scope():
            rows, progress = paper_storage.payload_page(tracked, 'paper_events', 'namespace=%s AND user_id=%s',
                                                        ('qqbot', '123456'), count=3)
        assert [row['kind'] for row in rows] == ['create', 'preferences', 'preferences']
        assert progress['has_more'] and progress['next_cursor'] == rows[-1]['id']
        # One column lookup, one bounded metadata page, and one budgeted blob
        # read per returned row. The lookahead row must never load its payload.
        assert tracked.calls == 2 + len(rows)


def test_epoch_without_durable_reset_does_not_hide_old_unverified_events(ledger):
    caller, _, _ = ledger
    create(caller)
    with store.connection() as conn:
        account = paper._get(conn, 'qqbot', '123456')
        paper._event(conn, account, 'buy', {'fill': {'exchange': 'okx'}})
        account['account_epoch'] = 1
        paper._save(conn, account)
        conn.commit()
    assert paper.get_account(caller, '123456')['completeness']['valuation'] == 'incomplete'
    paper.create_account(caller, '123456', 'create-existing-with-history')
    assert paper.get_account(caller, '123456')['net_equity'] is None


def test_new_epoch_scan_cannot_advance_a_stale_old_epoch_reader(ledger, monkeypatch):
    caller, _, _ = ledger
    create(caller)
    with store.connection() as conn:
        account = paper._get(conn, 'qqbot', '123456')
        for _ in range(5):
            paper._event(conn, account, 'buy', {'fill': {'exchange': 'binance'}})
        paper._event(conn, account, 'buy', {'fill': {'exchange': 'okx'}})
        account.update(account_status='bankrupt', bankruptcy_reason='insufficient_cash_for_interest', cash=-1, debt=1)
        paper._save(conn, account)
        conn.commit()
    monkeypatch.setenv('PAPER_SCAN_BATCH_SIZE', '2')
    paper_lifecycle.reset(caller, '123456', 'reset-concurrent-epoch-read')
    assert paper.get_account(caller, '123456')['completeness']['valuation'] == 'complete'
    # Simulate a read which loaded its old account before reset but only now
    # evaluates historical provenance. The new epoch must not skip its pages.
    with store.connection() as conn:
        assert paper_policy.performance_complete(conn, account) == (False, 'paper_performance_scan_incomplete')
        for _ in range(3):
            complete, reason = paper_policy.performance_complete(conn, account)
        assert not complete and reason == 'paper_historical_performance_unverified'
        conn.commit()
    assert paper.get_account(caller, '123456')['completeness']['valuation'] == 'complete'


def test_batched_metadata_still_detects_payload_size_change(ledger):
    caller, _, _ = ledger
    create(caller)
    with store.connection() as conn:
        class ChangedConnection:
            changed = False

            def execute(self, query, *args, **kwargs):
                result = conn.execute(query, *args, **kwargs)
                if isinstance(query, str) and ' AS payload_bytes ' in query and not self.changed:
                    self.changed = True
                    conn.execute("UPDATE paper_events SET payload=%s WHERE kind='create'", (json.dumps({'changed': 'x' * 1000}),))
                return result

        with paper_storage.payload_scope(), pytest.raises(ValueError, match='changed during bounded read'):
            paper_storage.payload_page(ChangedConnection(), 'paper_events', 'namespace=%s AND user_id=%s',
                                       ('qqbot', '123456'))


def test_admin_adjustment_commits_incremental_valuation_progress(ledger, monkeypatch):
    caller, _, _ = ledger
    create(caller)
    with store.connection() as conn:
        account = paper._get(conn, 'qqbot', '123456')
        account['spot']['BTC/USDT'] = paper_policy.stamp({
            'symbol': 'BTC/USDT', 'exchange': 'binance', 'cycle_id': 'admin-progress-cycle',
            'amount': 1, 'entry_price': 100, 'mark_price': 100, 'marked_at': 1,
        })
        for _ in range(4):
            paper._event(conn, account, 'preferences', {'name': account['name']})
        paper._save(conn, account)
        conn.commit()
    monkeypatch.setenv('PAPER_SCAN_BATCH_SIZE', '2')
    cursors = []
    for name in ('first-adjustment', 'second-adjustment'):
        view = paper.admin_update('qqbot', '123456', paper.AccountUpdate(name=name))
        assert view['completeness']['valuation'] == 'incomplete'
        with store.connection() as conn:
            saved = conn.execute("SELECT cursor FROM paper_scan_progress WHERE worker LIKE 'performance:%%' "
                                 "AND worker NOT LIKE '%%:unresolved'").fetchone()
            assert saved is not None
            cursors.append(saved[0])
    assert cursors[1] > cursors[0] > 0


def test_admin_accounts_past_last_offset_does_not_repeat_alphabetically_later_account(ledger):
    caller, _, _ = ledger
    create(caller, 'zulu')
    create(caller, 'alpha')
    assert [row['user_id'] for row in paper.admin_accounts(namespace='qqbot')['items']] == ['alpha', 'zulu']
    page = paper.admin_accounts(namespace='qqbot', offset=99)
    assert page['items'] == []
    assert page['total'] == 2
    assert page['scan_complete'] and not page['has_more']


def test_protection_preflight_retains_incomplete_origin_scan_without_placing_order(ledger, monkeypatch):
    caller, _, quotes = ledger
    create(caller)
    paper.trade(caller, '123456', 'spot', 'BTC', 'buy', 1, 'original-legacy-exposure')
    with store.connection() as conn:
        account = paper._get(conn, 'qqbot', '123456')
        for key in ('origin_version', 'origin_exchange', 'resource_id'):
            account['spot']['BTC/USDT'].pop(key, None)
        paper._save(conn, account)
        conn.commit()
    monkeypatch.setenv('PAPER_SCAN_BATCH_SIZE', '1')
    quote_count = len(quotes)
    with pytest.raises(ValueError, match='no eligible Binance exposure'):
        paper_orders.protection(caller, '123456', 'spot', 'BTC', 'LONG', 'protect-legacy-proof', stop_loss=90)
    assert len(quotes) == quote_count
    with store.connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM paper_orders').fetchone()[0] == 0
        state = conn.execute("SELECT payload FROM paper_origin_scans WHERE kind='spot'").fetchone()
        assert state is not None and not json.loads(state[0])['complete']
    result = paper_orders.protection(caller, '123456', 'spot', 'BTC', 'LONG', 'protect-legacy-proof', stop_loss=90)
    assert len(result['orders']) == 1 and result['orders'][0]['exchange'] == 'binance'
    assert result['account']['spot'][0]['amount'] == 1


def test_edit_preflight_retains_origin_progress_without_editing_unverified_order(ledger, monkeypatch):
    caller, _, quotes = ledger
    create(caller)
    placed = paper_orders.place(caller, '123456', 'spot', 'BTC', 'buy', 'limit',
                                'original-legacy-order', amount=1, price=90)
    order = dict(placed['order'])
    for key in ('origin_version', 'origin_exchange', 'resource_id'):
        order.pop(key, None)
    with store.connection() as conn:
        paper_orders.save(conn, order)
        conn.commit()
    monkeypatch.setenv('PAPER_SCAN_BATCH_SIZE', '1')
    quote_count = len(quotes)
    with pytest.raises(ValueError, match='paper_origin_unverified'):
        paper_orders.edit(caller, '123456', order['order_id'], 'edit-legacy-proof', price=85)
    assert len(quotes) == quote_count
    with store.connection() as conn:
        state = conn.execute("SELECT payload FROM paper_origin_scans WHERE kind='orders'").fetchone()
        assert state is not None and not json.loads(state[0])['complete']
        saved = paper_storage.one(conn, 'paper_orders', 'payload', 'order_id=%s', (order['order_id'],))
        assert saved['price'] == 90 and saved['reserved_cash'] == order['reserved_cash']
    result = paper_orders.edit(caller, '123456', order['order_id'], 'edit-legacy-proof', price=85)
    assert result['order']['price'] == 85 and result['order']['order_id'] == order['order_id']


def test_cancel_preflight_retains_origin_progress_and_replay_stays_original(ledger, monkeypatch):
    caller, _, quotes = ledger
    create(caller)
    paper.trade(caller, '123456', 'spot', 'ETH', 'buy', 1, 'entry-before-legacy-cancel')
    placed = paper_orders.place(caller, '123456', 'spot', 'BTC', 'buy', 'limit',
                                'original-cancel-target', amount=1, price=90)
    order = dict(placed['order'])
    for key in ('origin_version', 'origin_exchange', 'resource_id'):
        order.pop(key, None)
    with store.connection() as conn:
        paper_orders.save(conn, order)
        conn.commit()
    monkeypatch.setenv('PAPER_SCAN_BATCH_SIZE', '1')
    quote_count = len(quotes)
    for _ in range(2):
        with pytest.raises(ValueError, match='paper_origin_unverified'):
            paper_orders.cancel(caller, '123456', order['order_id'], 'cancel-legacy-proof')
        with store.connection() as conn:
            saved = paper_storage.one(conn, 'paper_orders', 'payload', 'order_id=%s', (order['order_id'],))
            assert saved['state'] == 'open' and saved['reserved_cash'] == order['reserved_cash']
    result = paper_orders.cancel(caller, '123456', order['order_id'], 'cancel-legacy-proof')
    assert result['order']['state'] == 'cancelled' and result['account']['reserved_cash'] == 0
    assert len(quotes) == quote_count
    monkeypatch.setattr(paper_orders, '_require_order_origin', lambda *args: pytest.fail('Replay must not recheck the old target'))
    assert paper_orders.cancel(caller, '123456', order['order_id'], 'cancel-legacy-proof') == result
