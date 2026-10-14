"""Admin lifecycle endpoints preserve structured conflicts and scoped recovery."""
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from backend.app.api import management as api
from backend.app.core.deps import get_current_user
from backend.config import config


@pytest.fixture
def client(monkeypatch):
    app = FastAPI()
    app.include_router(api.router)
    monkeypatch.setattr(config, 'get_config_by_id', lambda identity: {'config_id': identity} if identity in {'owner', 'receiver', 'mcp:profile'} else None)
    app.dependency_overrides[get_current_user] = lambda: {'sub': 'admin'}
    return TestClient(app)


def test_read_status_has_no_claim_or_exchange_action(client, monkeypatch):
    calls = []
    monkeypatch.setattr(api.visible_management, 'management_status', lambda identity: {'owner': identity, 'scopes': []})
    monkeypatch.setattr(api.visible_management, 'management_recovery_targets', lambda identity: [{'recovery_id': 'accepted'}])
    monkeypatch.setattr(api.visible_management, 'claim_management', lambda *a, **k: calls.append(a))
    result = client.get('/api/management/mcp:profile')
    assert result.status_code == 200 and result.headers['cache-control'] == 'no-store'
    assert result.json()['management']['owner'] == 'mcp:profile' and not calls
    assert client.get('/api/management/unknown').status_code == 404


def test_lifecycle_requires_authentication():
    app = FastAPI()
    app.include_router(api.router)
    client = TestClient(app)
    assert client.get('/api/management/owner').status_code == 401
    assert client.post('/api/management/owner/scopes/claim/release', json={'confirmed': True}).status_code == 401


def test_release_preserves_active_resource_conflict(client, monkeypatch):
    def release(owner, claim, *, confirmed):
        assert (owner, claim, confirmed) == ('owner', 'claim', True)
        return {'status': 'blocked', 'conflicts': ['active_or_unresolved_resources'], 'resources': {'unresolved': ['old-id']}}
    monkeypatch.setattr(api.visible_management, 'release_management_scope', release)
    result = client.post('/api/management/owner/scopes/claim/release', json={'confirmed': True})
    assert result.status_code == 409
    assert result.json()['detail']['resources']['unresolved'] == ['old-id']


def test_transfer_keeps_acceptance_and_receiver_identity(client, monkeypatch):
    def transfer(owner, claim, target, *, accepted):
        assert target == 'receiver'
        if not accepted:
            raise ValueError('Explicit acceptance is required')
        return {'status': 'transferred', 'owner': target, 'recovery_targets': [{'operation_id': 'original-id'}]}
    monkeypatch.setattr(api.visible_management, 'transfer_management_scope', transfer)
    path = '/api/management/owner/scopes/claim/transfer'
    assert client.post(path, json={'target_config_id': 'receiver'}).status_code == 409
    response = client.post(path, json={'target_config_id': 'receiver', 'accepted': True})
    assert response.status_code == 200 and response.json()['recovery_targets'][0]['operation_id'] == 'original-id'
    assert client.post(path, json={'target_config_id': 'unknown', 'accepted': True}).status_code == 404
    assert client.post(path, json={'target_config_id': 'receiver', 'accepted': True, 'force': True}).status_code == 422


def test_recovery_identity_is_receiver_scoped(client, monkeypatch):
    monkeypatch.setattr(api.visible_management, 'lookup_management_recovery', lambda owner, identity: {'source_owner': 'original'} if owner == 'receiver' and identity == 'handoff-id' else None)
    monkeypatch.setattr(api.visible_management, 'query_management_recovery', lambda *args: {'query_only': True, 'result': {'status': 'pending'}})
    assert client.get('/api/management/owner/recovery/handoff-id').status_code == 404
    response = client.get('/api/management/receiver/recovery/handoff-id')
    assert response.status_code == 200 and response.json()['query_only'] is True


def test_observation_failure_retains_ownership(client, monkeypatch):
    def failed(*args, **kwargs):
        raise RuntimeError('credential-bearing exchange transport exception')
    monkeypatch.setattr(api.visible_management, 'release_management_scope', failed)
    result = client.post('/api/management/owner/scopes/claim/release', json={'confirmed': True})
    assert result.status_code == 502
    assert 'credential-bearing' not in result.text
