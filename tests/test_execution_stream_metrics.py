import asyncio
import json
import threading
from types import SimpleNamespace

from test_execution_ledger import local_db
from test_position_protection import service, entry
from backend.utils.execution_stream import consume, ensure_stream
from backend.utils.execution_metrics import update_post_exit
import backend.database as database


def test_stream_ingests_actual_fills_and_closes_transport():
    stop = threading.Event()
    calls = []

    class Client:
        async def watch_my_trades(self, symbol):
            stop.set()
            return [{'id': 'trade'}]

        async def close(self):
            calls.append('closed')

    ledger = SimpleNamespace(symbol='ETH/USDT:USDT', ingest=lambda values: calls.extend(values))
    asyncio.run(consume(Client(), ledger, stop))
    assert calls == [{'id': 'trade'}, 'closed']


def test_spot_does_not_start_perpetual_stream():
    ensure_stream(SimpleNamespace(runtime_config={'mode': 'SPOT_DCA'}))


def test_observed_excursions_and_flat_reason(service):
    svc, ex = service
    ex.quantity = 1
    svc.open('ETH/USDT', entry())
    with database.get_db_conn() as conn:
        initial = conn.execute('SELECT payload FROM execution_episodes').fetchone()
    assert json.loads(initial[0])['samples'] == 1
    ex.quantity = 0
    svc.reconcile_all()
    with database.get_db_conn() as conn:
        result = json.loads(conn.execute('SELECT payload FROM execution_episodes').fetchone()[0])
    assert result['flat_observed_at'] >= result['first_observed_at']
    assert result['exit_reason'] == 'unknown'  # No evidence of a stop/manual reason, so don't guess.


def test_post_exit_samples_only_closed_bars_at_fixed_horizons(local_db):
    value = {'flat_observed_at': 6000000, 'post_exit': {}}
    with database.get_db_conn() as conn:
        conn.execute('INSERT INTO execution_episodes VALUES(?,?,?,?,?)', ('e', 'scope', 'cfg', 'ETH/USDT:USDT', json.dumps(value)))
        conn.commit()
    ex = SimpleNamespace(fetch_ohlcv=lambda *args, **kwargs: [[6000000 + i * 60000, 100, 110, 90, 100 + i, 1] for i in range(65)])
    update_post_exit(ex, 'scope', 'ETH/USDT:USDT', now_ms=6000000 + 16 * 60000)
    with database.get_db_conn() as conn:
        result = json.loads(conn.execute('SELECT payload FROM execution_episodes').fetchone()[0])
    assert list(result['post_exit']) == ['15']
    assert result['post_exit']['15']['bar_close_ms'] == 6000000 + 15 * 60000
