"""Isolated fake-dependency driver. Mounted for verification, never installed as an API."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
import gc
import faulthandler
from collections import deque
import hashlib
import json
import os
from pathlib import Path
import socket
import signal
import sys
import threading
import time
import traceback

faulthandler.enable(all_threads=True)


def dump_probe_threads(_signum=None, _frame=None):
    # A Python callback holds the GIL while walking live thread frames.
    # CPython #116008 makes native asynchronous diagnostic dumps unsafe.
    faulthandler.dump_traceback(all_threads=True)


def start_phase_watchdog(delay):
    watchdog = threading.Timer(delay, dump_probe_threads)
    watchdog.name = 'capacity-phase-watchdog'
    watchdog.daemon = True
    watchdog.start()
    return watchdog


def stop_phase_watchdog(watchdog, *, timeout=1.0):
    watchdog.cancel()
    watchdog.join(timeout=timeout)
    return not watchdog.is_alive()


if hasattr(signal, 'SIGUSR1'):
    signal.signal(signal.SIGUSR1, dump_probe_threads)

sys.path.insert(0, '/app')
from backend.app.main import app
from backend.utils.capacity_profile import load_profile
from backend.utils.public_catalogue import (load_public_catalogue, release_catalogue_client,
    retire_catalogues, catalogue_diagnostics)
from backend.utils.runtime_capacity import CapacityBusy, capacity_scope, get_capacity_manager
from backend.utils.resource_diagnostics import resource_snapshot, start_resource_collector
from fastapi import HTTPException
import uvicorn

PROFILE = load_profile('/verification/profile.json')
ROLE = os.environ['VERIFICATION_ROLE']
ROOT = Path(os.environ['DATA_DIR'])
CFG = {'config_id': 'verification', 'mode': 'STRATEGY', 'exit_mode': 'independent_exits', 'symbol': 'ETH/USDT'}
state = {'phase': None, 'running': False, 'phases': {}, 'busy': 0, 'critical': 0,
         'writes_replayed': 0, 'tail_executed': 0, 'restart_continuity': False}
state_lock = threading.Lock()
retired_keys_seen = set()
recovery_target = None
recovery_seen = set()


def seed_recovery(phase):
    """Persist an original unknown ACK; the real background helper observes it."""
    global recovery_target
    from backend.config import config
    from backend.database import get_db_conn
    from backend.utils.trade_operations import run_once
    from backend.agent import unified_trading
    config.get_config_by_id = lambda _: dict(CFG)
    original_query = unified_trading.query_unified_operation
    if not hasattr(original_query, '_verification_original'):
        def observed(config_id, symbol, operation_id, **kwargs):
            if operation_id.startswith('capacity-recovery-'):
                return {'contract_version':3,'operation_id':operation_id,'status':'completed',
                        'order_id':'regular:' + operation_id,'filled_amount':.03,'symbol':symbol}
            return original_query(config_id,symbol,operation_id,**kwargs)
        observed._verification_original = original_query
        unified_trading.query_unified_operation = observed
    operation_id='capacity-recovery-'+phase
    run_once('verification','ETH/USDT',operation_id,{'contract_version':3},
             lambda:{'contract_version':3,'operation_id':operation_id,'status':'unknown','order_id':'regular:'+operation_id})
    with get_db_conn() as conn:
        row=conn.execute('SELECT rowid FROM trade_action_runs WHERE config_id=? AND operation_id=?',('verification',operation_id)).fetchone()
    recovery_target=(row[0],operation_id)

# No configured/accidental external connection is possible in the probe.
def deny_outbound(*_args, **_kwargs):
    raise AssertionError('Verification cannot connect to an external dependency')

socket.socket.connect = deny_outbound
socket.socket.connect_ex = deny_outbound


class OfflineExchange:
    id = 'binance'
    precisionMode = 4
    session = None
    timeout = 1000

    def __init__(self, key=0, market='spot'):
        self.urls = {'api': {'public': f'https://fixture.invalid/{key}'}}
        self.options = {'defaultType': market}
        self.markets, self.currencies = {}, {}
        self.market = market

    def load_markets(self):
        time.sleep(.03)
        result = {}
        for index in range(PROFILE.catalogue_symbols):
            base = f'FIX{index}'
            symbol = f'{base}/USDT' + (':USDT' if self.market == 'swap' else '')
            result[symbol] = {'id': base + 'USDT', 'symbol': symbol, 'base': base,
                'quote': 'USDT', 'settle': 'USDT' if self.market == 'swap' else None,
                'spot': self.market == 'spot', 'swap': self.market == 'swap',
                'linear': self.market == 'swap', 'contract': self.market == 'swap',
                'active': True, 'contractSize': 1, 'precision': {'amount': .001, 'price': .01},
                'limits': {'amount': {'min': .001}, 'cost': {'min': 5}},
                'info': {'filters': [{'filterType': 'PRICE_FILTER', 'tickSize': '0.01'}],
                         'orderTypes': ['LIMIT', 'MARKET'], 'fixture_padding': 'x' * 4090 + f'{index:06d}'}}
        self.markets = result
        self.currencies = {'USDT': {'id': 'USDT', 'code': 'USDT', 'precision': 6}}
        return result

    def set_markets(self, markets, currencies):
        self.markets, self.currencies = markets, currencies


def catalogue(key=0, market='spot', refresh=False):
    from backend.utils import public_catalogue as cache
    client = OfflineExchange(key, market)
    if refresh:
        identity = cache.public_key(client, market_type=market)
        with cache._lock:
            entry = cache._catalogues.get(identity)
            if entry:
                entry['expires_at'] = 0
    try:
        load_public_catalogue(client, market_type=market, timeout=.5)
        if len(client.markets) != PROFILE.catalogue_symbols:
            raise AssertionError('Catalogue did not cover declared fixture')
    finally:
        release_catalogue_client(client)


def background_recovery_progress():
    target = recovery_target
    if target is None:
        return
    from backend.utils.background_receipts import reconcile
    from backend.database import get_db_conn
    try:
        reconcile(limit=1, after_cursor=target[0]-1)
    except CapacityBusy:
        state['busy'] += 1
    # The scheduler may already have completed this same original identity.
    # Observe durable evidence regardless of which helper made the progress.
    with get_db_conn() as conn:
        row = conn.execute('SELECT result FROM trade_action_runs WHERE rowid=? AND config_id=? AND operation_id=?',
                           (target[0], 'verification', target[1])).fetchone()
    if row is None:
        raise AssertionError('Seeded critical recovery identity disappeared')
    latest = json.loads(row[0])
    if latest.get('operation_id') != target[1]:
        raise AssertionError('Critical recovery changed the original identity')
    if latest.get('status') == 'completed':
        with state_lock:
            if target[1] not in recovery_seen:
                if len(recovery_seen) >= len(PROFILE.phase_seconds):
                    raise AssertionError('Critical recovery identity set exceeds declared phases')
                recovery_seen.add(target[1])
                state['critical'] += 1


def work(index, phase):
    role = ('agent', 'mcp_read', 'memory', 'news', 'maintenance')[index % 5]
    try:
        with capacity_scope(role, timeout=.25):
            if phase in {'cold', 'typical', 'concurrent_refresh', 'key_retirement'}:
                catalogue(index % 2 if phase != 'key_retirement' else index % PROFILE.retired_keys,
                          'swap' if index % 2 else 'spot', phase == 'concurrent_refresh')
                if phase == 'key_retirement':
                    with state_lock:
                        retired_keys_seen.add(index % PROFILE.retired_keys)
            if phase != 'idle':
                until = time.monotonic() + (.2 if phase == 'slow_dependencies' else .025)
                while time.monotonic() < until:
                    hashlib.sha256(b'fixed-fake-cpu-work' * 512).digest()
                time.sleep(.05)
            return True
    except CapacityBusy:
        state['busy'] += 1
        return False
    except ValueError as exc:
        if 'metadata temporarily unavailable; refresh backoff' not in str(exc):
            raise
        state['busy'] += 1
        return False


def financial_fixture():
    """Stop after the original second child was attempted, leaving tail untouched."""
    from backend.agent import unified_trading as unified
    from backend.config import config
    config.get_config_by_id = lambda _: dict(CFG)
    payload = {'contract_version': 3, 'actions': [
        {'action': 'cancel', 'order_id': f'regular:{index}', 'reason': 'verification'} for index in range(3)]}
    path = ROOT / 'attempted.json'
    if path.exists():
        previous = json.loads(path.read_text())
        result = unified.query_unified_operation('verification', 'ETH/USDT', 'verification-parent', read_exchange=False)

        def forbidden(*_):
            state['writes_replayed'] += 1
            raise AssertionError('Restart must not replay any financial write')

        replay = unified.execute_v3_actions(payload, config_id='verification', symbol='ETH/USDT',
            operation_id='verification-parent', config=CFG, dispatch=forbidden)
        statuses = [row['status'] for row in result['results']]
        state['tail_executed'] += int(statuses[2] != 'not_executed')
        state['restart_continuity'] = statuses == ['completed', 'unknown', 'not_executed'] and previous == [0, 1]
        return
    attempted = []

    def dispatch(_action, index, *_):
        attempted.append(index)
        path.write_text(json.dumps(attempted))
        if index == 1:
            raise SystemExit('isolated original worker stopped')
        return {'receipt_version': 1, 'status': 'completed'}

    try:
        unified.execute_v3_actions(payload, config_id='verification', symbol='ETH/USDT',
            operation_id='verification-parent', config=CFG, dispatch=dispatch)
    except SystemExit:
        pass


def drive(phase):
    begin = time.monotonic()
    samples, completed, accepted, errors = deque(maxlen=3600), 0, 0, []
    next_sample = begin
    retirement_mid_rss = None
    iteration = 0
    critical_before = state['critical']
    before = resource_snapshot().get('resources', {}).get('process', {}).get('rss_bytes')
    watchdog = start_phase_watchdog(max(0, begin + PROFILE.phase_seconds[phase] + 10 - time.monotonic()))
    try:
        if phase == 'restart':
            financial_fixture()
        seed_recovery(phase)
        with ThreadPoolExecutor(max_workers=PROFILE.concurrent_requests) as pool:
            while time.monotonic() - begin < PROFILE.phase_seconds[phase]:
                if phase == 'idle' or phase == 'restart':
                    time.sleep(.2)
                else:
                    count = 2 if phase in {'cold', 'typical', 'key_retirement'} else PROFILE.concurrent_requests
                    indices = list(range(count))
                    if phase == 'key_retirement':
                        with state_lock:
                            pending = [key for key in range(PROFILE.retired_keys) if key not in retired_keys_seen]
                        # A pair can have only one admitted refresh under the
                        # declared byte budget. Retry rejected identities first
                        # instead of advancing over them forever.
                        indices = pending[:count] if pending else [
                            (iteration * count + index) % PROFILE.retired_keys for index in range(count)]
                    futures = [pool.submit(work, index, phase) for index in indices]
                    background_recovery_progress()
                    for future in futures:
                        accepted += int(future.result(timeout=5))
                    completed += len(futures)
                    iteration += 1
                    time.sleep(.01)
                if phase == 'key_retirement':
                    retire_catalogues(all_idle=True)
                    gc.collect()
                if time.monotonic() >= next_sample:
                    observation = resource_snapshot()
                    if 'resources' in observation:
                        samples.append(observation)
                        if phase == 'key_retirement' and retirement_mid_rss is None and time.monotonic() - begin >= PROFILE.phase_seconds[phase] / 2:
                            retirement_mid_rss = observation['resources']['process'].get('rss_bytes')
                    next_sample = time.monotonic() + .2
        if phase == 'slow_dependencies':
            financial_fixture()
        gc.collect()
        if phase == 'key_retirement':
            retire_catalogues(all_idle=True)
    except Exception as exc:
        traceback.print_exc()
        errors.append(f'{type(exc).__name__}: {str(exc)[:240]}')
    finally:
        if not stop_phase_watchdog(watchdog):
            errors.append('Diagnostic watchdog cleanup exceeded 1 second')
    memory = [sample['resources']['memory'].get(key) for sample in samples
              for key in ('current_bytes', 'peak_bytes')]
    rss = [sample['resources']['process'].get('rss_bytes') for sample in samples]
    host = [sample['resources']['host'].get('memory_available_bytes') for sample in samples]
    result = {'elapsed_seconds': time.monotonic() - begin, 'sample_count': len(samples),
        'workload_completed': not errors and (phase in {'idle', 'restart'} or accepted > 0),
        'errors': errors, 'completed_requests': completed, 'accepted_requests': accepted,
        'critical_receipts_updated': state['critical'] - critical_before,
        'memory_peak_bytes': max((n for n in memory if n is not None), default=None),
        'rss_peak_bytes': max((n for n in rss if n is not None), default=None),
        'host_min_available_bytes': min((n for n in host if n is not None), default=None),
        'rss_before_bytes': before, 'rss_after_bytes': rss[-1] if rss else None,
        'retirement_mid_rss_bytes': retirement_mid_rss, 'retired_keys_seen': len(retired_keys_seen),
        'capacity': get_capacity_manager().snapshot(), 'catalogue': catalogue_diagnostics()}
    with state_lock:
        state['phases'][phase] = result
        state['running'] = False
    (ROOT / 'verification-state.json').write_text(json.dumps(state))


original_lifespan = app.router.lifespan_context


@asynccontextmanager
async def probe_lifespan(app):
    start_resource_collector(ROLE)
    async with original_lifespan(app):
        saved = ROOT / 'verification-state.json'
        if saved.exists():
            state.update(json.loads(saved.read_text()))
            state['running'] = False
            financial_fixture()
        yield


app.router.lifespan_context = probe_lifespan


@app.post('/verification/start/{phase}')
def start(phase: str):
    if phase not in PROFILE.phase_seconds:
        raise HTTPException(400)
    with state_lock:
        if state['running']:
            raise HTTPException(409)
        state['phase'], state['running'] = phase, True
    threading.Thread(target=drive, args=(phase,), daemon=True).start()
    return {'started': phase}


@app.get('/verification/result')
def result():
    # Phase completion adds an entry on another thread while FastAPI encodes
    # this response. Freeze the dictionary structure before leaving the lock.
    with state_lock:
        current = {**state, 'phases': dict(state['phases'])}
    return {**current, 'resources': resource_snapshot(), 'capacity': get_capacity_manager().snapshot(),
            'catalogue': catalogue_diagnostics()}


# Production has an SPA catch-all. Put the probe's isolated routes before it.
app.router.routes[:] = (
    [route for route in app.router.routes if getattr(route, 'path', '').startswith('/verification/')]
    + [route for route in app.router.routes if not getattr(route, 'path', '').startswith('/verification/')])
uvicorn.run(app, host='0.0.0.0', port=7860, log_level='warning')
