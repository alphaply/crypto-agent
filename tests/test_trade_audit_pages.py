import json
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import database
from backend.utils.trade_audit import task_audit, live_audit
from backend.utils.trade_evidence import append_receipt
from backend.utils.trade_operations import run_once
from test_unified_trading import ledger


def test_large_original_and_latest_are_separate_fixed_character_pages(ledger):
    original = {'status': 'submitted', 'operation_id': 'audit-original', 'order_status': 'waiting_trigger',
                'filled_amount': 0, 'diagnostics': '中文证据' * 20000}
    run_once('cfg', 'ETH/USDT', 'audit-original', {'tool': 'close'}, lambda: original)
    first = task_audit('cfg', 'audit-original', length=2048)
    assert first['has_more'] and len(first['text']) == 2048
    latest = {**original, 'status': 'completed', 'order_status': 'filled', 'filled_amount': .03}
    with database.get_db_conn() as conn:
        append_receipt(conn, 'cfg', 'audit-original', latest)
        conn.commit()
    latest_page = task_audit('cfg', 'audit-original', view='latest')
    assert latest_page['version'] != first['version']
    text, cursor = first['text'], first['next_offset']
    while cursor is not None:
        page = task_audit('cfg', 'audit-original', offset=cursor, version=first['version'])
        assert page['version'] == first['version']
        assert len(page['text'].encode('utf-8')) <= 8192
        assert '\ufffd' not in page['text']
        text += page['text']
        cursor = page['next_offset']
    assert json.loads(text) == original
    with pytest.raises(ValueError):
        task_audit('cfg', 'audit-original', length=2049)
    with pytest.raises(FileNotFoundError):
        task_audit('other-task', 'audit-original')


def test_task_audit_http_requires_existing_login_and_does_not_fetch_exchange(ledger, monkeypatch):
    from backend.app.api.trade_audit import router
    from backend.app.core.deps import get_current_user
    app = FastAPI()
    app.include_router(router)
    forbidden = Mock(side_effect=AssertionError('Audit must not contact an exchange'))
    monkeypatch.setattr('backend.agent.agent_tools.MarketTool', forbidden)
    run_once('cfg', 'ETH/USDT', 'http-original', {'tool': 'close'}, lambda: {'status': 'submitted'})
    with TestClient(app) as client:
        assert client.get('/api/trade-receipts/cfg/http-original/audit').status_code == 401
        app.dependency_overrides[get_current_user] = lambda: {'sub': 'administrator'}
        result = client.get('/api/trade-receipts/cfg/http-original/audit')
        assert result.status_code == 200 and result.json()['read_only']
        assert client.get('/api/trade-receipts/other/http-original/audit').status_code == 404
        assert client.get('/api/trade-receipts/cfg/http-original/audit?length=2049').status_code == 422
    forbidden.assert_not_called()


def test_live_audit_binds_original_principal_profile_and_external_id(ledger, monkeypatch):
    from backend.mcp import store, service
    monkeypatch.setattr(service, 'require', lambda caller, scope, profile: None)
    store.reserve_operation('fixture-profile', 'external-original', 'alice', {'symbol': 'ETH/USDT', 'tool': 'close'})
    store.complete_operation('fixture-profile', 'external-original', {'status': 'submitted', 'order_id': 'algo:original'})
    alice = {'id': 'alice', 'scopes': ['read'], 'profile_ids': ['fixture-profile']}
    assert live_audit(alice, 'fixture-profile', 'external-original')['read_only']
    with pytest.raises(FileNotFoundError):
        live_audit({**alice, 'id': 'bob'}, 'fixture-profile', 'external-original')
    with pytest.raises(FileNotFoundError):
        live_audit(alice, 'another-profile', 'external-original')
