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


def _binance_symbol(symbol='MUBUSDT', *, status='TRADING', contract=False):
    market = {
        'symbol': symbol, 'status': status, 'baseAsset': symbol[:-4],
        'quoteAsset': 'USDT', 'baseAssetPrecision': 8, 'quotePrecision': 8,
        'isSpotTradingAllowed': not contract, 'isMarginTradingAllowed': False,
        'orderTypes': ['LIMIT', 'MARKET', 'STOP_LOSS_LIMIT'], 'ocoAllowed': True,
        'permissions': ['SPOT'],
        'filters': [
            {'filterType': 'PRICE_FILTER', 'minPrice': '0.01',
             'maxPrice': '100000', 'tickSize': '0.01'},
            {'filterType': 'LOT_SIZE', 'minQty': '0.001',
             'maxQty': '100000', 'stepSize': '0.001'},
            {'filterType': 'MIN_NOTIONAL', 'minNotional': '5'},
        ],
    }
    if contract:
        market.update(contractType='PERPETUAL', marginAsset='USDT',
                      deliveryDate=4133404800000, onboardDate=1700000000000,
                      quantityPrecision=3, pricePrecision=2)
    return market


@pytest.mark.parametrize('legacy_options', [False, True])
def test_binance_spot_full_catalogue_omits_unused_permissions_only(
        local_capacity, monkeypatch, legacy_options):
    import ccxt
    from urllib.parse import parse_qs, urlsplit

    requests = []
    source = [_binance_symbol(), _binance_symbol('ETHUSDT', status='HALT')]

    def fetch(self, url, method='GET', headers=None, body=None):
        parsed = urlsplit(url)
        requests.append((method, parsed.path, parse_qs(parsed.query)))
        assert parsed.path == '/api/v3/exchangeInfo'
        return {'symbols': copy.deepcopy(source)}

    monkeypatch.setattr(ccxt.binance, 'fetch', fetch)
    client = ccxt.binance({'options': {
        'defaultType': 'spot', 'fetchCurrencies': False,
        'fetchMarkets': ['spot'] if legacy_options else {'types': ['spot']},
        'adjustForTimeDifference': False,
    }})
    try:
        # Omitting market_type preserves CCXT's supported legacy option shape.
        catalogues.load_public_catalogue(client)
        assert len(requests) == 1
        method, path, params = requests[0]
        assert method == 'GET' and path == '/api/v3/exchangeInfo'
        if not legacy_options:
            assert params == {'showPermissionSets': ['false']}
        else:
            assert set(params) <= {'showPermissionSets'}
        assert set(client.markets) == {'MUB/USDT', 'ETH/USDT'}
        assert client.market('ETH/USDT')['active'] is False
        market = client.market('MUB/USDT')
        assert market['active'] is True and market['spot'] is True
        assert client.amount_to_precision('MUB/USDT', 1.23456) == '1.234'
        assert client.price_to_precision('MUB/USDT', 12.3456) == '12.35'
        assert market['limits']['cost']['min'] == 5
        assert market['info']['orderTypes'] == source[0]['orderTypes']
        assert market['info']['ocoAllowed'] is True
        assert market['info']['filters'] == source[0]['filters']
    finally:
        catalogues.release_catalogue_client(client)
        client.session.close()


def test_binance_futures_catalogue_does_not_send_spot_parameter(local_capacity, monkeypatch):
    import ccxt
    from urllib.parse import parse_qs, urlsplit

    requests = []

    def fetch(self, url, method='GET', headers=None, body=None):
        parsed = urlsplit(url)
        requests.append((parsed.path, parse_qs(parsed.query)))
        assert parsed.path == '/fapi/v1/exchangeInfo'
        return {'symbols': [_binance_symbol(contract=True)]}

    monkeypatch.setattr(ccxt.binance, 'fetch', fetch)
    client = ccxt.binance({'options': {
        'defaultType': 'swap', 'fetchCurrencies': False,
        'adjustForTimeDifference': False,
    }})
    try:
        catalogues.load_public_catalogue(client, market_type='swap')
        assert requests == [('/fapi/v1/exchangeInfo', {})]
        market = client.market('MUB/USDT:USDT')
        assert market['swap'] is True and market['linear'] is True
        assert market['contractSize'] == 1
        assert client.amount_to_precision('MUB/USDT:USDT', 1.23456) == '1.234'
    finally:
        catalogues.release_catalogue_client(client)
        client.session.close()


def test_gzip_decoded_body_still_obeys_default_http_cap(local_capacity, monkeypatch):
    import gzip
    import io
    import requests
    import urllib3

    monkeypatch.delenv('RUNTIME_HTTP_RESPONSE_BYTES', raising=False)
    cap = 8 * 1024 * 1024
    compressed = gzip.compress(b'x' * (cap + 1))
    assert len(compressed) < cap
    response = requests.Response()
    response.status_code = 200
    response.headers.update({'Content-Encoding': 'gzip', 'Content-Length': str(len(compressed))})
    response.raw = urllib3.response.HTTPResponse(
        body=io.BytesIO(compressed), headers=response.headers, preload_content=False)

    class Session:
        def request(self, *args, **kwargs):
            assert kwargs['stream'] is True
            return response

    client = OfflineExchange()
    client.session = Session()
    catalogues.install_bounded_http(client)
    with pytest.raises(capacity.CapacityBusy, match='exchange: response_bytes'):
        client.session.request('GET', 'https://fixture.invalid/api/v3/exchangeInfo')
    assert response.raw.closed
    assert local_capacity.snapshot()['bytes'] == 0


def test_production_sized_catalogue_cold_and_warm_consumer_overlap(local_capacity, monkeypatch):
    import ccxt

    # Sizes measured on production with showPermissionSets=false. Virtual sizing
    # exercises actual admission and lease transfer without a 35 MiB test fixture.
    raw_bytes, compact_bytes = 34_819_893, 32_641_483
    for name in ('RUNTIME_CAPACITY_BYTES', 'RUNTIME_CATALOGUE_BYTES',
                 'RUNTIME_CATALOGUE_REFRESH_BYTES', 'RUNTIME_HTTP_RESPONSE_BYTES'):
        monkeypatch.delenv(name, raising=False)
    manager = capacity.CapacityManager.from_environment()
    monkeypatch.setattr(capacity, '_manager', manager)
    assert manager.byte_limit == 512 * 1024 * 1024
    original_size = catalogues.resident_size
    refresh_commitments, discarded_commitments = [], []

    class TransientPayload:
        pass

    class RawMarkets(dict):
        pass

    class Loader(ccxt.binance):
        def __init__(self):
            super().__init__({'options': {'fetchCurrencies': False}})
            self.counter = []

        def load_markets(self, reload=False, params=None):
            with capacity.reserve_bytes('parse', 32 * 1024 * 1024):
                refresh_commitments.append(manager.snapshot()['bytes'])
                OfflineExchange.load_markets(self)
            self.markets = RawMarkets(self.markets)
            payload = TransientPayload()
            weakref.finalize(payload, lambda: discarded_commitments.append(
                manager.snapshot()['byte_classes']['parse']))
            self.markets['ETH/USDT']['info']['unused_directory_payload'] = payload

    def measured_size(value, limit=None):
        if isinstance(value, tuple) and len(value) == 2 and isinstance(value[0], dict):
            measured = raw_bytes if isinstance(value[0], RawMarkets) else compact_bytes
        elif isinstance(value, dict) and 'ETH/USDT' in value:
            measured = compact_bytes
        else:
            return original_size(value, limit)
        if limit is not None and measured > limit:
            raise capacity.CapacityBusy('metadata', 'catalogue_bytes')
        return measured

    monkeypatch.setattr(catalogues, 'resident_size', measured_size)
    monkeypatch.setattr(catalogues, '_public_loader', lambda *_: Loader())
    first, second = OfflineExchange(), OfflineExchange(credential='second-account')
    try:
        observed = catalogues.load_public_catalogue(first)
        assert catalogues.load_public_catalogue(second) == observed
        entry = catalogues._catalogues[catalogues.public_key(first)]
        assert entry['bytes'] == compact_bytes
        entry['expires_at'] = 0
        catalogues.load_public_catalogue(first)
        assert len(refresh_commitments) == 2
        assert refresh_commitments[-1] > 256 * 1024 * 1024
        assert len(discarded_commitments) == 2
        # The raw loader must disappear while its transient lease remains live.
        assert all(amount >= 128 * 1024 * 1024 - compact_bytes
                   for amount in discarded_commitments)
        diagnostics = manager.snapshot()
        assert diagnostics['byte_classes']['parse'] == 0
        assert diagnostics['byte_classes']['catalogue'] == compact_bytes
        assert diagnostics['peaks']['bytes'] <= manager.byte_limit
        assert catalogues.catalogue_diagnostics()['refresh_peak_reserved_bytes'] == 128 * 1024 * 1024
    finally:
        catalogues.release_catalogue_client(first)
        catalogues.release_catalogue_client(second)
        catalogues.retire_catalogues(all_idle=True)
    assert manager.snapshot()['bytes'] == 0


def test_observation_mode_loads_full_catalogues_above_all_configured_caps(local_capacity, monkeypatch):
    monkeypatch.setenv('RUNTIME_CAPACITY_ENABLED', 'false')
    for name in ('RUNTIME_CAPACITY_BYTES', 'RUNTIME_CATALOGUE_BYTES',
                 'RUNTIME_CATALOGUE_REFRESH_BYTES', 'RUNTIME_CATALOGUE_ENTRIES',
                 'RUNTIME_CLIENT_ENTRIES'):
        monkeypatch.setenv(name, '1')
    manager = capacity.CapacityManager.from_environment()
    monkeypatch.setattr(capacity, '_manager', manager)
    clients = [OfflineExchange(), OfflineExchange(market_type='swap')]
    try:
        for client in clients:
            catalogues.load_public_catalogue(client)
            assert client.markets['ETH/USDT']['info']['ocoAllowed'] is True
        diagnostics = catalogues.catalogue_diagnostics()
        assert diagnostics['entries'] == diagnostics['clients'] == 2
        assert diagnostics['bytes'] > 1 and manager.snapshot()['bytes'] > 1
    finally:
        for client in clients:
            catalogues.release_catalogue_client(client)
        catalogues.retire_catalogues(all_idle=True)
    assert manager.snapshot()['bytes'] == 0


def test_observation_mode_reads_entire_response_above_configured_cap(local_capacity, monkeypatch):
    import requests
    monkeypatch.setenv('RUNTIME_CAPACITY_ENABLED', 'false')
    monkeypatch.setenv('RUNTIME_HTTP_RESPONSE_BYTES', '8')
    manager = capacity.CapacityManager(limits_enabled=False, byte_limit=1)
    monkeypatch.setattr(capacity, '_manager', manager)
    response = requests.Response()
    response.status_code = 200
    response.headers['Content-Length'] = '17'
    response.iter_content = lambda chunk_size: iter([b'12345678', b'901234567'])
    response.close = lambda: None
    class Session:
        def request(self, *args, **kwargs):
            assert kwargs['stream'] is True
            return response
    client = OfflineExchange()
    client.session = Session()
    catalogues.install_bounded_http(client)
    observed = client.session.request('GET', 'https://fixture.invalid/exchangeInfo')
    assert observed.content == b'12345678901234567'
    assert manager.snapshot()['byte_classes']['parse'] == 17 * 4
    del observed, response
    gc.collect()
    assert manager.snapshot()['byte_classes']['parse'] == 0
