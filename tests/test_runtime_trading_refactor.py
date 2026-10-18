"""Focused offline regressions for the agreed shared-management behavior."""
from copy import deepcopy
from pathlib import Path
import json

import pytest

from test_exclusive_position_management import managed, make_service, plan, SYMBOL
from test_position_protection import service, entry
from backend import database
from backend.utils.position_management import ensure_managed
from backend.utils.management_policy import normalize_management


@pytest.mark.parametrize('policy', ['strict_owned', 'exclusive_scope', 'visible_first_claim'])
def test_historical_policies_manage_external_changes(managed, policy):
    original, exchange = managed
    runtime = deepcopy(original.mt.runtime_config)
    runtime['position_management_policy'] = policy
    normalized = normalize_management(runtime)
    assert normalized['position_management_policy'] == 'visible_first_claim'
    assert normalized['management_scopes'] == []
    original.mt.runtime_config = runtime
    ensure_managed(original, SYMBOL)
    exchange.external_fill(.01)
    result = original.close(SYMBOL, 'LONG', .001, 'market', operation_id='after-manual')
    assert result['status'] in {'closed', 'completed'}
    assert exchange.quantities['LONG'] == pytest.approx(.029)


def test_external_full_close_keeps_entry_and_exit_orders(managed):
    svc, exchange = managed
    exchange.external_order('entry', role='entry', amount=.01, price=2385)
    exchange.external_order('sl', channel='algo', price=None, trigger=2458)
    ensure_managed(svc, SYMBOL)
    exchange.external_fill(-.02)
    ensure_managed(svc, SYMBOL)
    svc.reconcile_all()
    exchange.now += 3000
    svc.reconcile_all()
    assert exchange.writes == []
    assert all(row['status'] == 'open' for row in exchange.orders.values())
    assert plan(svc)['position_observation']['amount'] == 0
    assert plan(svc)['state'] == 'WAITING'
    # The next Agent can still decide to cancel the observed target.
    svc.cancel(SYMBOL, 'regular:entry', operation_id='explicit-cancel')
    assert exchange.orders['regular', 'entry']['status'] == 'canceled'
    assert exchange.orders['algo', 'sl']['status'] == 'open'


def test_same_quantity_reopen_preserves_orders_and_original_identity(managed):
    svc, exchange = managed
    exchange.external_order('old-all', channel='algo', price=None, trigger=2458, dynamic=True)
    ensure_managed(svc, SYMBOL)
    previous = plan(svc)['episode_id']
    exchange.external_fill(-.02)
    exchange.external_fill(.02)
    ensure_managed(svc, SYMBOL)
    current = plan(svc)
    assert current['episode_id'] != previous
    assert current['management']['contracts'] == pytest.approx(.02)
    retained = next(row for row in current['exits'] if row['id'] == 'old-all')
    assert retained['origin_episode_id'] == previous
    svc.reconcile_all()
    assert exchange.writes == []
    assert exchange.orders['algo', 'old-all']['status'] == 'open'
    with database.get_db_conn() as conn:
        assert conn.execute('SELECT COUNT(*) FROM execution_fills').fetchone()[0] == 2
        history = conn.execute('SELECT payload FROM trade_cycle_evidence WHERE episode_id=%s', (previous,)).fetchone()
        assert json.loads(history[0])['state'] == 'DONE'


def test_explicit_agent_full_close_retains_its_cleanup(managed):
    svc, exchange = managed
    exchange.external_order('entry', role='entry', amount=.01, price=2385)
    exchange.external_order('sl', channel='algo', price=None, trigger=2458)
    ensure_managed(svc, SYMBOL)
    result = svc.close(SYMBOL, 'LONG', None, 'market', close_all=True,
                       contract_version=2, operation_id='agent-full-close')
    assert result['status'] == 'completed'
    exchange.now += 3000
    svc.reconcile_all()
    assert exchange.orders['regular', 'entry']['status'] == 'canceled'
    assert exchange.orders['algo', 'sl']['status'] == 'canceled'
    assert plan(svc)['state'] == 'DONE'


def test_partial_exits_do_not_infer_cleanup_from_external_baseline():
    from backend.utils.independent_lifecycle import cleanup_authorized
    current = {'entries': [{'filled': 1}], 'management': {'contracts': 2},
               'exits': [{'source': 'agent', 'quantity_mode': 'fixed',
                          'cleanup_on_fill': False, 'filled': 1, 'status': 'closed',
                          'fill_verified': True}]}
    assert not cleanup_authorized(current)
    # The same legacy partial fills cannot prove an instruction to cancel.
    current['exits'][0].pop('cleanup_on_fill')
    assert not cleanup_authorized(current)
    current['exits'][0]['quantity_mode'] = 'all'
    assert cleanup_authorized(current)
    current['exits'][0]['source'] = 'external'
    assert not cleanup_authorized(current)


@pytest.mark.parametrize('external_change', ['cancel', 'price'])
def test_attached_required_maintains_saved_target(service, external_change):
    svc, exchange = service
    svc.mt.runtime_config = {'mode': 'REAL', 'exit_mode': 'attached_required'}
    # This fixture has no shared account identity; use its established local
    # service scope while exercising the actual attached maintenance engine.
    svc.mt.runtime_config.pop('mode')
    opened = svc.open('ETH/USDT', entry())
    exchange.orders[opened['id']].update(status='closed', filled=1)
    exchange.quantity = 1
    svc.reconcile_all()
    current = svc._load(SYMBOL, 'LONG')
    old = next(row for row in current['legs'] if row['kind'] == 'sl')
    if external_change == 'cancel':
        exchange.orders[old['id']]['status'] = 'canceled'
    else:
        exchange.orders[old['id']]['stopLossPrice'] = 80
    svc.reconcile_all()
    saved = svc._load(SYMBOL, 'LONG')
    assert saved['stop_loss'] == 90
    assert exchange.orders[old['id']]['status'] == 'canceled'
    active = [row for row in saved['legs'] if row['kind'] == 'sl' and row['status'] == 'open']
    assert len(active) == 1 and active[0]['trigger_price'] == 90
    # A successful system update supersedes the previous desired price.
    svc.adjust(SYMBOL, 'LONG', sl=95)
    assert svc._load(SYMBOL, 'LONG')['stop_loss'] == 95
    svc.adjust(SYMBOL, 'LONG', clear_sl=True)
    assert svc._load(SYMBOL, 'LONG')['stop_loss'] is None


def test_legacy_adoption_uses_shared_management_without_clearing_orders(managed):
    from backend.utils.position_adoption import adopt_position
    svc, exchange = managed
    svc.mt.runtime_config['position_management_policy'] = 'strict_owned'
    exchange.external_order('manual-sl', channel='algo', price=None, trigger=2458)
    result = adopt_position(svc.mt, SYMBOL, 'LONG', .02, 'legacy call', 'legacy-adopt')
    replay = adopt_position(svc.mt, SYMBOL, 'LONG', .02, 'legacy call', 'legacy-adopt')
    assert result == replay and result['status'] == 'completed'
    assert exchange.writes == []
    assert 'adoption' not in plan(svc)


def test_migration_preserves_execution_and_other_configuration_fields(financial_pg):
    migration = Path(__file__).parents[1] / 'backend/storage/migrations/0011_shared_policy_normalization.sql'
    original = {'config_id': 'old', 'symbol': SYMBOL, 'mode': 'REAL',
                'position_management_policy': 'strict_owned', 'initial_cost': 123.45}
    with database.get_db_conn() as conn:
        conn.execute('INSERT INTO agent_configs(config_id,symbol,mode,data_json,updated_at) VALUES(%s,%s,%s,%s,%s)',
                     ('old', SYMBOL, 'REAL', json.dumps(original), 'unchanged'))
        conn.execute('INSERT INTO mcp_records(kind,id,payload) VALUES(%s,%s,%s)',
                     ('profile', 'old', json.dumps({'position_management_policy': 'strict_owned', 'spot_allowance': 77})))
        conn.execute(migration.read_text(encoding='utf-8'))
        row = conn.execute('SELECT data_json,updated_at FROM agent_configs WHERE config_id=%s', ('old',)).fetchone()
        converted = json.loads(row[0])
        assert converted['position_management_policy'] == 'visible_first_claim'
        assert converted['initial_cost'] == 123.45 and row[1] == 'unchanged'
        profile = json.loads(conn.execute("SELECT payload FROM mcp_records WHERE kind='profile' AND id='old'").fetchone()[0])
        assert profile['position_management_policy'] == 'visible_first_claim'
        assert profile['spot_allowance'] == 77
        conn.commit()


def test_each_entry_checks_current_leverage_once(managed, monkeypatch):
    from backend.agent.agent_models import OpenOrderStrategy
    from backend.mcp import guard, settings
    _, exchange = managed
    svc = make_service(exchange, owner='mcp:guard-test', policy='strict_owned')
    profile = {**svc.mt.runtime_config, 'enabled': True, 'mcp_max_leverage': 5, 'leverage': 1}
    monkeypatch.setattr(settings, 'get_runtime_profile', lambda _: profile)
    monkeypatch.setattr(settings, 'settings', lambda: {'enabled': True})
    leverage, reads = [1], []

    def fetch_leverage(symbol):
        reads.append(symbol)
        return {'longLeverage': leverage[0], 'shortLeverage': leverage[0]}

    monkeypatch.setattr(exchange, 'fetch_leverage', fetch_leverage)
    order = OpenOrderStrategy(action='BUY_LIMIT', entry_price=2385, amount=.001, reason='entry')
    guard.preflight_tool('open', {'side': 'LONG'}, svc.config_id, SYMBOL)
    svc.open(SYMBOL, order, operation_id='entry-one')
    assert len(reads) == 1
    writes = len(exchange.writes)
    leverage[0] = 10
    guard.preflight_tool('open', {'side': 'LONG'}, svc.config_id, SYMBOL)
    with pytest.raises(ValueError, match='exceeds MCP limit'):
        svc.open(SYMBOL, order, operation_id='entry-two')
    assert len(reads) == 2 and len(exchange.writes) == writes
