from copy import deepcopy
import sqlite3

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from backend import config_store, database
from backend.app.api import config as routes
from backend.app.core.deps import get_current_user
from backend.app.services import config_service
from backend.config import Config
from backend.database_schema import initialize_schema
from backend.utils.spot_execution import _initialize


@pytest.fixture
def deletion_api(tmp_path, monkeypatch):
    path = tmp_path / 'deletion.sqlite'
    with sqlite3.connect(path) as conn:
        initialize_schema(conn)
        _initialize(conn)
    monkeypatch.setattr(config_store, 'DB_NAME', path)
    monkeypatch.setattr(database, 'DB_NAME', str(path))
    monkeypatch.setattr(config_store, 'load_dotenv', lambda *args, **kwargs: None)
    monkeypatch.setenv('CONFIG_MASTER_KEY', 'deletion-test-key')
    profiles = [
        {'profile_id': name, 'exchange': 'binance', 'market_type': 'spot',
         'api_key': f'{name}-key', 'secret': f'{name}-secret'}
        for name in ('account-a', 'account-b')
    ]
    config_store.save_runtime_snapshot({'enable_scheduler': False}, [
        {'config_id': 'spot', 'mode': 'SPOT_DCA', 'symbol': 'BTC/USDT',
         'symbols': ['BTC/USDT'], 'exchange_profile_id': 'account-a'},
        {'config_id': 'keep', 'mode': 'STRATEGY', 'symbol': 'ETH/USDT', 'exchange_profile_id': 'account-a'},
    ], exchange_profiles_payload=profiles, validate_snapshot=Config.validate_snapshot)
    runtime = Config()
    monkeypatch.setattr(config_service, 'global_config', runtime)
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[get_current_user] = lambda: {'sub': 'test-admin'}
    return TestClient(app), runtime, path


def seed_task_data(status='OPEN'):
    for config_id in ('spot', 'keep'):
        database.save_order_log(f'{config_id}-order', 'BTC/USDT', config_id, 'buy', 10, 0, 0, '',
                                trade_mode='SPOT_DCA', config_id=config_id, amount=2, status=status)
    with database.get_db_conn() as conn:
        conn.execute('''INSERT INTO spot_budget_reservations
            (config_id,operation_id,cycle_id,symbol,quote_cost,status,created_at,updated_at)
            VALUES ('spot','unverified','cycle','BTC/USDT',20,'unknown',1,1)''')
        conn.execute('''INSERT INTO chat_sessions
            (session_id,title,config_id,symbol,created_at,updated_at)
            VALUES ('spot-chat','Test','spot','BTC/USDT','2026-10-09','2026-10-09')''')
        conn.commit()


def dump_database(path):
    with sqlite3.connect(path) as conn:
        return list(conn.iterdump())


@pytest.mark.parametrize('status', ['OPEN', 'PARTIAL', 'FILLED', 'CANCELLED'])
def test_explicit_delete_cleans_spot_task_with_order_evidence(deletion_api, status):
    client, runtime, _ = deletion_api
    seed_task_data(status)

    response = client.delete('/api/config/spot')

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload['cleanup']['orders_deleted'] == 1
    assert payload['cleanup']['spot_budget_reservations_deleted'] == 1
    assert payload['cleanup']['chat_sessions_deleted'] == 1
    assert payload['exchange_orders_cancelled'] is False
    assert payload['exchange_positions_closed'] is False
    assert runtime.get_config_by_id('spot') is None
    assert runtime.get_config_by_id('keep') is not None
    assert all(count == 0 for count in database.get_config_dependency_counts('spot').values())
    assert database.get_config_dependency_counts('keep')['orders'] == 1
    assert len(config_store.load_management_snapshot()['exchange_profiles']) == 2
    assert client.delete('/api/config/spot').status_code == 404


def test_explicit_delete_allows_manual_initial_holdings(deletion_api):
    client, runtime, _ = deletion_api
    snapshot = config_store.load_management_snapshot()
    snapshot['agents'][0].update(initial_qty=1, initial_cost=50000)
    config_store.save_runtime_snapshot(snapshot['globals'], snapshot['agents'], snapshot['llm_providers'],
                                       snapshot['exchange_profiles'], validate_snapshot=runtime.validate_snapshot)
    runtime.reload_config()
    assert client.delete('/api/config/spot').status_code == 200
    assert runtime.get_config_by_id('spot') is None


@pytest.mark.parametrize('change', ['account', 'mode', 'bulk_remove'])
def test_ordinary_config_edits_still_protect_owned_spot_orders(deletion_api, change):
    client, runtime, path = deletion_api
    seed_task_data()
    before = dump_database(path)
    snapshot = config_store.load_management_snapshot()
    agent = next(item for item in snapshot['agents'] if item['config_id'] == 'spot')
    if change == 'account':
        agent['exchange_profile_id'] = 'account-b'
    elif change == 'mode':
        agent['mode'] = 'STRATEGY'
    else:
        snapshot['agents'] = [item for item in snapshot['agents'] if item['config_id'] != 'spot']

    response = client.put('/api/config', json={
        key: snapshot[key] for key in ('globals', 'agents', 'llm_providers', 'exchange_profiles')
    })

    assert response.status_code == 409, response.text
    if change == 'bulk_remove':
        assert '删除' in response.json()['detail']
    assert dump_database(path) == before
    assert runtime.get_config_by_id('spot')['mode'] == 'SPOT_DCA'


@pytest.mark.parametrize('failure', ['validation', 'cleanup'])
def test_failed_deletion_rolls_back_configuration_and_history(deletion_api, monkeypatch, failure):
    _, runtime, path = deletion_api
    seed_task_data()
    before_db = dump_database(path)
    before_runtime = deepcopy(runtime.__dict__)
    original_purge = config_service.purge_config_all_data

    def fail_validation(_snapshot):
        raise ValueError('validation failed')

    def fail_cleanup(config_id, *, connection=None):
        original_purge(config_id, connection=connection)
        raise ValueError('cleanup failed')

    if failure == 'validation':
        monkeypatch.setattr(runtime, 'validate_snapshot', fail_validation)
    else:
        monkeypatch.setattr(config_service, 'purge_config_all_data', fail_cleanup)
    with pytest.raises(ValueError, match=f'{failure} failed'):
        config_service.delete_config_payload('spot')

    assert dump_database(path) == before_db
    assert runtime.configs_by_id == before_runtime['configs_by_id']
    assert database.get_config_dependency_counts('spot')['orders'] == 1


@pytest.mark.parametrize('exchange', ['binance', 'okx'])
@pytest.mark.parametrize('update_kind', ['unchanged', 'rotated', 'cleared'])
def test_config_roundtrip_with_global_credentials_and_owned_spot_orders(
    deletion_api, monkeypatch, exchange, update_kind,
):
    from backend.utils import llm_utils

    monkeypatch.setattr(llm_utils, 'sync_langsmith_environment', lambda: None)
    client, runtime, path = deletion_api
    global_secrets = {
        f'global_{exchange}_api_key': 'saved-exchange-key',
        f'global_{exchange}_secret': 'saved-exchange-secret',
    }
    if exchange == 'okx':
        global_secrets['global_okx_passphrase'] = 'saved-passphrase'
    config_store.save_runtime_snapshot({'enable_scheduler': False, **global_secrets}, [
        {'config_id': 'spot', 'mode': 'SPOT_DCA', 'symbol': 'BTC/USDT', 'exchange': exchange},
    ], validate_snapshot=runtime.validate_snapshot)
    runtime.reload_config()
    seed_task_data()

    response = client.get('/api/config')
    assert response.status_code == 200, response.text
    before_db = dump_database(path)
    snapshot = response.json()
    secret = snapshot['globals']['secrets'][f'global_{exchange}_api_key']
    assert secret['value'] == 'saved-exchange-key'
    if update_kind == 'rotated':
        secret['value'] = 'replacement-exchange-key'
    elif update_kind == 'cleared':
        secret['clear'] = True
    snapshot['agents'][0]['title'] = 'Unrelated title edit'

    response = client.put('/api/config', json={
        key: snapshot[key] for key in ('globals', 'agents', 'llm_providers', 'exchange_profiles')
    })

    if update_kind == 'unchanged':
        assert response.status_code == 200, response.text
        assert runtime.get_config_by_id('spot')['title'] == 'Unrelated title edit'
        assert database.get_config_dependency_counts('spot')['orders'] == 1
    else:
        assert response.status_code == 409, response.text
        assert dump_database(path) == before_db
    assert getattr(runtime, f'global_{exchange}_api_key') == 'saved-exchange-key'
