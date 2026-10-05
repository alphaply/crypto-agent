import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool

from backend import database
from backend import database_agent_runs as runs
from backend.app.api.agent_runs import router
from backend.app.core.deps import get_current_user
from backend.database_schema import initialize_schema


@pytest.fixture
def local_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(database, 'DB_NAME', str(tmp_path / 'runs.sqlite'))


@pytest.fixture
def client(local_runs):
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def authenticate(client):
    client.app.dependency_overrides[get_current_user] = lambda: {'username': 'admin'}


def start(config_id='cfg', purpose='decision', **kwargs):
    return runs.start_agent_run(config_id, purpose, 'test-model', [{'role': 'user', 'content': '真实输入'}], **kwargs)


def test_actual_messages_and_tool_schemas_survive_roundtrip(local_runs):
    @tool
    def inspect_price(symbol: str) -> str:
        """Inspect a symbol's price."""
        return symbol

    messages = [
        SystemMessage(content='风险约束'), HumanMessage(content=[{'type': 'text', 'text': '正文\n第二行'}]),
        AIMessage(content='', tool_calls=[{'name': 'inspect_price', 'args': {'symbol': 'BTC/USDT'}, 'id': 'call-1', 'type': 'tool_call'}]),
        ToolMessage(content='{"price":100}', tool_call_id='call-1', name='inspect_price'),
        {'role': 'assistant', 'content': '完整输出', 'metadata': {'api_key': 'not-persisted'}},
    ]
    run_id = runs.start_agent_run('cfg', 'decision', 'test-model', messages, [inspect_price])
    runs.finish_agent_run(run_id, status='success', output='等待确认', usage={'input_tokens': 12, 'output_tokens': 4}, details={'tool_calls': [{'name': 'inspect_price'}]})
    saved = runs.get_agent_run(run_id)
    assert [item['role'] for item in saved['messages']] == ['system', 'user', 'assistant', 'tool', 'assistant']
    assert saved['messages'][1]['content'] == messages[1].content
    assert saved['messages'][2]['tool_calls'][0]['args'] == {'symbol': 'BTC/USDT'}
    assert saved['messages'][3]['tool_call_id'] == 'call-1'
    assert 'metadata' not in saved['messages'][4]
    assert saved['tools'][0]['function']['name'] == 'inspect_price'
    assert saved['tools'][0]['function']['parameters']['properties']['symbol']['type'] == 'string'
    assert saved['status'] == 'success' and saved['output'] == '等待确认'
    assert saved['prompt_tokens'] == 12 and saved['completion_tokens'] == 4 and saved['total_tokens'] == 16
    assert saved['cost'] is None and saved['currency'] is None
    assert saved['output_chars'] == len('等待确认')
    assert saved['input_chars'] == saved['message_chars'] + saved['tool_chars']
    assert saved['message_chars'] == len(json.dumps(saved['messages'], ensure_ascii=False, separators=(',', ':')))


def test_unknown_usage_errors_parent_and_sensitive_metadata(local_runs):
    parent = start()
    child = start(parent_run_id=parent)
    runs.finish_agent_run(child, status='error', error='Provider unavailable', details={'api_key': 'do-not-store', 'config': {'secret': 'hidden'}, 'object': object()})
    saved = runs.get_agent_run(child)
    assert saved['parent_run_id'] == parent
    assert saved['error'] == 'Provider unavailable'
    assert saved['prompt_tokens'] is None and saved['total_tokens'] is None and saved['cost'] is None
    assert saved['details'] == {'api_key': '[redacted]', 'config': '[redacted]', 'object': '[unsupported value]'}
    runs.finish_agent_run(parent, status='success', usage={'prompt_tokens': 0, 'completion_tokens': 0, 'cost': 0, 'currency': 'USD'})
    assert runs.get_agent_run(parent)['cost'] == 0
    assert runs.get_agent_run(parent)['cost_source'] == 'provider'
    assert runs.get_agent_run(parent)['total_tokens'] == 0


def test_cost_estimate_uses_call_time_prices_and_persists_currency(local_runs):
    with database.get_db_conn() as conn:
        initialize_schema(conn)
    database.update_model_pricing('test-model', 2.5, 8, 'CNY')
    run_id = start()
    database.update_model_pricing('test-model', 10, 100, 'USD')
    runs.finish_agent_run(run_id, status='success', usage={'prompt_tokens': 2000, 'completion_tokens': 500})
    saved = runs.get_agent_run(run_id)
    assert saved['cost'] == pytest.approx(0.009)
    assert saved['currency'] == 'CNY'
    assert saved['cost_source'] == 'model_pricing'
    assert saved['input_price_per_m'] == 2.5 and saved['output_price_per_m'] == 8
    assert runs.list_agent_runs()['runs'][0]['cost_source'] == 'model_pricing'


@pytest.mark.parametrize('usage', [{}, {'total_tokens': 1000}, {'prompt_tokens': 2000}, {'completion_tokens': 500}])
def test_priced_model_without_complete_usage_has_unknown_cost(local_runs, usage):
    with database.get_db_conn() as conn:
        initialize_schema(conn)
    database.update_model_pricing('test-model', 2.5, 8)
    run_id = start()
    runs.finish_agent_run(run_id, status='success', usage=usage)
    saved = runs.get_agent_run(run_id)
    assert saved['cost'] is None and saved['currency'] is None and saved['cost_source'] is None


def test_unpriced_model_unknown_cost_and_provider_cost_takes_priority(local_runs):
    with database.get_db_conn() as conn:
        initialize_schema(conn)
    run_id = start()
    runs.finish_agent_run(run_id, status='success', usage={'prompt_tokens': 2000, 'completion_tokens': 500})
    assert runs.get_agent_run(run_id)['cost'] is None
    database.update_model_pricing('test-model', 2.5, 8)
    priced = start()
    runs.finish_agent_run(priced, status='success', usage={'prompt_tokens': 2000, 'completion_tokens': 500, 'cost': 0.007, 'currency': 'USD'})
    saved = runs.get_agent_run(priced)
    assert saved['cost'] == 0.007 and saved['currency'] == 'USD' and saved['cost_source'] == 'provider'


def test_private_api_requires_auth_for_list_and_detail(client):
    run_id = start()
    assert client.get('/api/agent-runs').status_code == 401
    assert client.get('/api/agent-runs/' + run_id).status_code == 401
    assert client.get('/api/public/agent-runs').status_code == 404


def test_list_metadata_only_filtering_and_detail(client):
    first = start()
    runs.finish_agent_run(first, status='error', output='sensitive-result', error='sensitive-error', details={'evidence': 'sensitive-details'})
    second = start('other', 'memory_review')
    authenticate(client)
    listed = client.get('/api/agent-runs', params={'config_id': 'cfg', 'purpose': 'decision', 'limit': 1, 'offset': 0}).json()
    assert listed['total'] == 1 and listed['runs'][0]['run_id'] == first
    assert not ({'messages', 'tools', 'output', 'error', 'details', 'messages_json', 'tools_json', 'details_json'} & set(listed['runs'][0]))
    assert 'sensitive' not in json.dumps(listed)
    assert client.get('/api/agent-runs', params={'purpose': 'memory_review'}).json()['runs'][0]['run_id'] == second
    assert client.get('/api/agent-runs', params={'limit': 1, 'offset': 1}).json()['runs'][0]['run_id'] == first
    detail = client.get('/api/agent-runs/' + first).json()['run']
    assert detail['messages'][0]['content'] == '真实输入'
    assert detail['output'] == 'sensitive-result'
    assert client.get('/api/agent-runs/' + 'f' * 32).status_code == 404


@pytest.mark.parametrize('params', [
    {'limit': 0}, {'limit': 101}, {'offset': -1}, {'offset': 10001},
    {'purpose': 'unknown'}, {'config_id': ''}, {'config_id': 'x' * 201}, {'limit': 'bad'},
])
def test_pagination_and_filters_are_bounded(client, params):
    authenticate(client)
    assert client.get('/api/agent-runs', params=params).status_code == 422


def test_retention_keeps_per_task_limit_and_expires_on_read(local_runs, monkeypatch):
    monkeypatch.setattr(runs, 'MAX_RUNS_PER_CONFIG', 2)
    stale = start()
    retained = [start(), start()]
    other = start('other')
    assert runs.get_agent_run(stale) is None
    assert {item['run_id'] for item in runs.list_agent_runs()['runs']} == {*retained, other}
    with database.get_db_conn() as conn:
        conn.execute("UPDATE agent_runs SET started_at = '2000-01-01T00:00:00+00:00' WHERE run_id = ?", (other,))
        conn.commit()
    assert runs.get_agent_run(other) is None
    assert runs.list_agent_runs()['total'] == 2


def test_schema_initialization_is_additive_and_idempotent(local_runs):
    with database.get_db_conn() as conn:
        initialize_schema(conn)
        conn.execute("INSERT INTO agent_runs (run_id, config_id, purpose, model, status, started_at, input_chars, message_chars, tool_chars, messages_json, tools_json) VALUES ('existing', 'cfg', 'decision', 'model', 'running', 'now', 0, 0, 0, '[]', '[]')")
        conn.commit()
        initialize_schema(conn)
        assert conn.execute('SELECT COUNT(*) FROM agent_runs').fetchone()[0] == 1
        assert conn.execute("SELECT name FROM sqlite_master WHERE name = 'mock_orders'").fetchone()
