"""Manual edits reach both views and decision input without changing audit snapshots."""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import database
from backend.agent.decision_context import load_decision_memory
from backend.app.api.history import router
from backend.app.core.deps import get_current_user
from backend.app.services.public_service import build_public_short_memories_payload
from backend.config import config
from backend.utils import trade_evidence


def test_authenticated_edit_publishes_same_text_for_all_readers(pg, monkeypatch):
    cfg = {'config_id': 'cfg', 'symbol': 'ETH/USDT', 'mode': 'STRATEGY'}
    monkeypatch.setattr(config, 'get_config_by_id', lambda cid: cfg if cid == 'cfg' else None)
    monkeypatch.setattr(trade_evidence, 'configured_account_scope', lambda cid: 'mock:' + cid)
    key = '2026-10-29 13:00:00.123456'
    for cid in ('cfg', 'other'):
        database.save_short_memory(key, '2026-10-29 13:00:00', 'ETH/USDT', cid,
                                   'Old memory', 'Preserved historical snapshot', 1)
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    payload = dict(config_id='cfg', bucket_start=key, market_summary='GDP 2026-10-29 12:30 UTC; not Beijing 12:30.')
    assert client.put('/api/history/short-memories', json=payload).status_code == 401
    app.dependency_overrides[get_current_user] = lambda: {'sub': 'admin'}
    result = client.put('/api/history/short-memories', json=payload)
    assert result.status_code == 200 and result.json()['updated'] == 1
    saved = database.get_short_memory('cfg', key)
    assert saved['market_summary'] == payload['market_summary']
    assert saved['position_summary'] == 'Preserved historical snapshot'
    assert database.get_short_memory('other', key)['market_summary'] == 'Old memory'
    public = build_public_short_memories_payload(config_id='cfg')['short_memories'][0]
    admin = client.get('/api/history/short-memories', params={'config_id': 'cfg'}).json()['short_memories'][0]
    assert public['market_summary'] == admin['market_summary'] == payload['market_summary']
    assert payload['market_summary'] in load_decision_memory('cfg', cfg).short_memory_text
    assert 'Old memory' not in load_decision_memory('cfg', cfg).short_memory_text
    assert client.put('/api/history/short-memories', json={**payload, 'bucket_start': 'missing'}).status_code == 404
