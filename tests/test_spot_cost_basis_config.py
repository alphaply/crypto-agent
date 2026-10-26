"""Configuration commits cannot detach a concurrently saved spot cost basis."""
from copy import deepcopy

import pytest

from backend import config_store, database, storage
from backend.utils.spot_config_guard import (
    SpotConfigConflict,
    assert_spot_config_change_allowed,
    spot_cost_basis_account_identity,
)


@pytest.fixture
def configured(pg, monkeypatch):
    monkeypatch.setattr(config_store, 'load_dotenv', lambda *args, **kwargs: None)
    config_store.save_runtime_snapshot(
        {'enable_scheduler': False},
        [{'config_id': 'spot', 'mode': 'SPOT_DCA', 'symbol': 'BTC/USDT',
          'symbols': ['BTC/USDT', 'ETH/USDT'], 'exchange_profile_id': 'account-a',
          'llm_provider_id': 'model'}],
        llm_providers_payload=[{'provider_id': 'model', 'name': 'Model', 'model': 'test',
                                'api_key': 'model-key'}],
        exchange_profiles_payload=[
            {'profile_id': name, 'exchange': 'okx', 'market_type': 'spot',
             'api_key': 'exchange-key', 'secret': 'exchange-secret', 'passphrase': 'exchange-pass'}
            for name in ('account-a', 'account-b')
        ],
    )
    return config_store.load_management_snapshot()


def save(snapshot, **kwargs):
    config_store.save_runtime_snapshot(snapshot['globals'], snapshot['agents'],
                                      snapshot['llm_providers'], snapshot['exchange_profiles'], **kwargs)


def insert_basis(symbol='ETH/USDT'):
    with database.get_db_conn() as conn:
        # Mirrors the separate API process committing under runtime-config.
        storage.lock_scope(conn, 'runtime-config')
        conn.execute('''INSERT INTO spot_cost_basis
            (config_id,symbol,quantity,avg_cost,order_baselines_json,updated_at)
            VALUES ('spot',%s,1,100,'{}','2026-10-26T10:00:00+00:00')''', (symbol,))
        conn.commit()


@pytest.mark.parametrize('change', ['profile', 'symbol', 'mode', 'remove'])
def test_basis_saved_after_prevalidation_still_blocks_detaching_commit(configured, change):
    candidate = deepcopy(configured)
    previous = config_store.load_runtime_snapshot()['agents'][0]
    agent = candidate['agents'][0]
    if change == 'profile':
        agent['exchange_profile_id'] = 'account-b'
    elif change == 'symbol':
        agent['symbols'] = ['BTC/USDT']
    elif change == 'mode':
        agent['mode'] = 'STRATEGY'
        agent['symbols'] = ['BTC/USDT']
    else:
        candidate['agents'] = []
    # Request A passes its early guard before request B saves a cost basis.
    assert_spot_config_change_allowed(previous, agent if change != 'remove' else None)
    insert_basis()
    before = config_store.load_runtime_snapshot()
    # Request A now reaches its transaction with the stale configuration.
    with pytest.raises(SpotConfigConflict, match='持仓成本'):
        save(candidate)
    assert config_store.load_runtime_snapshot() == before
    assert database.get_config_dependency_counts('spot')['spot_cost_basis'] == 1


@pytest.mark.parametrize('field', ['api_key', 'secret', 'passphrase'])
def test_same_profile_credential_rotation_rolls_back(configured, field):
    insert_basis()
    before = config_store.load_runtime_snapshot()
    profile = next(item for item in configured['exchange_profiles'] if item['profile_id'] == 'account-a')
    profile['secrets'][field] = {'value': 'replacement'}
    with pytest.raises(SpotConfigConflict, match='凭据'):
        save(configured)
    assert config_store.load_runtime_snapshot() == before


def test_unrelated_model_key_change_and_symbol_reorder_preserve_basis(configured):
    insert_basis()
    configured['llm_providers'][0]['secrets']['api_key'] = {'value': 'replacement-model-key'}
    configured['agents'][0]['symbols'] = ['ETH/USDT', 'BTC/USDT']
    configured['agents'][0]['symbol'] = 'ETH/USDT'
    save(configured)
    current = config_store.load_runtime_snapshot()
    assert current['agents'][0]['api_key'] == 'replacement-model-key'
    assert current['agents'][0]['symbol'] == 'ETH/USDT'
    assert database.get_config_dependency_counts('spot')['spot_cost_basis'] == 1


def test_explicit_deletion_hook_cleans_basis_before_guard(configured):
    insert_basis()
    configured['agents'] = []
    save(configured, transaction_hook=lambda conn: database.purge_config_all_data('spot', connection=conn))
    assert config_store.load_runtime_snapshot()['agents'] == []
    assert database.get_config_dependency_counts('spot')['spot_cost_basis'] == 0


def test_failed_hook_rolls_back_cleanup_and_configuration(configured):
    insert_basis()
    before = config_store.load_runtime_snapshot()
    configured['agents'] = []
    def fail_after_cleanup(conn):
        database.purge_config_all_data('spot', connection=conn)
        raise ValueError('test failure after cleanup')
    with pytest.raises(ValueError, match='test failure'):
        save(configured, transaction_hook=fail_after_cleanup)
    assert config_store.load_runtime_snapshot() == before
    assert database.get_config_dependency_counts('spot')['spot_cost_basis'] == 1


def test_global_credentials_used_by_bound_empty_profile_are_guarded(pg, monkeypatch):
    monkeypatch.setattr(config_store, 'load_dotenv', lambda *args, **kwargs: None)
    config_store.save_runtime_snapshot(
        {'global_binance_api_key': 'global-key', 'global_binance_secret': 'global-secret'},
        [{'config_id': 'spot', 'mode': 'SPOT_DCA', 'symbol': 'ETH/USDT',
          'symbols': ['ETH/USDT'], 'exchange_profile_id': 'global-account', 'llm_provider_id': 'model'}],
        llm_providers_payload=[{'provider_id': 'model', 'name': 'Model', 'model': 'test',
                                'api_key': 'model-key'}],
        exchange_profiles_payload=[{'profile_id': 'global-account', 'exchange': 'binance', 'market_type': 'spot'}],
    )
    insert_basis()
    before = config_store.load_runtime_snapshot()
    candidate = config_store.load_management_snapshot()
    candidate['globals']['secrets']['global_binance_api_key'] = {'value': 'changed-global-key'}
    with pytest.raises(SpotConfigConflict, match='凭据'):
        save(candidate)
    assert config_store.load_runtime_snapshot() == before


def test_identity_matches_resolved_runtime_without_profile_list():
    agent = {'exchange_profile_id': 'a', 'exchange': 'okx', 'market_type': 'spot',
             'llm_provider_id': 'model', 'api_key': 'model-key', 'okx_api_key': 'exchange-key',
             'okx_secret': 'exchange-secret', 'passphrase': 'exchange-pass'}
    snapshot = {'exchange_profiles': [{'profile_id': 'a', 'exchange': 'okx', 'market_type': 'spot',
                                      'api_key': 'exchange-key', 'secret': 'exchange-secret',
                                      'passphrase': 'exchange-pass'}]}
    assert spot_cost_basis_account_identity(snapshot, agent) == spot_cost_basis_account_identity({}, agent)
    assert spot_cost_basis_account_identity({}, agent) == spot_cost_basis_account_identity(
        {}, {**agent, 'api_key': 'changed-model-key', 'physical_account_id': 'first-discovery'})


def test_identity_prefers_actual_global_fallback_over_llm_key():
    snapshot = {'global_binance_api_key': 'global-key', 'global_binance_secret': 'global-secret'}
    agent = {'exchange': 'binance', 'llm_provider_id': 'model', 'api_key': 'model-key'}
    initial = spot_cost_basis_account_identity(snapshot, agent)
    assert initial == spot_cost_basis_account_identity(snapshot, {**agent, 'api_key': 'new-model-key'})
    assert initial != spot_cost_basis_account_identity({**snapshot, 'global_binance_secret': 'new-secret'}, agent)
