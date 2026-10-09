"""Workbench configuration boundaries. Uses a disposable database only."""
import copy
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import config_store, database
from backend.app.api import config as config_api
from backend.app.core.deps import get_current_user
from backend.app.services import public_service


@pytest.fixture
def runtime_db(tmp_path, monkeypatch):
    path = tmp_path / 'runtime.db'
    monkeypatch.setattr(database, 'DB_NAME', str(path))
    monkeypatch.setattr(config_store, 'DB_NAME', path)
    monkeypatch.setenv('CONFIG_MASTER_KEY', 'workbench-test-only-master')
    monkeypatch.setattr(database, '_initialize_runtime_config', lambda: None)
    monkeypatch.setattr(database, '_reload_runtime_config', lambda: None)
    database.init_db()
    return path


def test_channel_settings_round_trip_preserves_existing_cadence(runtime_db):
    settings = copy.deepcopy(config_store.DEFAULT_GLOBAL_SETTINGS)
    settings['news']['min_score'] = 75
    providers = [{'provider_id':'a','name':'Primary','model':'same-name','api_base':'https://example.invalid/v1','api_protocol':'chat','report_output_mode':'tool','pricing_mode':'models_dev','models_dev_provider_id':'vendor','models_dev_model_id':'original-name','cache_read_price_per_m':0.2,'secrets':{'api_key':{'value':'private-test-key'}}}, {'provider_id':'b','name':'Other','model':'same-name','api_protocol':'decisions','input_price_per_m':1.5,'output_price_per_m':3.0}]
    agents = [{'config_id':'legacy','symbol':'BTC/USDT','mode':'MOCK','enabled':False,'run_interval':17,'market_timeframes':['15m','4h'],'llm_provider_id':'a','run_schedule':[{'days':[0,1], 'start':'09:00','end':'10:00','interval':20,'timezone':'Asia/Shanghai'}]}]
    config_store.save_runtime_snapshot(settings, agents, providers, [])
    hidden = config_store.load_management_snapshot()
    visible = config_store.load_management_snapshot(include_secrets=True)
    hidden['llm_providers'].sort(key=lambda p:p['provider_id'])
    visible['llm_providers'].sort(key=lambda p:p['provider_id'])
    assert not hidden['llm_providers'][0]['secrets']['api_key'].get('value')
    assert visible['llm_providers'][0]['secrets']['api_key']['value'] == 'private-test-key'
    assert b'private-test-key' not in runtime_db.read_bytes()
    assert visible['agents'][0]['run_interval'] == 17
    assert visible['agents'][0]['run_schedule'][0]['interval'] == 20
    assert visible['agents'][0]['market_timeframes'] == ['15m','4h']
    assert visible['globals']['news']['min_score'] == 75
    assert visible['llm_providers'][0]['report_output_mode'] == 'tool'
    assert visible['llm_providers'][0]['models_dev_model_id'] == 'original-name'
    assert visible['llm_providers'][1]['input_price_per_m'] == 1.5


def test_configuration_secrets_require_admin_and_are_not_cacheable(monkeypatch):
    app=FastAPI();app.include_router(config_api.router)
    monkeypatch.setattr(config_api,'get_raw_config_payload',lambda:{'secret':'only-management'})
    with TestClient(app) as client:
        assert client.get('/api/config').status_code == 401
        app.dependency_overrides[get_current_user]=lambda:{'sub':'admin'}
        response=client.get('/api/config')
        assert response.status_code == 200
        assert response.headers['cache-control'] == 'no-store'
        assert response.json()['secret'] == 'only-management'


def test_usage_summary_never_sums_different_currencies_or_implies_free():
    payload={'daily':[], 'models':[{'cost':3,'currency':'USD'},{'cost':4,'currency':'CNY'}], 'agents':[], 'summary':{'cost':None,'unpriced_calls':2,'costs_by_currency':{'USD':3,'CNY':4}}}
    result=public_service._usage_summary(payload)
    assert result['total_cost'] is None
    assert result['unpriced_calls'] == 2
    assert result['costs_by_currency'] == {'USD':3,'CNY':4}


def test_public_dashboard_lists_all_tasks_without_resolved_credentials(runtime_db, monkeypatch):
    from backend.app.services import dashboard_service, news_service
    configs = [
        {'config_id': 'btc', 'symbol': 'BTC/USDT', 'enabled': True, 'mode': 'STRATEGY',
         'api_key': 'must-stay-private', 'fallback_models': [{'api_key': 'backup-private'}]},
        {'config_id': 'eth', 'symbol': 'ETH/USDT', 'enabled': False, 'mode': 'STRATEGY',
         'secret': 'exchange-private'},
    ]
    monkeypatch.setattr(dashboard_service.global_config, 'get_all_symbol_configs', lambda: configs)
    monkeypatch.setattr(dashboard_service, 'get_scheduler_status', lambda: False)
    monkeypatch.setattr(news_service, 'get_latest_global_snapshot', lambda: {'raw': {'digest': 'shared digest'}})
    monkeypatch.setattr(public_service, 'get_token_stats_payload', lambda: {})
    result = public_service.build_public_dashboard_payload()
    assert result['current_symbol'] is None
    assert {row['config_id'] for row in result['agent_summaries']} == {'btc', 'eth'}
    assert result['overview_metrics']['enabled_count'] == 1
    assert result['news_snapshot']['raw']['digest'] == 'shared digest'
    serialized = json.dumps(result)
    assert all(secret not in serialized for secret in ('must-stay-private', 'backup-private', 'exchange-private'))


def test_import_validation_rejects_jev_as_an_agent_fallback(runtime_db):
    from backend.config import Config
    config_store.save_runtime_snapshot(copy.deepcopy(config_store.DEFAULT_GLOBAL_SETTINGS), [
        {'config_id': 'keep', 'symbol': 'BTC/USDT', 'mode': 'STRATEGY'},
    ])
    with pytest.raises(ValueError, match='Jev Decisions'):
        config_store.save_runtime_snapshot(copy.deepcopy(config_store.DEFAULT_GLOBAL_SETTINGS), [
            {'config_id': 'invalid', 'symbol': 'BTC/USDT', 'fallback_llm_provider_ids': ['jev']},
        ], [{'provider_id': 'jev', 'model': 'jev-v1', 'api_protocol': 'decisions'}], [],
            validate_snapshot=Config.validate_snapshot)
    assert config_store.load_runtime_snapshot()['agents'][0]['config_id'] == 'keep'
