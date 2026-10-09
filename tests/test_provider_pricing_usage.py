import json
from datetime import datetime, timezone

import httpx
import pytest

from backend import database, database_agent_runs, database_usage
from backend.app.services import pricing_service
from backend.database_schema import initialize_schema


@pytest.fixture
def usage_env(tmp_path, monkeypatch):
    monkeypatch.setattr(database, 'DB_NAME', str(tmp_path / 'usage.sqlite'))
    with database.get_db_conn() as conn:
        initialize_schema(conn)
    state = {'llm_providers': [
        {'provider_id': 'a', 'model': 'same-model', 'pricing_mode': 'manual', 'input_price_per_m': 1, 'output_price_per_m': 2, 'cache_read_price_per_m': .1, 'cache_write_price_per_m': 1.25},
        {'provider_id': 'b', 'model': 'same-model', 'pricing_mode': 'models_dev', 'models_dev_provider_id': 'upstream', 'models_dev_model_id': 'same-model'},
    ]}
    monkeypatch.setattr(pricing_service, '_runtime', lambda: state)
    return state


def fake_catalog():
    return {'upstream': {'name': 'Upstream', 'models': {'same-model': {'cost': {'input': 4, 'output': 8, 'cache_read': .4, 'cache_write': 5}}}}}


def test_sync_changes_only_opted_in_provider_prices(usage_env, monkeypatch):
    monkeypatch.setattr(pricing_service.httpx, 'get', lambda *a, **kw: httpx.Response(200, json=fake_catalog(), request=httpx.Request('GET', pricing_service.CATALOG_URL)))
    assert pricing_service.sync_prices()['updated'] == 1
    assert pricing_service.get_price_snapshot('same-model', 'a')['input_price_per_m'] == 1
    assert pricing_service.get_price_snapshot('same-model', 'b')['input_price_per_m'] == 4
    assert pricing_service.resolve_provider_id('same-model') is None


def test_catalog_failure_retains_price_and_does_not_clear_data(usage_env, monkeypatch):
    monkeypatch.setattr(pricing_service.httpx, 'get', lambda *a, **kw: httpx.Response(200, json=fake_catalog(), request=httpx.Request('GET', pricing_service.CATALOG_URL)))
    pricing_service.sync_prices()
    monkeypatch.setattr(pricing_service.httpx, 'get', lambda *a, **kw: (_ for _ in ()).throw(httpx.ConnectError('offline')))
    assert pricing_service.sync_prices()['status'] == 'error'
    assert pricing_service.get_price_snapshot('same-model', 'b')['input_price_per_m'] == 4


def test_cache_costs_no_double_count_and_immutable_after_price_edit(usage_env):
    run = database_agent_runs.start_agent_run('cfg', 'chat', 'same-model', [], provider_id='a')
    usage_env['llm_providers'][0]['input_price_per_m'] = 100
    database_agent_runs.finish_agent_run(run, status='success', usage={'input_tokens': 1000, 'output_tokens': 100, 'input_token_details': {'cache_read': 400, 'cache_creation': 100}})
    result = database_agent_runs.get_agent_run(run)
    assert result['cost'] == pytest.approx((500 + 40 + 125 + 200) / 1_000_000)
    with database.get_db_conn() as conn:
        conn.execute('DELETE FROM agent_runs')
        conn.commit()
    stats = database_usage.token_stats()
    assert stats['summary']['calls'] == 1
    assert stats['providers'][0]['provider_id'] == 'a'
    assert stats['summary']['cost'] == result['cost']


def test_native_anthropic_input_excludes_cached_tokens():
    counts = database_usage.normalize_usage({'input_tokens': 500, 'output_tokens': 100, 'cache_read_input_tokens': 400, 'cache_creation_input_tokens': 100})
    assert counts['prompt_tokens'] == 1000 and counts['total_tokens'] == 1100


def test_tier_prices_are_call_time_and_malformed_tiers_stay_unknown():
    prices = {'input_price_per_m': 1, 'output_price_per_m': 2,
              'tiers': [{'tier': {'size': 200000}, 'input': 2, 'output': 4}]}
    cost, currency, _ = database_usage.calculate_cost({'input_tokens': 300000, 'output_tokens': 10}, prices)
    assert cost == pytest.approx(.60004) and currency == 'USD'
    prices['tiers'] = [{'tier': None}]
    assert database_usage.calculate_cost({'input_tokens': 100, 'output_tokens': 10}, prices)[0] is None


def test_ambiguous_provider_does_not_fall_back_to_model_only_prices(usage_env):
    database.update_model_pricing('same-model', 100, 200)
    assert pricing_service.get_price_snapshot('same-model')['input_price_per_m'] is None


def test_unknown_price_not_zero_and_currencies_not_combined(usage_env):
    for provider_id, price in [('a', {'input_price_per_m': 1, 'output_price_per_m': 2, 'currency': 'USD'}), ('b', {'input_price_per_m': 1, 'output_price_per_m': 2, 'currency': 'CNY'}), ('unknown', {})]:
        run = database_agent_runs.start_agent_run('cfg', 'decision', 'same-model', [], provider_id=provider_id, price_snapshot=price)
        database_agent_runs.finish_agent_run(run, status='success', usage={'input_tokens': 1000, 'output_tokens': 100})
    stats = database_usage.token_stats()
    assert stats['summary']['cost'] is None and stats['summary']['unpriced_calls'] == 1
    assert set(stats['today']['costs_by_currency']) == {'USD', 'CNY'}


def test_legacy_rows_import_once_without_double_counting_new_calls(usage_env):
    database.save_token_usage('BTC', 'old-cfg', 'legacy-model', 10, 20)
    run = database_agent_runs.start_agent_run('cfg', 'decision', 'same-model', [], provider_id='a')
    database.save_token_usage('BTC', 'cfg', 'same-model', 100, 200)
    database_agent_runs.finish_agent_run(run, status='success', usage={'input_tokens': 100, 'output_tokens': 200})
    stats = database_usage.token_stats()
    assert stats['summary']['calls'] == 2
    assert stats['summary']['total'] == 330
    assert database_usage.token_stats()['summary']['calls'] == 2


def test_provider_reported_cost_wins_and_final_accounting_is_idempotent(usage_env):
    run = database_agent_runs.start_agent_run('cfg', 'decision', 'same-model', [], provider_id='a')
    database_agent_runs.finish_agent_run(run, status='success', usage={'input_tokens': 100, 'output_tokens': 200, 'cost': .004, 'currency': 'USD'})
    database_agent_runs.finish_agent_run(run, status='success', usage={'input_tokens': 999, 'output_tokens': 999})
    assert database_usage.token_stats()['summary']['cost'] == .004
    assert database_usage.token_stats()['summary']['total'] == 300


def test_summarizer_provider_follows_effective_connection_not_agent_reference(monkeypatch):
    from backend.config import config
    from backend.utils.llm_utils import resolve_summarizer_provider_id
    agent = {'llm_provider_id': 'agent', 'model': 'same', 'api_base': 'https://agent.test/v1', 'api_key': 'agent-key'}
    providers = [
        {'provider_id': 'agent', 'model': 'same', 'api_base': 'https://agent.test/v1', 'api_key': 'agent-key'},
        {'provider_id': 'summary', 'model': 'same', 'api_base': 'https://summary.test/v1', 'api_key': 'summary-key'},
    ]
    monkeypatch.setattr(config, 'llm_providers', providers)
    for key in ('model', 'api_base', 'api_key'):
        monkeypatch.setattr(config, f'global_summarizer_{key}', '')
        monkeypatch.delenv(f'GLOBAL_SUMMARIZER_{key.upper()}', raising=False)
    assert resolve_summarizer_provider_id(agent) == 'agent'
    for key in ('model', 'api_base', 'api_key'):
        monkeypatch.setattr(config, f'global_summarizer_{key}', providers[1][key])
    assert resolve_summarizer_provider_id(agent) == 'summary'
    agent['summarizer'] = {'api_key': 'inline-key'}
    assert resolve_summarizer_provider_id(agent) is None
