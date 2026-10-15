"""Maintenance startup must not initialize imported Paper balances."""
import pytest
from fastapi import FastAPI


@pytest.mark.parametrize('value', ['true', '1', 'yes', 'on'])
def test_imported_paper_key_does_not_initialize_account_until_pause_ends(pg, monkeypatch, value):
    from backend import storage
    from backend.mcp import server, store
    monkeypatch.setattr(server, '_server', None)
    monkeypatch.setattr(server, '_public_url', None)
    store.put('key', 'imported-paper', {'key_type': 'paper', 'paper_namespace': 'imported-paper', 'revoked': False})
    monkeypatch.setenv('DEPLOYMENT_MAINTENANCE', value)
    server.install_mcp(FastAPI())
    with storage.get_connection() as conn:
        for table in ('paper_accounts', 'paper_events', 'paper_scan_progress'):
            assert conn.execute('SELECT count(*) FROM ' + table).fetchone()[0] == 0
    monkeypatch.setenv('DEPLOYMENT_MAINTENANCE', 'false')
    server.install_mcp(FastAPI())
    with storage.get_connection() as conn:
        assert conn.execute("SELECT count(*) FROM paper_accounts WHERE namespace='imported-paper'").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM paper_events WHERE namespace='imported-paper'").fetchone()[0] == 1
