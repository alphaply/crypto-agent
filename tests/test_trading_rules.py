import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import database, database_rules as rules
from backend.agent.rule_tools import manage_trading_rules
from backend.app.api.trading_rules import router
from backend.app.core.deps import get_current_user
from backend.app.services import trading_rules_service as service
from backend.utils.trade_operations import current_operation_id


@pytest.fixture
def local_rules(tmp_path, monkeypatch):
    monkeypatch.setattr(database, 'DB_NAME', str(tmp_path / 'rules.sqlite'))
    with database.get_db_conn() as conn:
        rules.initialize_trading_rules_schema(conn.cursor())
        conn.commit()
    monkeypatch.setattr(service.config, 'get_config_by_id', lambda cid: {'config_id': cid} if cid in {'cfg', 'other'} else None)


def add(content='Wait for confirmation', actor='human', **fields):
    return rules.change_trading_rules('cfg', [{'action': 'add', 'content': content, **fields}], actor=actor)[0]


def test_human_lock_model_edits_and_revision_history(local_rules):
    locked = add()
    assert locked['locked'] is True and locked['revision'] == 1
    change = {'action': 'update', 'rule_id': locked['rule_id'], 'expected_revision': 1, 'content': 'Changed'}
    with pytest.raises(rules.RuleLockedError):
        rules.change_trading_rules('cfg', [change], actor='model')
    unlocked = rules.change_trading_rules('cfg', [{**change, 'locked': False}], actor='human')[0]
    updated = rules.change_trading_rules('cfg', [{**change, 'expected_revision': unlocked['revision'], 'content': 'Use verified fills', 'reason': 'Observe fees'}], actor='model')[0]
    assert updated['revision'] == 3 and updated['updated_by'] == 'model'
    with pytest.raises(rules.RuleConflictError):
        rules.change_trading_rules('cfg', [change], actor='human')
    assert [x['revision'] for x in rules.trading_rule_history('cfg', locked['rule_id'])] == [3, 2, 1]
    assert rules.trading_rule_history('cfg', locked['rule_id'])[0]['reason'] == 'Observe fees'


def test_scoped_rules_atomic_batch_and_disabled_prompt(local_rules):
    rule = add(actor='model')
    assert rules.list_trading_rules('other') == []
    with pytest.raises(FileNotFoundError):
        rules.change_trading_rules('other', [{'action': 'disable', 'rule_id': rule['rule_id'], 'expected_revision': 1}], actor='human')
    with pytest.raises(rules.RuleConflictError):
        rules.change_trading_rules('cfg', [
            {'action': 'add', 'content': 'This must roll back'},
            {'action': 'disable', 'rule_id': rule['rule_id'], 'expected_revision': 99},
        ], actor='model')
    assert len(rules.list_trading_rules('cfg')) == 1
    assert rule['content'] in rules.format_trading_rules_context('cfg')
    rules.change_trading_rules('cfg', [{'action': 'disable', 'rule_id': rule['rule_id'], 'expected_revision': 1}], actor='model')
    assert rule['content'] not in rules.format_trading_rules_context('cfg')
    assert len(rules.trading_rule_history('cfg', rule['rule_id'])) == 2


def test_rule_tool_replay_is_idempotent_and_conflicting_id_rejected(local_rules):
    token = current_operation_id.set('call-one')
    try:
        args = dict(action='apply', changes=[{'action': 'add', 'content': 'Follow the trend', 'reason': 'Recent evidence'}], config_id='cfg')
        first = json.loads(manage_trading_rules.func(**args))
        assert json.loads(manage_trading_rules.func(**args)) == first
        assert len(rules.list_trading_rules('cfg')) == 1
        assert first['rules'][0]['locked'] is False
        with pytest.raises(rules.RuleConflictError):
            manage_trading_rules.func(**{**args, 'changes': [{'action': 'add', 'content': 'Different', 'reason': 'Other evidence'}]})
    finally:
        current_operation_id.reset(token)
    with pytest.raises(ValueError):
        manage_trading_rules.func(action='apply', changes=[{'action': 'add', 'content': 'test', 'locked': True, 'reason': 'reason'}], config_id='cfg')


def test_rule_api_auth_validation_history_and_stale_revision(local_rules):
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    assert client.get('/api/history/trading-rules', params={'config_id': 'cfg'}).status_code in {401, 403}
    app.dependency_overrides[get_current_user] = lambda: {'username': 'admin'}
    base = '/api/history/trading-rules'
    assert client.post(base, json={'config_id': 'cfg', 'content': ' '}).status_code == 422
    assert client.post(base, json={'config_id': 'missing', 'content': 'Rule'}).status_code == 404
    created = client.post(base, json={'config_id': 'cfg', 'content': 'Preserve risk budget'}).json()['rule']
    assert created['locked'] is True
    url = base + '/' + created['rule_id']
    updated = client.patch(url, json={'config_id': 'cfg', 'expected_revision': 1, 'locked': False})
    assert updated.status_code == 200 and updated.json()['rule']['revision'] == 2
    assert client.patch(url, json={'config_id': 'cfg', 'expected_revision': 1, 'enabled': False}).status_code == 409
    assert client.get(url + '/history', params={'config_id': 'other'}).status_code == 404
    assert len(client.get(url + '/history', params={'config_id': 'cfg'}).json()['history']) == 2
    assert client.get(base, params={'config_id': 'other'}).json()['rules'] == []
