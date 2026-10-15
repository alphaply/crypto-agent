"""Offline catalogue fixtures also support the isolated capacity harness."""
import copy
import gc
import threading
import time
import weakref
from collections import OrderedDict

import pytest

from backend.utils import public_catalogue as catalogues, runtime_capacity as capacity
from backend.mcp import exchange_runtime


class OfflineExchange:
    id = 'binance'
    timeout = 1000
    precisionMode = 4
    urls = {'api': {'public': 'https://binance.fixture.invalid'}}
    def __init__(self, *, credential='key', market_type='spot', counter=None):
        self.apiKey, self.secret = credential, 'fixture-secret'
        self.options = {'defaultType': market_type, 'timeDifference': 5}
        self.markets = self.currencies = None
        self.counter = counter if counter is not None else []

    def load_markets(self):
        self.counter.append('load')
        time.sleep(.01)
        self.markets = {'ETH/USDT': {'id': 'ETHUSDT', 'symbol': 'ETH/USDT', 'base': 'ETH',
                                   'quote': 'USDT', 'baseId': 'ETH', 'quoteId': 'USDT',
                                   'spot': True, 'swap': False, 'contract': False,
                                   'contractSize': 1, 'precision': {'amount': .001, 'price': .01},
                                   'limits': {'amount': {'min': .001}},
                                   'info': {'orderTypes': ['LIMIT', 'MARKET', 'STOP_LOSS_LIMIT'],
                                            'ocoAllowed': True, 'filters': [{'filterType': 'LOT_SIZE', 'stepSize': '.001'}],
                                            'unused_directory_payload': 'x' * 2048}}}
        self.currencies = {'ETH': {'id': 'ETH', 'code': 'ETH', 'precision': .001, 'info': {'large': 'unused'}}}

    def set_markets(self, markets, currencies):
        self.markets, self.currencies = markets, currencies


@pytest.fixture
def local_capacity(monkeypatch):
    # A new capacity manager must own a new cache/coordinator namespace. Old
    # test adapters may still be retained by unrelated fixtures/tracebacks;
    # their real references must not be silently closed to make these tests fit.
    for name in ('_accounts', '_quotes', '_quote_refreshes'):
        monkeypatch.setattr(exchange_runtime, name, OrderedDict())
    monkeypatch.setattr(catalogues, '_catalogues', OrderedDict())
    monkeypatch.setattr(catalogues, '_consumer_leases', weakref.WeakKeyDictionary())
    monkeypatch.setattr(catalogues, '_client_slots', weakref.WeakKeyDictionary())
    monkeypatch.setattr(catalogues, '_refresh_peak', 0)
    value = capacity.CapacityManager(total=8, reserved=2, byte_limit=256 * 1024 * 1024)
    monkeypatch.setattr(capacity, '_manager', value)
    yield value
    gc.collect()
    catalogues.retire_catalogues(all_idle=True)
    exchange_runtime.retire_runtime_caches(all_idle=True)


def test_public_key_ignores_credentials_and_copy_isolation(local_capacity):
    first, second = OfflineExchange(), OfflineExchange(credential='different-key')
    assert catalogues.public_key(first) == catalogues.public_key(second)
    counter = first.counter
    second.counter = counter
    observed = catalogues.load_public_catalogue(first)
    assert catalogues.load_public_catalogue(second) == observed
    assert counter == ['load']
    assert first.apiKey != second.apiKey
    first.markets['ETH/USDT']['precision']['amount'] = 99
    first.currencies['ETH']['precision'] = 99
    first.options['timeDifference'] = 99
    assert second.markets['ETH/USDT']['precision']['amount'] == .001
    assert second.currencies['ETH']['precision'] == .001
    assert second.options['timeDifference'] == 5
    assert second.markets['ETH/USDT']['info']['ocoAllowed']
    assert 'unused_directory_payload' not in second.markets['ETH/USDT']['info']
    assert local_capacity.snapshot()['byte_classes']['client'] > 0
    catalogues.release_catalogue_client(first)
    catalogues.release_catalogue_client(second)


def test_same_key_cold_load_is_single_flight_across_worker_roles(local_capacity):
    from concurrent.futures import ThreadPoolExecutor
    counter = []
    clients = [OfflineExchange(counter=counter) for _ in range(4)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [capacity.submit_capacity(pool, role, catalogues.load_public_catalogue, client)
                   for role, client in zip(('agent', 'mcp_read', 'maintenance', 'paper'), clients)]
        times = [future.result() for future in futures]
    assert len(set(times)) == 1 and counter == ['load']
    for client in clients:
        catalogues.release_catalogue_client(client)


def test_refresh_reserves_old_new_and_consumer_peak(local_capacity, monkeypatch):
    client = OfflineExchange()
    catalogues.load_public_catalogue(client)
    old_bytes = local_capacity.snapshot()['bytes']
    key = catalogues.public_key(client)
    catalogues._catalogues[key]['expires_at'] = 0
    seen = []
    original = client.load_markets
    def load():
        seen.append(local_capacity.snapshot()['bytes'])
        original()
    monkeypatch.setattr(client, 'load_markets', load)
    catalogues.load_public_catalogue(client)
    assert seen[0] > old_bytes
    assert local_capacity.snapshot()['peaks']['bytes'] > old_bytes
    assert client.counter == ['load', 'load']
    catalogues.release_catalogue_client(client)


def test_catalogue_bytes_reject_before_compact_copy(local_capacity, monkeypatch):
    client = OfflineExchange()
    monkeypatch.setenv('RUNTIME_CATALOGUE_BYTES', '100')
    monkeypatch.setattr(catalogues.copy, 'deepcopy', lambda *_: pytest.fail('overlarge retained row was copied'))
    with pytest.raises(capacity.CapacityBusy, match='catalogue_bytes'):
        catalogues.load_public_catalogue(client)
    assert catalogues.catalogue_diagnostics()['bytes'] == 0
    catalogues.release_catalogue_client(client)
    assert local_capacity.snapshot()['bytes'] == 0


def test_retired_consumers_and_keys_release_bytes(local_capacity):
    client = OfflineExchange()
    catalogues.load_public_catalogue(client)
    catalogues.release_catalogue_client(client)
    catalogues.retire_catalogues(all_idle=True)
    assert local_capacity.snapshot()['bytes'] == 0
    assert catalogues.catalogue_diagnostics()['entries'] == 0


def test_http_limit_stops_stream_before_ccxt_parse(local_capacity, monkeypatch):
    monkeypatch.setenv('RUNTIME_HTTP_RESPONSE_BYTES', '8')
    class Response:
        headers = {}
        closed = False
        def iter_content(self, chunk_size):
            yield b'12345678'
            yield b'9'
            pytest.fail('read beyond limit')
        def close(self):
            self.closed = True
    response = Response()
    class Session:
        def request(self, *args, **kwargs):
            assert kwargs['stream'] is True
            return response
    client = OfflineExchange()
    client.session = Session()
    catalogues.install_bounded_http(client)
    with pytest.raises(capacity.CapacityBusy, match='response_bytes'):
        client.session.request('GET', 'https://fixture.invalid')
    assert response.closed and local_capacity.snapshot()['bytes'] == 0


def test_coordinator_with_active_lock_is_not_retired(local_capacity):
    class Transport:
        timeout, rateLimit = 1000, 1
        def fetch(self, *args):
            return {'ok': True}
    client = Transport()
    exchange_runtime.install_transport(client, 'active-resource-fixture')
    coordinator = exchange_runtime._accounts['active-resource-fixture']
    coordinator.network.acquire()
    try:
        exchange_runtime.retire_runtime_caches(all_idle=True)
        assert exchange_runtime._accounts['active-resource-fixture'] is coordinator
        assert not exchange_runtime.close_runtime_client(client)
    finally:
        coordinator.network.release()
    assert exchange_runtime.close_runtime_client(client)
    exchange_runtime.retire_runtime_caches(all_idle=True)
    assert 'active-resource-fixture' not in exchange_runtime._accounts


def test_quote_refresh_single_flight_reserves_overlap_and_failed_refresh_keeps_time(local_capacity):
    from concurrent.futures import ThreadPoolExecutor
    calls = []
    def load():
        calls.append(local_capacity.snapshot()['bytes'])
        time.sleep(.02)
        return {'last': 7}
    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(lambda _: exchange_runtime.observe('shared-fixture', 'ticker', load, 2), range(4)))
    assert len(calls) == 1 and len({r[1] for r in results}) == 1
    entry = exchange_runtime._quotes[('shared-fixture', 'ticker')]
    old_time, old_bytes = entry['observed_at'], local_capacity.snapshot()['bytes']
    entry['expires_at'] = 0
    def failed():
        assert local_capacity.snapshot()['bytes'] > old_bytes
        raise TimeoutError('fixture')
    with pytest.raises(TimeoutError):
        exchange_runtime.observe('shared-fixture', 'ticker', failed, 2)
    assert entry['observed_at'] == old_time
    assert exchange_runtime._quotes[('shared-fixture', 'ticker')] is entry


def test_coordinator_entry_limit_pins_active_clients_and_reclaims_rotated_keys(local_capacity, monkeypatch):
    monkeypatch.setenv('RUNTIME_COORDINATOR_ENTRIES', '2')
    class Transport:
        timeout, rateLimit = 1000, 1
        def fetch(self, *args):
            return {}
    clients = [Transport(), Transport(), Transport()]
    exchange_runtime.install_transport(clients[0], 'rotation-a')
    exchange_runtime.install_transport(clients[1], 'rotation-b')
    with pytest.raises(capacity.CapacityBusy, match='coordinator_entries'):
        exchange_runtime.install_transport(clients[2], 'rotation-c')
    exchange_runtime.close_runtime_client(clients[0])
    exchange_runtime.install_transport(clients[2], 'rotation-c')
    assert 'rotation-a' not in exchange_runtime._accounts
    for client in clients[1:]:
        assert exchange_runtime.close_runtime_client(client)


def test_real_ccxt_clients_keep_precision_contract_units_and_native_capabilities(local_capacity, monkeypatch):
    import ccxt
    source = OfflineExchange()
    source.load_markets()
    market = source.markets['ETH/USDT']
    market.update(type='swap', spot=False, swap=True, linear=True, inverse=False, contract=True,
                  settle='USDT', settleId='USDT', contractSize=.01, active=True, expiry=None,
                  symbol='ETH/USDT:USDT')
    market['info'].update(uly='ETH-USDT', instFamily='ETH-USDT', pair='ETHUSDT')
    source.markets = {'ETH/USDT:USDT': market}
    source.load_markets = lambda: None
    monkeypatch.setattr(catalogues, '_public_loader', lambda *_: source)
    # Keep the offline loader separate from consumer sessions.
    source.session = type('Session', (), {'close': lambda self: None})()
    first, second = ccxt.binanceusdm({'apiKey': 'a'}), ccxt.binanceusdm({'apiKey': 'b'})
    catalogues.load_public_catalogue(first, market_type='swap')
    catalogues.load_public_catalogue(second, market_type='swap')
    assert first.amount_to_precision('ETH/USDT:USDT', 1.2345) == '1.234'
    assert first.price_to_precision('ETH/USDT:USDT', 123.456) == '123.46'
    assert first.market('ETH/USDT:USDT')['contractSize'] == .01
    assert first.market('ETH/USDT:USDT')['expiry'] is None
    assert first.market('ETH/USDT:USDT')['info']['uly'] == 'ETH-USDT'
    first.markets['ETH/USDT:USDT']['precision']['price'] = 5
    assert second.market('ETH/USDT:USDT')['precision']['price'] == .01
    for client in (first, second):
        exchange_runtime.close_runtime_client(client)


def test_non_mcp_markettool_initializers_share_catalogue_and_close_independent_clients(local_capacity, monkeypatch):
    import ccxt
    from backend.utils.market_data import MarketTool
    calls = []
    class Client(OfflineExchange):
        rateLimit = 1
        def __init__(self, config):
            super().__init__(credential=config['apiKey'], counter=calls)
            self.options = config['options']
            self.secret = config['secret']
        def fetch(self, *args, **kwargs):
            pytest.fail('offline fixture cannot reach the network')
    monkeypatch.setattr(ccxt, 'binance', Client)
    first = MarketTool(exchange_profile={'exchange': 'binance', 'market_type': 'spot',
                                         'api_key': 'one', 'secret': 'fixture-one'})
    second = MarketTool(exchange_profile={'exchange': 'binance', 'market_type': 'spot',
                                          'api_key': 'two', 'secret': 'fixture-two'})
    assert calls == ['load']
    assert first.market_observed_at == second.market_observed_at
    first.exchange.markets['ETH/USDT']['precision']['price'] = 10
    assert second.exchange.markets['ETH/USDT']['precision']['price'] == .01
    assert first.close() and second.close()
    exchange_runtime.retire_runtime_caches(all_idle=True)
    assert catalogues.catalogue_diagnostics()['clients'] == 0


def test_backend_ccxt_loads_cannot_bypass_common_public_catalogue():
    import ast
    from pathlib import Path
    root = Path(__file__).parents[1] / 'backend'
    callers = []
    for path in root.rglob('*.py'):
        tree = ast.parse(path.read_text(encoding='utf-8-sig'))
        if any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
               and node.func.attr == 'load_markets' for node in ast.walk(tree)):
            callers.append(path.relative_to(root).as_posix())
    assert callers == ['utils/public_catalogue.py']


def test_compact_consumer_allocation_peak_is_smaller_than_full_metadata_copy():
    import tracemalloc
    markets = {f'COIN{index}/USDT': {
        'symbol': f'COIN{index}/USDT', 'spot': True, 'precision': {'amount': .01},
        'info': {'orderTypes': ['LIMIT', 'MARKET'], 'unused_payload': list(range(512))}}
        for index in range(256)}
    compact = catalogues._compact(markets, limit=8 * 1024 * 1024)
    def peak(value):
        gc.collect()
        tracemalloc.start()
        try:
            first, second = copy.deepcopy(value), copy.deepcopy(value)
            result = tracemalloc.get_traced_memory()[1]
            assert first is not second
            return result
        finally:
            tracemalloc.stop()
    full_peak, compact_peak = peak(markets), peak(compact)
    assert compact_peak < full_peak / 2


def test_client_slots_are_bounded_until_actual_close(local_capacity, monkeypatch):
    monkeypatch.setenv('RUNTIME_CLIENT_ENTRIES', '1')
    first, second = OfflineExchange(), OfflineExchange()
    catalogues.load_public_catalogue(first)
    with pytest.raises(capacity.CapacityBusy, match='client_entries'):
        catalogues.load_public_catalogue(second)
    catalogues.release_catalogue_client(first)
    catalogues.load_public_catalogue(second)
    assert catalogues.catalogue_diagnostics()['clients'] == 1
    catalogues.release_catalogue_client(second)


def test_failed_expired_catalogue_refresh_preserves_original_observation(local_capacity, monkeypatch):
    client = OfflineExchange()
    observed = catalogues.load_public_catalogue(client)
    entry = catalogues._catalogues[catalogues.public_key(client)]
    entry['expires_at'] = 0
    monkeypatch.setattr(client, 'load_markets', lambda: (_ for _ in ()).throw(TimeoutError('fixture')))
    with pytest.raises(TimeoutError) as error:
        catalogues.load_public_catalogue(client)
    assert error.value.metadata_observation == {
        'source': 'binance', 'market_type': 'spot', 'observed_at': observed, 'refresh_complete': False}
    assert entry['observed_at'] == observed and entry['value'] is not None
    assert catalogues.catalogue_diagnostics()['observations'][0]['expired']
    catalogues.release_catalogue_client(client)


def test_invalid_transport_adapter_cannot_leave_a_pinned_coordinator(local_capacity):
    before = local_capacity.snapshot()['bytes']
    with pytest.raises(AttributeError):
        exchange_runtime.install_transport(object(), 'invalid-offline-client')
    assert 'invalid-offline-client' not in exchange_runtime._accounts
    assert local_capacity.snapshot()['bytes'] == before
