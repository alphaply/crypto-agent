"""Clear a display snapshot without clearing strategy or execution evidence."""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import database
from backend.agent.decision_context import load_decision_memory
from backend.app.api.history import router
from backend.app.core.deps import get_current_user
from test_context_projections import CFG, sources, simulation
from test_decision_context_availability import publish


def client(authenticated=True):
    app = FastAPI()
    app.include_router(router)
    if authenticated:
        app.dependency_overrides[get_current_user] = lambda: {'sub': 'admin'}
    return TestClient(app)


def test_clear_is_scoped_idempotent_and_preserves_published_memory_and_evidence(sources):
    original = database.get_short_memories('cfg', 1)[0]
    key = {'config_id': 'cfg', 'bucket_start': original['bucket_start']}
    with database.get_db_conn() as conn:
        conn.execute("UPDATE short_memories SET position_summary='long old receipt snapshot'")
        conn.commit()
    database.save_short_memory(original['bucket_start'], original['bucket_end'], 'ETH/USDT',
                               'other', 'Other strategy', 'Other snapshot', 1)
    publish()
    before = load_decision_memory('cfg', CFG)
    with database.get_db_conn() as conn:
        tables = ['short_memories', 'summaries', 'trade_action_runs', 'decision_context_projections']
        saved = {table: [dict(row) for row in conn.execute(f'SELECT * FROM {table}')] for table in tables}
    api = client()
    for _ in range(2):
        response = api.request('DELETE', '/api/history/short-memories/execution-snapshot', json=key)
        assert response.status_code == 200 and response.json()['updated'] == 1
    with database.get_db_conn() as conn:
        for row in saved['short_memories']:
            if row['config_id'] == 'cfg':
                row['position_summary'] = ''
        for table in tables:
            assert [dict(row) for row in conn.execute(f'SELECT * FROM {table}')] == saved[table]
    assert load_decision_memory('cfg', CFG) == before
    response = api.request('DELETE', '/api/history/short-memories/execution-snapshot',
                           json={**key, 'bucket_start': 'missing'})
    assert response.status_code == 404


def test_text_only_edit_preserves_snapshot_and_does_not_restore_cleared_copy(sources):
    row = database.get_short_memories('cfg', 1)[0]
    key = {'config_id': 'cfg', 'bucket_start': row['bucket_start']}
    api = client()
    response = api.put('/api/history/short-memories', json={**key, 'market_summary': 'strategy', 'position_summary': 'snapshot'})
    assert response.status_code == 200
    assert api.put('/api/history/short-memories', json={**key, 'market_summary': 'new strategy'}).status_code == 200
    assert database.get_short_memory(**key)['position_summary'] == 'snapshot'
    assert api.request('DELETE', '/api/history/short-memories/execution-snapshot', json=key).status_code == 200
    assert api.put('/api/history/short-memories', json={**key, 'market_summary': 'edited after clear'}).status_code == 200
    saved = database.get_short_memory(**key)
    assert saved['position_summary'] == '' and saved['market_summary'] == 'edited after clear'


def test_anonymous_clear_cannot_write(sources):
    before = database.get_short_memories('cfg', 1)
    response = client(False).request('DELETE', '/api/history/short-memories/execution-snapshot',
                                     json={'config_id': 'cfg', 'bucket_start': before[0]['bucket_start']})
    assert response.status_code in {401, 403}
    assert database.get_short_memories('cfg', 1) == before
