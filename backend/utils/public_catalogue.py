"""Public metadata caches with independent consumers and allocation telemetry."""
from __future__ import annotations

import copy
import hashlib
import json
import sys
import threading
import time
import weakref
from collections import OrderedDict
from contextlib import contextmanager
from itertools import chain

from backend.utils.runtime_capacity import (CapacityBusy, capacity_scope,
                                           positive_setting, reserve_bytes)

_lock = threading.RLock()
_catalogues = OrderedDict()
_consumer_leases = weakref.WeakKeyDictionary()
_client_slots = weakref.WeakKeyDictionary()
_refresh_peak = 0
_public_options = ('defaultType', 'defaultSubType', 'fetchMarkets', 'fetchCurrencies',
                   'adjustForTimeDifference', 'recvWindow', 'commonCurrencies')
_native_fields = frozenset({'orderTypes', 'ocoAllowed', 'filters', 'permissions',
                            'allowedSelfTradePreventionModes', 'defaultSelfTradePreventionMode',
                            'instType', 'instId', 'instFamily', 'ctType', 'ruleType', 'groupId', 'uly', 'pair'})


def resident_size(value, limit=None):
    """Measure shared and cyclic objects once, without a size admission gate."""
    total, seen, stack = 0, set(), [iter((value,))]
    while stack:
        try:
            item = next(stack[-1])
        except StopIteration:
            stack.pop()
            continue
        identity = id(item)
        if identity in seen:
            continue
        seen.add(identity)
        total += sys.getsizeof(item)
        if isinstance(item, dict):
            stack.append(chain.from_iterable(item.items()))
        elif isinstance(item, (list, tuple, set, frozenset)):
            stack.append(iter(item))
    return total


def public_key(exchange, *, market_type=None, legacy_key=None):
    venue = getattr(exchange, 'id', None)
    if venue is None:
        if legacy_key is None:
            raise ValueError('Public catalogue requires a venue identity')
        return str(legacy_key)
    options = getattr(exchange, 'options', {}) or {}
    selected = {name: options[name] for name in _public_options if name in options}
    # Private endpoint URLs contain no account data, but including the whole
    # endpoint map keeps a configured venue/proxy namespace distinct.
    shape = [venue, market_type or options.get('defaultType'), getattr(exchange, 'urls', {}),
             selected, getattr(exchange, 'precisionMode', None), 1]
    return hashlib.sha256(json.dumps(shape, sort_keys=True, default=str).encode()).hexdigest()


def install_bounded_http(exchange):
    session = getattr(exchange, 'session', None)
    if session is None or getattr(session, '_runtime_body_bounded', False):
        return
    original = session.request

    def request(*args, **kwargs):
        lease = reserve_bytes('parse', 0)
        response = None
        try:
            kwargs['stream'] = True
            response = original(*args, **kwargs)
            body = bytearray()
            for chunk in response.iter_content(chunk_size=65536):
                body.extend(chunk)
            response._content = bytes(body)
            response._content_consumed = True
            lease.release()
            lease = reserve_bytes('parse', len(body) * 4)
            # Keep parse space until CCXT releases its Response, not just until
            # the socket completes. The enclosing refresh has extra peak space.
            weakref.finalize(response, lease.release)
            response.close()
            return response
        except BaseException:
            lease.release()
            if response is not None:
                response.close()
            raise

    session.request = request
    session._runtime_body_bounded = True
    # These transport debug caches otherwise retain entire responses in every
    # idle client. Actual financial receipts are persisted by the execution layer.
    exchange.enableLastHttpResponse = False
    exchange.enableLastJsonResponse = False


def _compact(mapping, *, limit=None):
    result = {}
    used = sys.getsizeof(result)
    for key, row in (mapping or {}).items():
        if not isinstance(row, dict):
            used += resident_size((key, row), limit) + 72
            result[key] = copy.deepcopy(row)
            continue
        item = {name: value for name, value in row.items() if name != 'info'}
        native = row.get('info') or {}
        item['info'] = {name: value for name, value in native.items()
                        if name in _native_fields} if isinstance(native, dict) else {}
        used += resident_size((key, item), limit) + 72
        result[key] = copy.deepcopy(item)
    return result


def _public_loader(exchange, market_type):
    import ccxt
    if not isinstance(exchange, ccxt.Exchange):
        return exchange  # An injected offline adapter supplies its own public loader.
    options = {key: copy.deepcopy(value) for key, value in (exchange.options or {}).items()
               if key in _public_options}
    if market_type:
        options['defaultType'] = market_type
        if exchange.id.startswith('binance'):
            options['fetchMarkets'] = {'types': ['spot' if market_type == 'spot' else 'linear']}
        elif exchange.id == 'okx':
            options['fetchMarkets'] = {'types': [market_type]}
    config = {'enableRateLimit': True, 'timeout': exchange.timeout, 'options': options}
    for key in ('urls', 'proxies', 'httpProxy', 'httpsProxy', 'socksProxy', 'proxyUrl'):
        value = getattr(exchange, key, None)
        if value is not None:
            config[key] = copy.deepcopy(value)
    loader = type(exchange)(config)
    install_bounded_http(loader)
    return loader


@contextmanager
def _refresh_loader(exchange, market_type):
    """Drop the raw SDK maps before releasing their refresh commitment."""
    import ccxt

    loader = _public_loader(exchange, market_type)
    try:
        yield loader
    finally:
        if loader is not exchange:
            loader.session.close()
            if isinstance(loader, ccxt.Exchange):
                for name in ('markets', 'markets_by_id', 'currencies', 'currencies_by_id',
                             'baseCurrencies', 'quoteCurrencies', 'symbols', 'codes'):
                    setattr(loader, name, None)


def _drop(key):
    item = _catalogues.pop(key)
    if item.get('lease'):
        item['lease'].release()


def retire_catalogues(*, now=None, all_idle=False):
    now = time.monotonic() if now is None else now
    idle = positive_setting('RUNTIME_CACHE_IDLE_SECONDS', 300.0)
    with _lock:
        for key, item in list(_catalogues.items()):
            if not item['users'] and not item['lock'].locked() and (all_idle or now - item['used_at'] >= idle):
                _drop(key)


def _reserve_catalogue(amount, *, keep, admitted=None):
    if admitted is not None and amount <= admitted.amount:
        return admitted.transfer('catalogue', amount)
    return reserve_bytes('catalogue', amount)


def _release_consumer(lease):
    lease.release()


def release_catalogue_client(exchange):
    with _lock:
        try:
            old = _consumer_leases.pop(exchange, None)
        except TypeError:
            old = None
        try:
            slot = _client_slots.pop(exchange, None)
        except TypeError:
            slot = None
    if old:
        old[1]()
    if slot:
        slot[1]()


def _pin_client_slot(exchange):
    with _lock:
        if exchange in _client_slots:
            return
        lease = reserve_bytes('client', 262144)
        try:
            finalizer = weakref.finalize(exchange, lease.release)
            _client_slots[exchange] = (lease, finalizer)
        except BaseException:
            lease.release()
            raise


def load_public_catalogue(exchange, legacy_key=None, *, market_type=None, timeout=30):
    started = time.monotonic()
    _pin_client_slot(exchange)
    with capacity_scope('metadata', timeout=timeout):
        return _load_public_catalogue(exchange, legacy_key, market_type=market_type,
                                      timeout=max(0, timeout - (time.monotonic() - started)))


def _load_public_catalogue(exchange, legacy_key=None, *, market_type=None, timeout=30):
    import ccxt

    global _refresh_peak
    key = public_key(exchange, market_type=market_type, legacy_key=legacy_key)
    install_bounded_http(exchange)
    retire_catalogues()
    with _lock:
        if key not in _catalogues:
            _catalogues[key] = {'lock': threading.Lock(), 'users': 0, 'value': None, 'bytes': 0,
                                'lease': None, 'observed_at': None, 'expires_at': 0,
                                'retry_at': 0, 'used_at': time.monotonic(),
                                'source': getattr(exchange, 'id', 'offline'),
                                'market_type': market_type or (getattr(exchange, 'options', {}) or {}).get('defaultType')}
        entry = _catalogues[key]
        entry['users'] += 1
        entry['used_at'] = time.monotonic()
        _catalogues.move_to_end(key)
    acquired = False
    try:
        if not entry['lock'].acquire(timeout=max(0, timeout)):
            raise CapacityBusy('metadata', 'refresh_wait')
        acquired = True
        if entry['value'] is None or time.monotonic() >= entry['expires_at']:
            if time.monotonic() < entry['retry_at']:
                raise ValueError('Public market metadata temporarily unavailable; refresh backoff')
            response_bound = 8 * 1024 * 1024
            max_bytes = 64 * 1024 * 1024
            peak = max(max_bytes * 2, response_bound * 8)
            ttl = positive_setting('RUNTIME_CATALOGUE_TTL_SECONDS', 300.0)
            retry_seconds = positive_setting('RUNTIME_CATALOGUE_RETRY_SECONDS', 2.0)
            loader = retained = None
            try:
                with (capacity_scope('metadata', timeout=timeout),
                      reserve_bytes('parse', peak) as refresh_space,
                      _refresh_loader(exchange, market_type) as loader):
                    _refresh_peak = max(_refresh_peak, peak)
                    fetch_options = (getattr(loader, 'options', {}) or {}).get('fetchMarkets')
                    if (isinstance(loader, ccxt.Exchange) and loader.id == 'binance'
                            and isinstance(fetch_options, dict) and fetch_options.get('types') == ['spot']):
                        # Binance's permissionSets can dwarf the entire useful
                        # catalogue. CCXT and our retained snapshot do not use
                        # them. This leaves the full symbol set and filters intact.
                        loader.load_markets(params={'showPermissionSets': False})
                    else:
                        loader.load_markets()
                    # The refresh reservation covers both the parsed loader
                    # and its compact replacement before either is retained.
                    raw_size = resident_size((loader.markets, loader.currencies), peak // 2)
                    markets = _compact(loader.markets, limit=max_bytes)
                    snapshot = (markets, _compact(loader.currencies, limit=max_bytes - resident_size(markets)))
                    size = resident_size(snapshot, max_bytes)
                    retained = _reserve_catalogue(size, keep=key, admitted=refresh_space)
                    old_lease = entry['lease']
                    entry.update(value=snapshot, bytes=size, lease=retained, observed_at=time.time(),
                                 expires_at=time.monotonic() + ttl,
                                 time_difference=(getattr(loader, 'options', {}) or {}).get('timeDifference'),
                                 retry_at=0)
                    if old_lease:
                        old_lease.release()
                    retained = None  # The entry now owns this commitment.
            except BaseException as exc:
                if retained is not None:
                    retained.release()
                entry['retry_at'] = time.monotonic() + retry_seconds
                exc.metadata_observation = {'source': entry['source'], 'market_type': entry['market_type'],
                                            'observed_at': entry['observed_at'], 'refresh_complete': False}
                raise
        # A separate commitment covers CCXT set_markets' mutable derived maps.
        lease = reserve_bytes('client', entry['bytes'] * 3)
        try:
            markets, currencies = entry['value']
            exchange.set_markets(copy.deepcopy(markets), copy.deepcopy(currencies))
            if entry.get('time_difference') is not None:
                exchange.options['timeDifference'] = entry['time_difference']
            # Replacing metadata must retain this client's lifetime slot.
            with _lock:
                old = _consumer_leases.pop(exchange, None)
            if old:
                old[1]()
            finalizer = weakref.finalize(exchange, _release_consumer, lease)
            with _lock:
                _consumer_leases[exchange] = (lease, finalizer)
        except BaseException:
            lease.release()
            raise
        return entry['observed_at']
    finally:
        if acquired:
            entry['lock'].release()
        with _lock:
            entry['users'] -= 1
            entry['used_at'] = time.monotonic()


def catalogue_diagnostics():
    with _lock:
        return {'entries': len(_catalogues), 'bytes': sum(item['bytes'] for item in _catalogues.values()),
                'users': sum(item['users'] for item in _catalogues.values()),
                'clients': len(_client_slots),
                'client_bytes': sum(v[0].amount for v in _consumer_leases.values()) + sum(v[0].amount for v in _client_slots.values()),
                'refresh_peak_reserved_bytes': _refresh_peak, 'scope': 'process',
                'observations': [{'source': row['source'], 'market_type': row['market_type'],
                                  'observed_at': row['observed_at'],
                                  'expired': time.monotonic() >= row['expires_at'], 'retained_bytes': row['bytes']}
                                 for row in _catalogues.values()]}
