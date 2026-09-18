import sqlite3
import time

import jwt
import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from backend.app.core.security import JWT_ALGORITHM, JWT_SECRET, create_access_token
from backend.app.main import app
from backend.app.services import database_export
from backend.utils.indicators import calc_adx, calc_atr, calc_rsi, wilder_rma


def test_wilder_seed_and_recurrence():
    result = wilder_rma(pd.Series([2., 4., 9., 5.]), 3)
    assert result.iloc[:2].isna().all()
    assert result.iloc[2] == 5
    assert result.iloc[3] == 5
    frame = pd.DataFrame({'high': [2, 4, 9, 5], 'low': [0, 0, 0, 0], 'close': [1, 2, 4, 3]})
    assert calc_atr(frame, 3).iloc[2] == 5


def test_rsi_flat_and_monotonic_series():
    flat = calc_rsi(pd.Series([100.] * 30))
    assert flat.iloc[:14].isna().all()
    assert flat.iloc[14:].eq(50).all()
    assert calc_rsi(pd.Series(np.arange(30, dtype=float))).iloc[-1] == 100
    assert calc_rsi(pd.Series(np.arange(30, 0, -1, dtype=float))).iloc[-1] == 0


def test_rsi_and_adx_match_hand_calculated_wilder_seeds():
    rsi = calc_rsi(pd.Series([1., 2., 1., 3., 2.]), period=3)
    assert rsi.iloc[3] == pytest.approx(75)
    assert rsi.iloc[4] == pytest.approx(100 - 100 / 2.2)
    frame = pd.DataFrame({'high': [10, 12, 13, 12, 15, 14],
                          'low': [8, 9, 10, 8, 12, 11],
                          'close': [9, 11, 12, 10, 14, 12]})
    adx, _, _ = calc_adx(frame, period=3)
    assert adx.iloc[:4].isna().all()
    assert adx.iloc[4] == pytest.approx((100 + 0 + 900 / 17) / 3)


def test_download_cookie_scope_expiry_and_consistent_snapshot(tmp_path, monkeypatch):
    source = tmp_path / 'source.db'
    conn = sqlite3.connect(source)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('CREATE TABLE evidence(value TEXT)')
    conn.execute("INSERT INTO evidence VALUES ('committed WAL record')")
    conn.commit()
    monkeypatch.setattr(database_export.database, 'DB_NAME', str(source))
    client = TestClient(app)
    assert client.get('/api/config/database/download').status_code == 401
    headers = {'Authorization': f'Bearer {create_access_token()}'}
    ticket = client.post('/api/config/database/download-ticket', headers=headers)
    assert ticket.status_code == 204
    assert 'HttpOnly' in ticket.headers['set-cookie']
    cookie = client.cookies.get('db_download')
    assert client.get('/api/config', headers={'Authorization': f'Bearer {cookie}'}).status_code == 401
    response = client.get('/api/config/database/download')
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'no-store'
    assert int(response.headers['content-length']) == len(response.content)
    snapshot = tmp_path / 'snapshot.db'
    snapshot.write_bytes(response.content)
    with sqlite3.connect(snapshot) as exported:
        assert exported.execute('SELECT value FROM evidence').fetchone()[0] == 'committed WAL record'
        assert exported.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    expired = jwt.encode({'sub': 'admin', 'aud': 'database-download', 'exp': int(time.time()) - 10}, JWT_SECRET, algorithm=JWT_ALGORITHM)
    response = client.get('/api/config/database/download', headers={'Cookie': f'db_download={expired}'})
    assert response.status_code == 401
    conn.close()


@pytest.mark.parametrize('name', ['../outside.txt', '/tmp/outside.txt', 'C:\\outside.txt', '..\\outside.txt', 'folder/name.txt'])
def test_prompt_routes_reject_paths(name):
    client = TestClient(app)
    headers = {'Authorization': f'Bearer {create_access_token()}'}
    assert client.get('/api/config/prompts/content', params={'name': name}, headers=headers).status_code == 400
    assert client.put('/api/config/prompts', json={'name': name, 'content': 'invalid'}, headers=headers).status_code == 400
    assert client.request('DELETE', '/api/config/prompts', json={'name': name}, headers=headers).status_code == 400
