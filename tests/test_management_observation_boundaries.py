"""Complete exchange observations keep ambiguous history and current exposure separate."""
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from backend import database
from backend.mcp import service as gateway
from backend.utils import position_management as management
from test_exclusive_position_management import managed, plan, SYMBOL


def test_same_time_ambiguous_boundary_keeps_current_position_manageable(managed):
    manager, exchange = managed
    exchange.external_order('old-all', channel='algo', trigger=2458, price=None, dynamic=True)
    management.ensure_managed(manager, SYMBOL)
    original = plan(manager)['episode_id']
    exchange.external_fill(-.02, trade_id='manual-close', advance=100)
    exchange.external_fill(.01, trade_id='manual-reopen', advance=0)
    management.ensure_managed(manager, SYMBOL)
    current = plan(manager)
    assert current['episode_id'] == original
    assert current['management'].get('boundary_pending'), current['management']
    assert current['position_observation']['amount'] == pytest.approx(.01)
    assert manager.snapshot(SYMBOL)['exits'][0]['coverage_estimate'] is None
    assert not exchange.writes
    # An uncertain history boundary does not authorize extra risk, but the
    # actual verified remaining position can still be reduced close-only.
    result = manager.close(SYMBOL, 'LONG', .001, 'market', operation_id='reduce-ambiguous')
    assert result['status'] in {'completed', 'closed'}
    assert exchange.quantities['LONG'] == pytest.approx(.009)


def test_numeric_exchange_sequence_orders_same_millisecond_boundary(managed):
    manager, exchange = managed
    management.ensure_managed(manager, SYMBOL)
    original = plan(manager)['episode_id']
    exchange.external_fill(-.02, trade_id='9', advance=100)
    exchange.external_fill(.01, trade_id='10', advance=0)
    management.ensure_managed(manager, SYMBOL)
    assert plan(manager)['episode_id'] != original, plan(manager)['management']
    assert plan(manager)['management']['contracts'] == pytest.approx(.01)
    assert not exchange.writes


def test_external_fill_source_survives_repeated_observation(managed):
    manager, exchange = managed
    management.ensure_managed(manager, SYMBOL)
    exchange.external_fill(.01)
    management.ensure_managed(manager, SYMBOL)
    management.ensure_managed(manager, SYMBOL)
    with database.get_db_conn() as conn:
        links = list(conn.execute('SELECT payload FROM execution_order_links'))
        facts = list(conn.execute("SELECT payload FROM position_management_facts WHERE identity LIKE 'trade:%'"))
    assert all(json.loads(r['payload'])['source'] == 'external' for r in links)
    assert all(json.loads(r['payload'])['source'] == 'external' for r in facts)


def test_external_enrolled_entry_fill_keeps_external_source(managed):
    manager, exchange = managed
    exchange.external_order('entry', role='entry', price=2385, amount=.01)
    management.ensure_managed(manager, SYMBOL)
    exchange.external_fill(.01, order_id='entry')
    management.ensure_managed(manager, SYMBOL)
    with database.get_db_conn() as conn:
        link = conn.execute("SELECT payload FROM execution_order_links WHERE order_id='entry'").fetchone()
        fact = conn.execute("SELECT payload FROM position_management_facts WHERE identity LIKE 'trade:%'").fetchone()
    assert json.loads(link['payload'])['source'] == 'external'
    assert json.loads(fact['payload'])['source'] == 'external'


def test_external_conditional_child_is_associated_before_enrollment(managed):
    manager, exchange = managed
    parent = exchange.external_order('parent', channel='algo', trigger=2458, price=2450,
                                     kind='stop_limit', amount=.02)
    management.ensure_managed(manager, SYMBOL)
    parent['status'] = 'closed'
    parent['info'].update(actualOrderId='child', ordId='child', algoStatus='TRIGGERED')
    child = exchange.external_order('child', price=2450, amount=.02)
    # Child has a distinct exchange/client identity, not the parent's client ID.
    management.ensure_managed(manager, SYMBOL)
    records = plan(manager)['exits']
    assert len(records) == 1
    assert records[0]['id'] == 'parent' and records[0]['execution_order_id'] == 'child'
    assert records[0]['order_type'] == 'stop_limit'
    assert manager.snapshot(SYMBOL)['exits'][0]['order_id'] == 'regular:child'
    child['price'] = 2451
    management.ensure_managed(manager, SYMBOL)
    assert plan(manager)['exits'][0]['price'] == 2451
    assert not exchange.writes


def test_concurrent_callers_share_only_complete_same_owner_observation(managed, monkeypatch):
    manager, _ = managed
    entered, release = threading.Event(), threading.Event()
    calls = []
    original = management._discover

    def discover(*args):
        calls.append(args)
        entered.set()
        assert release.wait(2)
        return original(*args)

    monkeypatch.setattr(management, '_discover', discover)
    with ThreadPoolExecutor(max_workers=2) as workers:
        first = workers.submit(management.ensure_managed, manager, SYMBOL)
        assert entered.wait(2)
        # Wait until the second caller has actually attempted the shared lock.
        second_entered = threading.Event()
        original_remaining = __import__('backend.mcp.resilience', fromlist=['remaining']).remaining
        def remaining(*args, **kwargs):
            second_entered.set()
            return original_remaining(*args, **kwargs)
        monkeypatch.setattr('backend.mcp.resilience.remaining', remaining)
        second = workers.submit(management.ensure_managed, manager, SYMBOL)
        assert second_entered.wait(2)
        release.set()
        first.result(timeout=5)
        second.result(timeout=5)
    assert len(calls) == 1
    assert plan(manager)['management']['sync_status'] == 'verified'


def test_optional_historical_mapping_failure_preserves_confirmed_ack(monkeypatch):
    def unavailable(**kwargs):
        raise TimeoutError('mapping database locked')
    monkeypatch.setattr(gateway.store, 'connection', unavailable)
    value = gateway._external_operation_receipt({'status': 'submitted', 'operation_id': 'stable-1',
                                                'order_id': 'regular:accepted'},
                                               'stable-1', 'original-1', 'profile', principal='caller')
    assert value['status'] == 'submitted'
    assert value['operation_id'] == 'original-1'
    assert value['order_id'] == 'regular:accepted'
