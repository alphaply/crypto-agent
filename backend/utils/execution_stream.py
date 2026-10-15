"""Perpetual user-trade stream; REST ledger remains the completeness authority."""
import asyncio
import threading
import time
import hashlib

from backend.utils.execution_ledger import ExecutionLedger, account_scope
from backend.utils.logger import setup_logger
from backend.utils.runtime_capacity import (get_capacity_manager, CapacityBusy,
    reserve_bytes, positive_setting, current_capacity)

logger = setup_logger('ExecutionStream')
_workers = {}
_lock = threading.Lock()


async def consume(client, ledger, stop):
    """CCXT Pro manages user stream authentication and reconnect transport."""
    backoff = 1
    try:
        while not stop.is_set():
            try:
                trades = await asyncio.wait_for(client.watch_my_trades(ledger.symbol), timeout=1)
                ledger.ingest(trades or [])
                backoff = 1
            except asyncio.TimeoutError:
                # No new trade is normal. REST independently catches any missed fills.
                continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(f'User-trade stream interrupted ({type(exc).__name__}); REST catch-up remains active')
                deadline = time.monotonic() + backoff
                while not stop.is_set() and time.monotonic() < deadline:
                    await asyncio.sleep(min(.25, max(0, deadline - time.monotonic())))
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
            if _workers[key]['thread'].is_alive():
                return False
            _workers.pop(key)
        try:
            lease = get_capacity_manager().acquire('stream', timeout=0)
        except CapacityBusy:
            return False
        try:
            retained = reserve_bytes('stream', 8 * 1024 * 1024)
        except BaseException:
            lease.release()
            return False
        stop = threading.Event()

        def run():
            lease.mark_started()
            token = current_capacity.set(lease)
            async def start():
                import ccxt.pro as pro
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
            finally:
                current_capacity.reset(token)
                retained.release()
                lease.release()

        thread = threading.Thread(target=run, name='execution-user-stream', daemon=True)
        _workers[key] = {'thread': thread, 'stop': stop, 'seen': time.monotonic(), 'fingerprint': fingerprint,
                         'config_id': mt.config_id}
        try:
            thread.start()
        except BaseException:
            _workers.pop(key, None)
            retained.release()
            lease.release()
            raise
        return True


def stop_unused_streams():
    from backend.config import config
    with _lock:
        for old_key, worker in list(_workers.items()):
            cfg = config.get_config_by_id(worker.get('config_id')) if worker.get('config_id') else None
            if not cfg or time.monotonic() - worker['seen'] > 180:
                worker['stop'].set()
                if not worker['thread'].is_alive():
                    del _workers[old_key]
