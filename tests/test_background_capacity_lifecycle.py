import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from backend.utils import runtime_capacity as capacity
from backend.utils import execution_stream as streams
from backend.utils import news_context as news


def test_stream_rotation_retains_physical_worker_and_byte_commitment(monkeypatch):
    manager = capacity.CapacityManager(total=3, reserved=1, byte_limit=4096)
    monkeypatch.setattr(capacity, '_manager', manager)
    monkeypatch.setenv('RUNTIME_STREAM_BYTES', '1024')
    workers = {}
    monkeypatch.setattr(streams, '_workers', workers)
    threads = []
    class DeferredThread:
        def __init__(self, target, **_): self.target, self.live = target, False; threads.append(self)
        def start(self): self.live = True
        def is_alive(self): return self.live
    monkeypatch.setattr(streams.threading, 'Thread', DeferredThread)
    monkeypatch.setattr(streams, 'account_scope', lambda *_: 'scope')
    mt = SimpleNamespace(runtime_config={'mode':'REAL'}, market_type='swap', config_id='cfg',symbol='ETH/USDT:USDT',
        exchange=SimpleNamespace(secret='first',password='',id='binance'))
    assert streams.ensure_stream(mt)
    assert manager.snapshot()['active'] == 1 and manager.snapshot()['bytes'] == 1024
    mt.exchange.secret = 'rotated'
    assert streams.ensure_stream(mt) is False
    assert workers[('scope',mt.symbol)]['stop'].is_set()
    assert len(threads) == 1 and manager.snapshot()['active'] == 1
    # Run the actual worker's failure/finally path without opening a transport.
    monkeypatch.setattr(streams, 'ExecutionLedger', Mock(side_effect=RuntimeError('fixture')))
    threads[0].target()
    threads[0].live = False
    assert manager.snapshot()['active'] == manager.snapshot()['bytes'] == 0


def test_news_source_parse_and_cache_share_bytes_and_release_on_clear(monkeypatch):
    manager = capacity.CapacityManager(total=3, reserved=1, byte_limit=65536)
    monkeypatch.setattr(capacity, '_manager', manager)
    monkeypatch.setenv('RUNTIME_NEWS_WORK_BYTES','8192')
    monkeypatch.setenv('RUNTIME_NEWS_CACHE_BYTES','8192')
    monkeypatch.setenv('RUNTIME_NEWS_CACHE_ENTRIES','2')
    monkeypatch.setattr(news, '_load_persistent_cache', lambda _: None)
    monkeypatch.setattr(news, '_save_persistent_cache', lambda *_: None)
    for name in ('_memory_cache','_failure_cache','_digest_cache','_source_locks'):
        monkeypatch.setattr(news,name,{})
    from datetime import timedelta
    def fetch():
        assert manager.snapshot()['bytes'] >= 8192
        return [{'title':'synthetic'}]
    for index in range(8):
        assert news._fetch_cached(str(index),fetch,ttl=timedelta(seconds=1),stale_ttl=timedelta(seconds=1),now=news._utc_now())['status']=='ok'
    assert len(news._memory_cache) <= 2 and not news._source_locks
    assert manager.snapshot()['bytes'] <= 8192
    news._memory_cache.clear()
    assert manager.snapshot()['bytes'] == 0
