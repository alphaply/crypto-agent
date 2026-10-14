"""Per-request clients with copied market metadata and shared account pacing."""
from __future__ import annotations

import copy
import hashlib
import json
import threading
import time
from collections import OrderedDict

from .resilience import current_budget, remaining, setting, stage, BudgetExceeded

_lock = threading.Lock()
_accounts = OrderedDict()
_catalogues = OrderedDict()


def account_key(exchange, market_type, credentials):
    # The digest is internal and never logged. Rotating any credential changes
    # both cache and coordinator identity, including same profile-ID rotation.
    value = json.dumps([exchange, market_type, credentials], sort_keys=True, default=str)
    return hashlib.sha256(value.encode()).hexdigest()


class Account:
    def __init__(self):
        self.network = threading.Lock()
        self.pacing = threading.Lock()
        self.next_at = 0.0


def _account(key):
    with _lock:
        item = _accounts.get(key)
        if item is None:
            item = _accounts[key] = Account()
        _accounts.move_to_end(key)
        # Do not evict coordinators with live locks/reservations. Configuration
        # cardinality is bounded by the application's saved account profiles.
        return item


def _acquire(lock, label):
    if not lock.acquire(timeout=remaining(label)):
        raise BudgetExceeded(f'MCP budget exhausted at {label}')


def install_transport(exchange, key):
    """Enforce remaining deadlines at every CCXT HTTP call (including paging)."""
    account = _account(key)
    original_fetch = exchange.fetch
    exchange.maxRetriesOnFailure = 0

    def throttle(cost=None):
        with stage('account_rate_limit'):
            _acquire(account.pacing, 'account_rate_limit')
            try:
                wait = max(0, account.next_at - time.monotonic())
                if wait >= remaining('account_rate_limit'):
                    raise BudgetExceeded('MCP budget exhausted at account_rate_limit')
                if wait:
                    time.sleep(wait)
                remaining('account_rate_limit')
                account.next_at = time.monotonic() + float(exchange.rateLimit) * float(cost or 1) / 1000
            finally:
                account.pacing.release()

    def fetch(*args, **kwargs):
        with stage('exchange_http'):
            _acquire(account.network, 'account_network')
            old_timeout = exchange.timeout
            try:
                seconds = min(remaining('exchange_http'), setting('DEPENDENCY_TIMEOUT_SECONDS', 8), old_timeout / 1000)
                exchange.timeout = max(1, int(seconds * 1000))
                budget = current_budget.get()
                method = args[1] if len(args) > 1 else kwargs.get('method', 'GET')
                if budget and budget.kind == 'execute' and str(method).upper() in {'POST', 'PUT', 'DELETE', 'PATCH'}:
                    budget.write_started = True
                result = original_fetch(*args, **kwargs)
                if budget and budget.kind != 'execute':
                    remaining('exchange_http_response')
                return result
            finally:
                exchange.timeout = old_timeout
                account.network.release()

    exchange.throttle = throttle
    exchange.fetch = fetch


def load_catalogue(exchange, key):
    with _lock:
        entry = _catalogues.get(key)
        if entry is None:
            entry = _catalogues[key] = {'lock': threading.Lock(), 'value': None, 'observed_at': None,
                                        'expires_at': 0, 'retry_at': 0, 'users': 0}
        entry['users'] += 1
        _catalogues.move_to_end(key)
        # Never evict a refresh in progress. Stored metadata is only ever
        # copied, so a caller modifying its CCXT market cannot alter the cache.
        for old_key in list(_catalogues)[:-64]:
            if not _catalogues[old_key]['users']:
                _catalogues.pop(old_key)
    acquired = False
    try:
        with stage('market_metadata'):
            _acquire(entry['lock'], 'market_metadata')
            acquired = True
            remaining('market_metadata')
            if entry['value'] is None or time.monotonic() >= entry['expires_at']:
                if time.monotonic() < entry['retry_at']:
                    raise ValueError('MCP market metadata temporarily unavailable; refresh backoff')
                try:
                    exchange.load_markets()
                    entry['value'] = copy.deepcopy((exchange.markets, exchange.currencies))
                    entry['time_difference'] = (getattr(exchange, 'options', {}) or {}).get('timeDifference')
                    entry['observed_at'] = time.time()
                    entry['expires_at'] = time.monotonic() + setting('MARKET_CACHE_SECONDS', 300, maximum=3600)
                except Exception:
                    entry['retry_at'] = time.monotonic() + setting('MARKET_CACHE_RETRY_SECONDS', 2)
                    raise
            markets, currencies = copy.deepcopy(entry['value'])
            exchange.set_markets(markets, currencies)
            if entry.get('time_difference') is not None:
                exchange.options['timeDifference'] = entry['time_difference']
            return entry['observed_at']
    finally:
        if acquired:
            entry['lock'].release()
        with _lock:
            entry['users'] -= 1


_quotes = OrderedDict()


def observe(key, source, loader, ttl):
    """Public observations only. Never used for private write admission."""
    with _lock:
        old = _quotes.get((key, source))
        if old and time.monotonic() < old['expires_at']:
            return copy.deepcopy(old['value']), old['observed_at'], True
    with stage(source):
        value = loader()
        remaining(source + '_response')
    observed_at = time.time()
    with _lock:
        _quotes[(key, source)] = dict(value=copy.deepcopy(value), observed_at=observed_at,
                                     expires_at=time.monotonic() + ttl)
        while len(_quotes) > 256:
            _quotes.popitem(last=False)
    return value, observed_at, False
