"""Per-request clients with copied market metadata and shared account pacing."""
from __future__ import annotations

import copy
import hashlib
import json
import threading
import time
import weakref
from collections import OrderedDict

from .resilience import current_budget, remaining, setting, stage, BudgetExceeded
from backend.utils.public_catalogue import load_public_catalogue, install_bounded_http
from backend.utils.public_catalogue import (resident_size, catalogue_diagnostics,
                                           release_catalogue_client, retire_catalogues)
from backend.utils.runtime_capacity import CapacityBusy, positive_setting, reserve_bytes

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
        self.users = self.references = 0
        self.used_at = time.monotonic()
        self.lease = reserve_bytes('coordinator', 4096)


def _account(key):
    retire_runtime_caches()
    with _lock:
        item = _accounts.get(key)
        if item is None:
            cap = positive_setting('RUNTIME_COORDINATOR_ENTRIES', 128)
            for old_key, old in list(_accounts.items()):
                if len(_accounts) < cap:
                    break
                if not old.references and not old.users and not old.network.locked() and not old.pacing.locked():
                    _accounts.pop(old_key)
                    old.lease.release()
            if len(_accounts) >= cap:
                raise CapacityBusy('exchange', 'coordinator_entries')
            item = _accounts[key] = Account()
        # Pin under the same lock as lookup, so retirement cannot detach an
        # account between lookup and finalizer installation.
        item.references += 1
        _accounts.move_to_end(key)
        return item


def _release_account(account):
    with _lock:
        account.references = max(0, account.references - 1)
        account.used_at = time.monotonic()


def retire_runtime_caches(*, now=None, all_idle=False):
    now = time.monotonic() if now is None else now
    idle = positive_setting('RUNTIME_CACHE_IDLE_SECONDS', 300.0)
    with _lock:
        for key, account in list(_accounts.items()):
            if (not account.references and not account.users and not account.network.locked()
                    and not account.pacing.locked() and (all_idle or now - account.used_at >= idle)):
                _accounts.pop(key)
                account.lease.release()
        for key, quote in list(_quotes.items()):
            if all_idle or now - quote['used_at'] >= idle:
                _quotes.pop(key)
                quote['lease'].release()
        for key, refresh in list(_quote_refreshes.items()):
            if not refresh['users'] and not refresh['lock'].locked() and (all_idle or now - refresh['used_at'] >= idle):
                _quote_refreshes.pop(key)
                refresh['lease'].release()
    retire_catalogues(now=now, all_idle=all_idle)


def close_runtime_client(exchange):
    account = getattr(exchange, '_runtime_account', None)
    with _lock:
        if account and (account.users or account.network.locked() or account.pacing.locked()):
            return False
    finalizer = getattr(exchange, '_runtime_account_finalizer', None)
    if finalizer:
        finalizer()
    release_catalogue_client(exchange)
    session = getattr(exchange, 'session', None)
    if session:
        session.close()
    for name, empty in (('markets', None), ('markets_by_id', {}), ('currencies', {}),
                        ('currencies_by_id', {}), ('symbols', []), ('ids', [])):
        if hasattr(exchange, name):
            setattr(exchange, name, empty)
    return True


def _acquire(lock, label):
    if not lock.acquire(timeout=remaining(label)):
        raise BudgetExceeded(f'MCP budget exhausted at {label}')


def install_transport(exchange, key):
    """Enforce remaining deadlines at every CCXT HTTP call (including paging)."""
    if getattr(exchange, '_runtime_transport_key', None) == key:
        return
    prior = getattr(exchange, '_runtime_account_finalizer', None)
    install_bounded_http(exchange)
    original_fetch = getattr(exchange, '_runtime_raw_fetch', exchange.fetch)
    account = _account(key)
    if prior:
        prior()
    exchange._runtime_raw_fetch = original_fetch
    exchange._runtime_transport_key = key
    exchange._runtime_account = account
    try:
        finalizer = weakref.finalize(exchange, _release_account, account)
    except TypeError:
        # Explicit close still releases an offline adapter without weakrefs.
        released = False
        def finalizer():
            nonlocal released
            if not released:
                released = True
                _release_account(account)
    exchange._runtime_account_finalizer = finalizer
    exchange.maxRetriesOnFailure = 0

    def throttle(cost=None):
        with stage('account_rate_limit'):
            with _lock:
                account.users += 1
                account.used_at = time.monotonic()
            acquired = False
            try:
                _acquire(account.pacing, 'account_rate_limit')
                acquired = True
                wait = max(0, account.next_at - time.monotonic())
                if wait >= remaining('account_rate_limit'):
                    raise BudgetExceeded('MCP budget exhausted at account_rate_limit')
                if wait:
                    time.sleep(wait)
                remaining('account_rate_limit')
                account.next_at = time.monotonic() + float(exchange.rateLimit) * float(cost or 1) / 1000
            finally:
                if acquired:
                    account.pacing.release()
                with _lock:
                    account.users -= 1
                    account.used_at = time.monotonic()

    def fetch(*args, **kwargs):
        with stage('exchange_http'):
            with _lock:
                account.users += 1
                account.used_at = time.monotonic()
            acquired = False
            old_timeout = exchange.timeout
            try:
                _acquire(account.network, 'account_network')
                acquired = True
                seconds = min(remaining('exchange_http'), old_timeout / 1000)
                if current_budget.get():
                    seconds = min(seconds, setting('DEPENDENCY_TIMEOUT_SECONDS', 8))
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
                if acquired:
                    account.network.release()
                with _lock:
                    account.users -= 1
                    account.used_at = time.monotonic()

    exchange.throttle = throttle
    exchange.fetch = fetch


def load_catalogue(exchange, key=None, *, market_type=None):
    with stage('market_metadata'):
        observed = load_public_catalogue(exchange, key, market_type=market_type,
                                         timeout=remaining('market_metadata'))
        remaining('market_metadata_response')
        return observed


_quotes = OrderedDict()
_quote_refreshes = OrderedDict()


def observe(key, source, loader, ttl):
    """Public observations only. Never used for private write admission."""
    if not isinstance(ttl, (int, float)) or not 0 < ttl < float('inf'):
        raise ValueError('Quote TTL must be a finite positive number')
    identity = (key, source)
    retire_runtime_caches()
    with _lock:
        old = _quotes.get(identity)
        if old and time.monotonic() < old['expires_at']:
            old['used_at'] = time.monotonic()
            return copy.deepcopy(old['value']), old['observed_at'], True
        refresh = _quote_refreshes.get(identity)
        if refresh is None:
            cap = positive_setting('RUNTIME_QUOTE_ENTRIES', 256)
            for old_key, old in list(_quote_refreshes.items()):
                if len(_quote_refreshes) < cap:
                    break
                if not old['users'] and not old['lock'].locked():
                    _quote_refreshes.pop(old_key)
                    old['lease'].release()
            if len(_quote_refreshes) >= cap:
                raise CapacityBusy('quotes', 'quote_entries')
            refresh = _quote_refreshes[identity] = dict(lock=threading.Lock(), users=0,
                                                       used_at=time.monotonic(), lease=reserve_bytes('coordinator', 4096))
        refresh['users'] += 1
    acquired = False
    try:
        if not refresh['lock'].acquire(timeout=remaining('quote_refresh')):
            raise CapacityBusy('quotes', 'refresh_wait')
        acquired = True
        with _lock:
            old = _quotes.get(identity)
            if old and time.monotonic() < old['expires_at']:
                old['used_at'] = time.monotonic()
                return copy.deepcopy(old['value']), old['observed_at'], True
        cap = positive_setting('RUNTIME_QUOTE_BYTES', 8 * 1024 * 1024)
        peak = positive_setting('RUNTIME_QUOTE_REFRESH_BYTES', cap * 2)
        with stage(source), reserve_bytes('parse', peak):
            value = loader()
            remaining(source + '_response')
            size = resident_size(value, cap)
            if size * 2 > cap or size * 2 > peak:
                raise CapacityBusy('quotes', 'quote_bytes')
            lease = reserve_bytes('quotes', size * 2)
            try:
                cached = copy.deepcopy(value)
            except BaseException:
                lease.release()
                raise
        observed_at = time.time()
        with _lock:
            old = _quotes.pop(identity, None)
            if old:
                old['lease'].release()
            while _quotes and sum(item['lease'].amount for item in _quotes.values()) + lease.amount > cap:
                _, item = _quotes.popitem(last=False)
                item['lease'].release()
            _quotes[identity] = dict(value=cached, observed_at=observed_at,
                                     expires_at=time.monotonic() + ttl, used_at=time.monotonic(), lease=lease)
            while len(_quotes) > positive_setting('RUNTIME_QUOTE_ENTRIES', 256):
                _, item = _quotes.popitem(last=False)
                item['lease'].release()
        return value, observed_at, False
    finally:
        if acquired:
            refresh['lock'].release()
        with _lock:
            refresh['users'] -= 1
            refresh['used_at'] = time.monotonic()


def diagnostics():
    with _lock:
        return {'catalogue': catalogue_diagnostics(), 'coordinators': len(_accounts),
                'coordinator_users': sum(item.users for item in _accounts.values()),
                'quotes': len(_quotes), 'quote_bytes': sum(item['lease'].amount for item in _quotes.values())}
