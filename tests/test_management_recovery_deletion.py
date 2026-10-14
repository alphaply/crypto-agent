"""Accepted recovery responsibility survives deletion of its original task."""
from copy import deepcopy
import json

import pytest

from backend import config_store, database
from backend.agent import agent_tools
from backend.agent.unified_trading import execute_v3_actions
from backend.app.services import config_service
from backend.config import Config, config
from backend.utils import visible_management as management
from backend.utils.trade_operations import run_once
from backend.utils.execution_ledger import register_order
from test_exclusive_position_management import ExclusiveExchange, make_service


SYMBOL = 'ETH/USDT:USDT'


@pytest.fixture
def deletion_runtime(tmp_path, monkeypatch):
    path = tmp_path / 'recovery-delete.sqlite'
    monkeypatch.setattr(database, 'DB_NAME', str(path))
    monkeypatch.setattr(config_store, 'DB_NAME', path)
    monkeypatch.setattr(config_store, 'load_dotenv', lambda *args, **kwargs: None)
    monkeypatch.setenv('CONFIG_MASTER_KEY', 'offline-recovery-delete-key')
    database.init_db()
    profile = {'profile_id': 'offline-account', 'exchange': 'binance', 'market_type': 'swap',
               'api_key': ExclusiveExchange.apiKey, 'secret': 'offline-secret'}
    agents = [{'config_id': owner, 'mode': 'REAL', 'symbol': SYMBOL, 'symbols': [SYMBOL],
               'enabled': True, 'exit_mode': 'attached_optional', 'position_management_policy': 'visible_first_claim',
               'exchange_profile_id': profile['profile_id']} for owner in ('owner', 'receiver')]
    config_store.save_runtime_snapshot({'enable_scheduler': False}, agents,
                                       exchange_profiles_payload=[profile], validate_snapshot=Config.validate_snapshot)
    runtime = Config()
    monkeypatch.setattr(config_service, 'global_config', runtime)
    monkeypatch.setattr(config, 'get_config_by_id', runtime.get_config_by_id)
    venue = ExclusiveExchange()
    source = make_service(venue, 'owner', policy='visible_first_claim')
    receiver = make_service(venue, 'receiver', policy='visible_first_claim')
    source.mt.runtime_config = runtime.get_config_by_id('owner')
    receiver.mt.runtime_config = runtime.get_config_by_id('receiver')
    monkeypatch.setattr(agent_tools, 'MarketTool', lambda config_id: source.mt if config_id == 'owner' else receiver.mt)
    monkeypatch.setattr('backend.utils.market_data.MarketTool', lambda config_id: source.mt if config_id == 'owner' else receiver.mt)
    return runtime, source, receiver, venue


@pytest.mark.parametrize('grant_kind', ['parent', 'child'])
def test_accepted_unknown_parent_survives_actual_source_task_delete(deletion_runtime, monkeypatch, grant_kind):
    runtime, source, receiver, venue = deletion_runtime
    payload = {'contract_version': 3, 'actions': [
        {'action': 'close', 'side': 'LONG', 'amount': .01, 'order_type': 'limit',
         'price': 2565, 'reason': 'preserve original identity'}]}
    venue.timeout_create = True
    original = execute_v3_actions(payload, config_id='owner', symbol=SYMBOL, operation_id='original-parent')
    assert original['status'] == 'unknown', original
    child_id = original['results'][0]['operation_id']
    scope = source.account_scope
    # A late observed fill retains its original actor even before the lost ACK
    # is reconciled. Its cycle history must not become the receiver's history.
    fill = venue.external_fill(-.005, side='LONG', order_id='managed-1')
    register_order(scope, SYMBOL, 'managed-1', 'owner', 'exit', refresh=False,
                   operation_id=child_id, episode_id='original-cycle')
    register_order(scope, SYMBOL, 'unrelated-order', 'owner', 'exit', refresh=False,
                   operation_id='unrelated-private', episode_id='private-cycle')
    with database.get_db_conn() as conn:
        conn.execute('INSERT INTO execution_fills VALUES(?,?,?,?,?,?,?)',
                     (scope, SYMBOL, fill['id'], fill['order'], 'owner', fill['timestamp'], json.dumps(fill)))
        conn.execute('INSERT INTO execution_episodes VALUES(?,?,?,?,?)',
                     ('original-cycle', scope, 'owner', SYMBOL, json.dumps({'side': 'LONG'})))
        conn.execute('INSERT INTO execution_episodes VALUES(?,?,?,?,?)',
                     ('private-cycle', scope, 'owner', SYMBOL, json.dumps({'side': 'SHORT'})))
        conn.execute('INSERT INTO execution_position_history(position_id,account_scope,config_id,symbol,payload) VALUES(?,?,?,?,?)',
                     ('original-history', scope, 'owner', SYMBOL, json.dumps({'plan_episode_id': 'original-cycle'})))
        conn.execute('INSERT INTO real_protection_events(timestamp,config_id,symbol,payload) VALUES(?,?,?,?)',
                     ('offline', 'owner', SYMBOL, json.dumps({'side': 'SHORT', 'account_scope': scope})))
        conn.commit()
    claim = management.management_status('owner')['scopes'][0]
    moved = management.transfer_management_scope('owner', claim['claim_id'], 'receiver', accepted=True)
    assert moved['status'] == 'transferred', moved
    granted_id = 'original-parent' if grant_kind == 'parent' else child_id
    grant = next(g for g in moved['recovery_targets'] if g['operation_id'] == granted_id)
    with database.get_db_conn() as conn:
        # Either exact accepted identity must retain the authoritative parent /
        # child relationship, without granting access to another private call.
        conn.execute('DELETE FROM management_recovery_grants WHERE source_owner=? AND operation_id!=?',
                     ('owner', granted_id))
        conn.commit()
    run_once('owner', SYMBOL, 'unrelated-private', {'tool': 'private-history'}, lambda: {'status': 'completed'})
    with database.get_db_conn() as conn:
        original_hashes = dict(conn.execute('SELECT operation_id,request_hash FROM trade_action_runs WHERE config_id=?', ('owner',)))
    before = deepcopy(venue.writes)

    deleted = config_service.delete_config_payload('owner')
    assert deleted['exchange_orders_cancelled'] is False
    assert runtime.get_config_by_id('owner') is None
    database.init_db()  # Restart must retain the granted immutable evidence.
    def receiver_only(config_id):
        assert config_id == 'receiver', 'Recovery attempted to reuse deleted source credentials'
        return receiver.mt
    monkeypatch.setattr('backend.utils.market_data.MarketTool', receiver_only)
    result = management.query_management_recovery('receiver', grant['recovery_id'])['result']
    assert result['contract_version'] == 3, result
    child = result['results'][0] if grant_kind == 'parent' else result
    assert child['operation_id'] == child_id
    assert child['status'] in {'submitted', 'completed'}, result
    assert result['query']['status'] != 'failed', result
    assert venue.writes == before
    assert management.lookup_management_recovery('unrelated', grant['recovery_id']) is None
    with database.get_db_conn() as conn:
        remaining = dict(conn.execute('SELECT operation_id,request_hash FROM trade_action_runs WHERE config_id=?', ('owner',)))
        assert remaining == {identity: original_hashes[identity] for identity in ('original-parent', child_id)}
        assert conn.execute("SELECT COUNT(*) FROM trade_operation_requests WHERE config_id='owner'").fetchone()[0] == 2
        assert list(conn.execute("SELECT order_id,config_id FROM execution_order_links WHERE config_id='owner'").fetchone()) == ['managed-1', 'owner']
        assert list(conn.execute("SELECT trade_id,config_id FROM execution_fills WHERE config_id='owner'").fetchone()) == [fill['id'], 'owner']
        assert [row[0] for row in conn.execute("SELECT episode_id FROM execution_episodes WHERE config_id='owner'")] == ['original-cycle']
        assert conn.execute("SELECT config_id FROM execution_position_history WHERE position_id='original-history'").fetchone()[0] == 'owner'
        assert not conn.execute("SELECT 1 FROM real_protection_events WHERE config_id='owner' AND json_extract(payload,'$.side')='SHORT'").fetchone()
        assert conn.execute("SELECT 1 FROM real_protection_events WHERE config_id='owner' AND json_extract(payload,'$.side')='LONG'").fetchone()


def test_ordinary_source_task_delete_without_grants_still_purges_history(deletion_runtime):
    runtime, source, _, _ = deletion_runtime
    run_once('owner', SYMBOL, 'ordinary-history', {'tool': 'private-history'}, lambda: {'status': 'completed'})
    register_order(source.account_scope, SYMBOL, 'ordinary-order', 'owner', 'exit', refresh=False,
                   operation_id='ordinary-history')
    deleted = config_service.delete_config_payload('owner')
    assert runtime.get_config_by_id('owner') is None
    assert deleted['cleanup']['trade_action_runs_deleted'] == 1
    assert deleted['cleanup']['execution_order_links_deleted'] == 1
    assert not any('retained_for_recovery' in key for key in deleted['cleanup'])
    assert all(count == 0 for count in database.get_config_dependency_counts('owner').values())
