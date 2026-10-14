"""Exclusive authorization remains attached across config edits and imports."""
from copy import deepcopy
import hashlib
import json
import sqlite3
from pathlib import Path
import multiprocessing
import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import config_store, database
from backend.app.api.config import router as config_router
from backend.app.core.deps import get_current_user
from backend.app.services import config_service, market_catalog_service
from backend.config import Config
from backend.database_schema import initialize_schema
from backend.mcp import store, settings as mcp_settings
from backend.mcp.management import router as mcp_router
from backend.mcp.transfer import import_settings
from backend.utils.position_management import configuration_candidates, synchronize_configuration

SYMBOL = 'ETH/USDT:USDT'
SCOPE = hashlib.sha256(b'binanceusdm:test-key').hexdigest()


def task(owner='owner', side='LONG', **updates):
    return {'config_id': owner, 'symbol': SYMBOL, 'mode': 'REAL', 'enabled': True,
            'exit_mode': 'independent_exits', 'exchange_profile_id': 'account',
            'position_management_policy': 'exclusive_scope',
            'management_scopes': [{'symbol': SYMBOL, 'side': side}], **updates}


ACCOUNT = {'profile_id': 'account', 'name': 'Offline', 'exchange': 'binance', 'market_type': 'swap',
           'api_key': 'test-key', 'secret': 'test-secret'}


@pytest.fixture
def lifecycle(tmp_path, monkeypatch):
    path = tmp_path / 'config-lifecycle.sqlite'
    monkeypatch.setattr(database, 'DB_NAME', str(path))
    monkeypatch.setattr(config_store, 'DB_NAME', path)
    monkeypatch.setattr(config_store, 'load_dotenv', lambda *a, **k: None)
    monkeypatch.setenv('CONFIG_MASTER_KEY', 'config-lifecycle-test')
    with database.get_db_conn() as conn:
        initialize_schema(conn)
        store.initialize_schema(conn)
        conn.commit()
    config_store.save_runtime_snapshot({'enable_scheduler': False}, [], exchange_profiles_payload=[ACCOUNT])
    runtime = Config()
    monkeypatch.setattr(config_service, 'global_config', runtime)
    monkeypatch.setattr(market_catalog_service, 'get_market_catalog', lambda *_: ([{'symbol': SYMBOL}], False))
    monkeypatch.setattr('backend.utils.llm_utils.sync_langsmith_environment', lambda: None)
    app = FastAPI()
    app.include_router(config_router)
    app.include_router(mcp_router)
    app.dependency_overrides[get_current_user] = lambda: {'sub': 'admin'}
    return TestClient(app), runtime, path


def save_tasks(agents):
    config_store.save_runtime_snapshot({'enable_scheduler': False}, agents, exchange_profiles_payload=[ACCOUNT])


def pending(owner='owner', side='LONG', state='unresolved'):
    with database.get_db_conn() as conn:
        conn.execute('INSERT INTO position_management_actions VALUES(?,?,?,?,?,?,?)',
                     (SCOPE, SYMBOL, side, owner, 'original-submission', state, 0))
        conn.commit()


def contents(path):
    with sqlite3.connect(path) as conn:
        return list(conn.iterdump())


@pytest.mark.parametrize('state', ['running', 'unresolved'])
@pytest.mark.parametrize('side', ['LONG', 'BOTH'])
def test_pending_actions_reject_scope_retirement_without_plans_and_keep_database(lifecycle, state, side):
    _, _, path = lifecycle
    save_tasks([task()])
    pending(side=side, state=state)
    before = contents(path)
    with pytest.raises(ValueError, match='active|unresolved'):
        save_tasks([])
    assert contents(path) == before


def test_old_client_null_fields_and_pause_keep_existing_exclusive_policy(lifecycle):
    client, runtime, _ = lifecycle
    save_tasks([task()])
    pending()
    runtime.reload_config()
    response = client.put('/api/config', json={'globals': {}, 'agents': [
        {'config_id': 'owner', 'symbol': SYMBOL, 'mode': 'REAL', 'enabled': False,
         'position_management_policy': None, 'management_scopes': None, 'exit_mode': None,
         'exchange_profile_id': 'account'}],
        'exchange_profiles': [{'profile_id': 'account', 'name': 'Offline', 'exchange': 'binance', 'market_type': 'swap'}]})
    assert response.status_code == 200, response.text
    cfg = runtime.get_config_by_id('owner')
    assert cfg['enabled'] is False and cfg['position_management_policy'] == 'exclusive_scope'
    assert cfg['management_scopes'] == [{'symbol': SYMBOL, 'side': 'LONG'}]
    with database.get_db_conn() as conn:
        assert conn.execute('SELECT owner FROM position_management_scopes').fetchone()[0] == 'owner'


def test_explicit_task_delete_checks_before_purging_original_operation(lifecycle):
    client, runtime, path = lifecycle
    save_tasks([task()])
    pending()
    runtime.reload_config()
    before = contents(path)
    response = client.delete('/api/config/owner')
    assert response.status_code in {400, 409}, response.text
    assert contents(path) == before
    assert runtime.get_config_by_id('owner') is not None


@pytest.mark.parametrize('path', ['save', 'delete'])
def test_active_inventory_without_action_stays_visible_until_retirement_guard(lifecycle, path):
    client, runtime, db_path = lifecycle
    save_tasks([task()])
    with database.get_db_conn() as conn:
        conn.execute('INSERT INTO real_protection_plans(config_id,symbol,side,payload) VALUES(?,?,?,?)',
                     ('owner', SYMBOL, 'LONG', json.dumps({'account_scope': SCOPE, 'symbol': SYMBOL,
                                                          'side': 'LONG', 'state': 'ACTIVE'})))
        conn.commit()
    runtime.reload_config()
    before = contents(db_path)
    if path == 'save':
        with pytest.raises(ValueError, match='active'):
            save_tasks([])
    else:
        assert client.delete('/api/config/owner').status_code == 409
    assert contents(db_path) == before


@pytest.mark.parametrize('field', ['api_key', 'secret', 'passphrase'])
def test_active_credential_rotations_roll_back_even_same_api_key(lifecycle, field):
    _, _, path = lifecycle
    save_tasks([task()])
    pending()
    before = contents(path)
    changed = {**ACCOUNT, field: 'replacement'}
    with pytest.raises(ValueError, match='active|credentials'):
        config_store.save_runtime_snapshot({}, [task()], exchange_profiles_payload=[changed])
    assert contents(path) == before


def test_rotation_guard_attaches_old_credentials_to_a_discovery_created_scope(lifecycle):
    _, _, path = lifecycle
    save_tasks([task()])
    with database.get_db_conn() as conn:
        conn.execute("UPDATE position_management_scopes SET payload='{}'")
        conn.commit()
    pending()
    before = contents(path)
    with pytest.raises(ValueError, match='credentials'):
        config_store.save_runtime_snapshot({}, [task()], exchange_profiles_payload=[{**ACCOUNT, 'secret': 'rotated'}])
    assert contents(path) == before


def test_discovery_retains_credential_and_policy_witness_for_next_save(lifecycle):
    from test_exclusive_position_management import ExclusiveExchange, make_service
    from backend.utils.position_management import ensure_managed
    save_tasks([task()])
    with database.get_db_conn() as conn:
        original = json.loads(conn.execute('SELECT payload FROM position_management_scopes').fetchone()[0])
    assert len(original['policy_revision']) == 64
    exchange = ExclusiveExchange()
    exchange.apiKey = ACCOUNT['api_key']
    service = make_service(exchange, 'owner')
    ensure_managed(service, SYMBOL)
    with database.get_db_conn() as conn:
        observed = json.loads(conn.execute('SELECT payload FROM position_management_scopes').fetchone()[0])
    assert observed['credential_revision'] == original['credential_revision']
    assert observed['policy_revision'] == original['policy_revision']
    assert ACCOUNT['secret'] not in json.dumps(observed)
    with pytest.raises(ValueError, match='credentials'):
        config_store.save_runtime_snapshot({}, [task()], exchange_profiles_payload=[{**ACCOUNT, 'secret': 'rotated'}])


def test_failed_crossowner_save_does_not_release_inactive_previous_claim(lifecycle):
    _, _, path = lifecycle
    save_tasks([task()])
    before = contents(path)
    with pytest.raises(ValueError, match='overlapping'):
        save_tasks([task('other'), task('another')])
    assert contents(path) == before


def test_configuration_headers_support_old_database_without_trade_tables():
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    snapshot = {'agents': [task()], 'exchange_profiles': [ACCOUNT]}
    synchronize_configuration(conn, snapshot)
    synchronize_configuration(conn, {**snapshot, 'agents': [task(enabled=False)]})
    assert conn.execute('SELECT COUNT(*) FROM position_management_scopes').fetchone()[0] == 1
    synchronize_configuration(conn, {**snapshot, 'agents': []})
    assert conn.execute('SELECT COUNT(*) FROM position_management_scopes').fetchone()[0] == 0
    conn.close()


def test_alias_credentials_and_hedge_proof_use_same_exchange_scope(lifecycle):
    snapshot = {'agents': [task(), {**task('second', 'SHORT'), 'exchange_profile_id': 'alias'}],
                'exchange_profiles': [ACCOUNT, {**ACCOUNT, 'profile_id': 'alias'}]}
    with database.get_db_conn() as conn:
        rows = configuration_candidates(conn, snapshot)
        assert {row['account_scope'] for row in rows} == {SCOPE}
        with pytest.raises(ValueError, match='overlapping'):
            synchronize_configuration(conn, snapshot)
        synchronize_configuration(conn, {**snapshot, 'agents': [task()]})
        conn.execute("UPDATE position_management_scopes SET payload=?", (json.dumps({'hedged': True}),))
        synchronize_configuration(conn, snapshot)
        assert conn.execute('SELECT COUNT(*) FROM position_management_scopes').fetchone()[0] == 2


def profile(**updates):
    return {'profile_id': 'mcp-owner', 'name': 'MCP owner', 'exchange_profile_id': 'account',
            'symbols': [SYMBOL], 'exit_mode': 'independent_exits',
            'position_management_policy': 'exclusive_scope',
            'management_scopes': [{'symbol': SYMBOL, 'side': 'LONG'}], **updates}


def test_mcp_external_only_profile_delete_and_downgrade_preserve_owner(lifecycle):
    client, _, path = lifecycle
    mcp_settings.save_profile(mcp_settings.MCPProfile.model_validate(profile()))
    pending(owner='mcp:mcp-owner')
    before = contents(path)
    assert client.delete('/api/mcp/profiles/mcp-owner').status_code == 409
    with pytest.raises(ValueError, match='active|unresolved'):
        mcp_settings.save_profile(mcp_settings.MCPProfile.model_validate(profile(position_management_policy='strict_owned', management_scopes=[])))
    assert contents(path) == before
    old_client = profile(enabled=False)
    old_client.pop('position_management_policy')
    old_client.pop('management_scopes')
    old_client.pop('exit_mode')
    result = mcp_settings.save_profile(mcp_settings.MCPProfile.model_validate(old_client))
    assert not result['enabled'] and result['position_management_policy'] == 'exclusive_scope'


def test_mcp_import_conflict_and_removed_scope_are_atomic(lifecycle):
    _, _, path = lifecycle
    save_tasks([task()])
    before = contents(path)
    with pytest.raises(ValueError, match='overlapping'):
        import_settings({'profiles': [profile()]})
    assert contents(path) == before
    save_tasks([])
    mcp_settings.save_profile(mcp_settings.MCPProfile.model_validate(profile()))
    pending(owner='mcp:mcp-owner')
    before = contents(path)
    with pytest.raises(ValueError, match='active|unresolved'):
        import_settings({'profiles': [profile(position_management_policy='strict_owned', management_scopes=[])]})
    assert contents(path) == before


def test_imported_old_client_profile_keeps_exclusive_claim(lifecycle):
    mcp_settings.save_profile(mcp_settings.MCPProfile.model_validate(profile()))
    pending(owner='mcp:mcp-owner')
    old = profile(enabled=False)
    old.pop('position_management_policy')
    old.pop('management_scopes')
    old.pop('exit_mode')
    import_settings({'profiles': [old]})
    assert store.get('profile', 'mcp-owner')['position_management_policy'] == 'exclusive_scope'


def test_full_import_null_management_fields_preserve_existing_scope(lifecycle):
    client, runtime, _ = lifecycle
    save_tasks([task()])
    pending()
    runtime.reload_config()
    data = config_store.export_full_snapshot(include_secrets=True)
    for item in data['agents']:
        item['position_management_policy'] = None
        item['management_scopes'] = None
        item['enabled'] = False
    response = client.post('/api/config/full-import', json={'data': data})
    assert response.status_code == 200, response.text
    assert runtime.get_config_by_id('owner')['position_management_policy'] == 'exclusive_scope'


def _save_competing_profile(path, identity, start, result):
    database.DB_NAME = str(path)
    config_store.DB_NAME = Path(path)
    os.environ['CONFIG_MASTER_KEY'] = 'config-lifecycle-test'
    mcp_settings.exchange_profile = lambda _: ACCOUNT
    start.wait(20)
    try:
        mcp_settings.save_profile(mcp_settings.MCPProfile.model_validate(profile(profile_id=identity)))
        result.put(('saved', identity))
    except Exception as exc:
        result.put(('rejected', str(exc)))


def test_two_process_mcp_configuration_claims_commit_only_one_owner(lifecycle):
    _, _, path = lifecycle
    ctx = multiprocessing.get_context('spawn')
    start, results = ctx.Event(), ctx.Queue()
    processes = [ctx.Process(target=_save_competing_profile, args=(str(path), identity, start, results))
                 for identity in ('first', 'second')]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(25)
        if process.is_alive():
            process.terminate()
            process.join(5)
            pytest.fail('Configuration transaction did not finish')
        assert process.exitcode == 0
    outcomes = [results.get(timeout=5) for _ in processes]
    assert sorted(row[0] for row in outcomes) == ['rejected', 'saved']
    with database.get_db_conn() as conn:
        rows = conn.execute("SELECT id FROM mcp_records WHERE kind='profile'").fetchall()
        assert len(rows) == 1
        assert conn.execute('SELECT owner FROM position_management_scopes').fetchone()[0] == 'mcp:' + rows[0][0]
