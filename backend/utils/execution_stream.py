"""Perpetual user-trade stream; REST ledger remains the completeness authority."""
import asyncio
import threading
import time
import hashlib

from backend.utils.execution_ledger import ExecutionLedger, account_scope
from backend.utils.logger import setup_logger

logger = setup_logger('ExecutionStream')
_workers = {}
_lock = threading.Lock()


async def consume(client, ledger, stop):
    """CCXT Pro manages user stream authentication and reconnect transport."""
    backoff = 1
    try:
        while not stop.is_set():
            try:
                trades = await asyncio.wait_for(client.watch_my_trades(ledger.symbol), timeout=60)
                ledger.ingest(trades or [])
                backoff = 1
            except asyncio.TimeoutError:
                # No new trade is normal. REST independently catches any missed fills.
                continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(f'User-trade stream interrupted ({type(exc).__name__}); REST catch-up remains active')
                await asyncio.sleep(backoff)
                backoff = min(30, backoff * 2)
    finally:
        await client.close()


def ensure_stream(mt):
    if mt.runtime_config.get('mode', '').upper() != 'REAL' or mt.market_type != 'swap':
        return
    ex = mt.exchange
    key = (account_scope(ex, mt.config_id), mt.symbol)
    fingerprint = hashlib.sha256(f"{ex.secret}:{getattr(ex, 'password', '')}".encode()).hexdigest()
    with _lock:
        if key in _workers and _workers[key]['thread'].is_alive() and _workers[key]['fingerprint'] == fingerprint:
            _workers[key]['seen'] = time.monotonic()
            return
        if key in _workers:
            _workers[key]['stop'].set()
        stop = threading.Event()

        def run():
            import ccxt.pro as pro
            async def start():
                name = 'binanceusdm' if ex.id in {'binance', 'binanceusdm'} else ex.id
                options = {'apiKey': ex.apiKey, 'secret': ex.secret, 'password': getattr(ex, 'password', None),
                           'enableRateLimit': True, 'newUpdates': True, 'options': dict(ex.options),
                           'timeout': ex.timeout}
                client = getattr(pro, name)(options)
                ledger = ExecutionLedger(ex, mt.config_id, mt.symbol)
                await consume(client, ledger, stop)
            try:
                asyncio.run(start())
            except Exception as exc:
                logger.warning(f'User-trade stream stopped ({type(exc).__name__}); periodic REST will continue')

        thread = threading.Thread(target=run, name='execution-user-stream', daemon=True)
        _workers[key] = {'thread': thread, 'stop': stop, 'seen': time.monotonic(), 'fingerprint': fingerprint}
        thread.start()


def stop_unused_streams():
    with _lock:
        for old_key, worker in list(_workers.items()):
            if time.monotonic() - worker['seen'] > 180:
                worker['stop'].set()
                if not worker['thread'].is_alive():
                    del _workers[old_key]
