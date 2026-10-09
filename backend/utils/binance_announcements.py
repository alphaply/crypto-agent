"""Official signed Binance announcements stream, collected for hourly digests."""
from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from backend import database_news

_lock = threading.Lock()
_worker = None
_stop = None
_identity = None


def signed_url(secret: str, *, timestamp: int | None = None, nonce: str | None = None) -> str:
    params = {'random': nonce or uuid.uuid4().hex, 'topic': 'com_announcement_en', 'recvWindow': 30000, 'timestamp': timestamp or int(time.time() * 1000)}
    query = urlencode(sorted(params.items()))
    signature = hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()
    return f'wss://api.binance.com/sapi/wss?{query}&signature={signature}'


def parse_announcement(frame: str | bytes) -> dict | None:
    from backend.utils.news_context import _make_item, _clean_text
    data = json.loads(frame)
    if data.get('type') != 'DATA' or data.get('topic') != 'com_announcement_en':
        return None
    value = data.get('data')
    value = json.loads(value) if isinstance(value, str) else value
    if not isinstance(value, dict) or not value.get('title') or not value.get('publishDate'):
        raise ValueError('Invalid announcement')
    published = datetime.fromtimestamp(float(value['publishDate']) / 1000, timezone.utc)
    item = _make_item(source='Binance', title=value['title'], category='exchange_announcement', impact='medium', relevance=0, published_at=published, url='https://www.binance.com/en/support/announcement')
    item.update(content=_clean_text(value.get('body'))[:12000], catalog_id=value.get('catalogId'), catalog_name=value.get('catalogName'))
    return item


def _run(key: str, secret: str, stop: threading.Event, owner: str) -> None:
    from websockets.sync.client import connect
    delay = 2
    try:
        while not stop.is_set():
            if not database_news.renew_lease('binance-stream', owner):
                return
            try:
                with connect(signed_url(secret), additional_headers={'X-MBX-APIKEY': key}, ping_interval=30, ping_timeout=20, open_timeout=10, close_timeout=5, max_size=2 * 1024 * 1024) as socket:
                    database_news.update_state('binance-stream', {'status': 'connected', 'connected_at': database_news.now_iso(), 'error': None}, owner=owner)
                    connected = time.monotonic()
                    delay = 2
                    while not stop.is_set() and time.monotonic() - connected < 23 * 3600 + 50 * 60:
                        if not database_news.renew_lease('binance-stream', owner):
                            return
                        try:
                            raw = socket.recv(timeout=15)
                        except TimeoutError:
                            continue
                        try:
                            item = parse_announcement(raw)
                        except (ValueError, TypeError, OverflowError):
                            database_news.update_state('binance-stream', {'error': 'Invalid announcement payload skipped'}, owner=owner)
                            continue
                        if item:
                            database_news.save_announcement(item)
                            database_news.update_state('binance-stream', {'last_message_at': database_news.now_iso()}, owner=owner)
            except Exception:
                # Exceptions may contain the signed connection URL. Never persist it.
                database_news.update_state('binance-stream', {'status': 'reconnecting', 'error': 'Binance announcements connection failed; retrying'}, owner=owner)
                stop.wait(delay)
                delay = min(delay * 2, 60)
    finally:
        database_news.update_state('binance-stream', {'status': 'stopped'}, owner=owner, release=True)


def ensure_worker(settings, snapshot: dict) -> None:
    global _worker, _stop, _identity
    enabled = settings.enabled and any(source.enabled and source.kind == 'binance' for source in settings.sources)
    profile = next((p for p in snapshot.get('exchange_profiles', []) if p.get('profile_id') == settings.binance_exchange_profile_id and p.get('exchange') == 'binance'), {})
    key = profile.get('api_key') or (snapshot.get('global_binance_api_key') if not settings.binance_exchange_profile_id else '')
    secret = profile.get('secret') or (snapshot.get('global_binance_secret') if not settings.binance_exchange_profile_id else '')
    identity = hashlib.sha256(f'{enabled}|{key}|{secret}'.encode()).hexdigest()
    with _lock:
        if _worker and _worker.is_alive() and identity == _identity:
            return
        if _stop:
            _stop.set()
        if _worker and _worker.is_alive():
            return  # Old credentials finish their bounded recv before replacement.
        _identity = identity
        if not enabled or not key or not secret:
            database_news.update_state('binance-stream', {'status': 'disabled' if not enabled else 'configuration_required', 'error': None if not enabled else 'Select a Binance account for announcement access'})
            return
        owner = database_news.acquire_lease('binance-stream', seconds=120)
        if not owner:
            return
        _stop = threading.Event()
        _worker = threading.Thread(target=_run, args=(key, secret, _stop, owner), name='binance-announcements', daemon=True)
        _worker.start()


def stop_worker() -> None:
    with _lock:
        if _stop:
            _stop.set()


def get_items(lookback_hours: int = 24) -> dict:
    state = database_news.read_state('binance-stream')
    items = database_news.announcements((datetime.now(timezone.utc) - timedelta(hours=lookback_hours)).isoformat())
    healthy = state.get('status') == 'connected' and state.get('running')
    return {'items': items, 'status': 'ok' if healthy else 'stale' if items else state.get('status', 'unavailable'), 'stale': bool(items and not healthy), 'fetched_at': state.get('last_message_at') or state.get('connected_at'), 'error': state.get('error')}
