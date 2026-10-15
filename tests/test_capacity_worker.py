"""Exercise probe helpers without importing its server or network guard."""
import ast
import json
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import textwrap
import traceback
from types import SimpleNamespace

import pytest

from backend import database
from backend.config import config
from backend.database_schema import initialize_schema
from backend.utils import background_receipts, runtime_capacity
from backend.utils.capacity_profile import PHASES


WORKER = Path('scripts/capacity/worker.py')


def diagnostic_prefix():
    """Run the actual probe's stdlib setup without its server or SDK imports."""
    nodes = []
    for node in ast.parse(WORKER.read_text(encoding='utf-8')).body:
        if isinstance(node, ast.ImportFrom) and (node.module or '').startswith('backend.'):
            break
        nodes.append(node)
    return ast.unparse(ast.Module(body=nodes, type_ignores=[]))


def run_diagnostic_child(body):
    program = diagnostic_prefix() + '\n' + textwrap.dedent(body)
    result = subprocess.run([sys.executable, '-c', program], capture_output=True,
                            text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout), result.stderr


@pytest.mark.skipif(sys.platform != 'linux', reason='Requires Linux prctl and core limits')
def test_probe_disables_native_core_and_keeps_fatal_tracebacks():
    outcome, _ = run_diagnostic_child('''
        import ctypes, resource
        prctl = ctypes.CDLL(None, use_errno=True).prctl
        prctl.argtypes = [ctypes.c_int] + [ctypes.c_ulong] * 4
        prctl.restype = ctypes.c_int
        assert PROBE_CORE_DUMPS_DISABLED
        assert prctl(3, 0, 0, 0, 0) == 0
        assert resource.getrlimit(resource.RLIMIT_CORE) == (0, 0)
        assert faulthandler.is_enabled()
        print(json.dumps({'native_core_disabled': True, 'fatal_tracebacks': True}))
    ''')
    assert outcome == {'native_core_disabled': True, 'fatal_tracebacks': True}


def test_core_policy_failure_stops_probe_before_application_imports():
    program = """
import ctypes, sys, types
class DeniedPrctl:
    def __call__(self, *_args):
        return -1
ctypes.CDLL = lambda *_args, **_kwargs: types.SimpleNamespace(prctl=DeniedPrctl())
sys.modules['resource'] = types.SimpleNamespace(RLIMIT_CORE=4, setrlimit=lambda *_: None)
sys.platform = 'linux'
""" + diagnostic_prefix() + "\nraise AssertionError('Probe continued after core policy failure')"
    result = subprocess.run([sys.executable, '-c', program], capture_output=True,
                            text=True, timeout=15)
    assert result.returncode != 0
    assert 'Cannot disable capacity probe core dumps' in result.stderr
    assert 'Probe continued' not in result.stderr


@pytest.mark.skipif(not hasattr(signal, 'SIGUSR1'), reason='Requires a real POSIX SIGUSR1')
def test_signal_dump_runs_on_main_python_thread_with_gil():
    outcome, stderr = run_diagnostic_child('''
        import copy, ctypes
        assert signal.getsignal(signal.SIGUSR1) is dump_probe_threads
        native_dump = faulthandler.dump_traceback
        calls = []
        stopped = threading.Event()
        def observed_dump(**kwargs):
            assert ctypes.pythonapi.PyGILState_Check() == 1
            assert kwargs == {'all_threads': True}
            calls.append(threading.current_thread().name)
            native_dump(**kwargs)
        faulthandler.dump_traceback = observed_dump
        def allocate():
            while not stopped.wait(.001):
                copy.deepcopy([{'nested': list(range(32))} for _ in range(32)])
        threads = [threading.Thread(target=allocate, name=f'allocating-{i}') for i in range(4)]
        for thread in threads:
            thread.start()
        try:
            for index in range(8):
                if hasattr(signal, 'pthread_kill'):
                    signal.pthread_kill(threads[index % len(threads)].ident, signal.SIGUSR1)
                else:
                    os.kill(os.getpid(), signal.SIGUSR1)
                deadline = time.monotonic() + 2
                while len(calls) <= index and time.monotonic() < deadline:
                    time.sleep(.001)
                assert len(calls) == index + 1, calls
        finally:
            stopped.set()
            for thread in threads:
                thread.join(timeout=1)
            assert all(not thread.is_alive() for thread in threads)
        assert calls == ['MainThread'] * 8
        assert gc.isenabled()
        print(json.dumps({'signal_dumps': len(calls), 'callback_threads': sorted(set(calls)),
                          'allocation_threads': len(threads), 'directed_to_workers': hasattr(signal, 'pthread_kill')}))
    ''')
    assert outcome == {'signal_dumps': 8, 'callback_threads': ['MainThread'],
                       'allocation_threads': 4, 'directed_to_workers': hasattr(signal, 'pthread_kill')}
    assert 'dump_probe_threads' in stderr and 'allocate' in stderr
    print(json.dumps(outcome, sort_keys=True))


def test_phase_timer_dump_holds_gil_and_cancellation_is_bounded():
    outcome, stderr = run_diagnostic_child('''
        import ctypes
        native_dump = faulthandler.dump_traceback
        called = threading.Event()
        calls = []
        stopped = threading.Event()
        thread = threading.Thread(target=lambda: stopped.wait(5), name='waiting-worker')
        thread.start()
        def observed_dump(**kwargs):
            assert ctypes.pythonapi.PyGILState_Check() == 1
            calls.append(threading.current_thread().name)
            native_dump(**kwargs)
            called.set()
        faulthandler.dump_traceback = observed_dump
        try:
            watchdog = start_phase_watchdog(.01)
            assert watchdog.daemon
            assert called.wait(2)
            assert stop_phase_watchdog(watchdog)
            cancelled = start_phase_watchdog(1)
            assert stop_phase_watchdog(cancelled)
            time.sleep(.02)
            assert calls == ['capacity-phase-watchdog']
            entered, release = threading.Event(), threading.Event()
            def waiting_dump():
                entered.set()
                release.wait(2)
            blocked = threading.Timer(0, waiting_dump)
            blocked.daemon = True
            blocked.start()
            try:
                assert entered.wait(1)
                begin = time.monotonic()
                assert not stop_phase_watchdog(blocked, timeout=.02)
                assert time.monotonic() - begin < .5
            finally:
                release.set()
                blocked.join(timeout=1)
            assert not blocked.is_alive()
        finally:
            stopped.set()
            thread.join(timeout=1)
        print(json.dumps({'timer_dumps': len(calls), 'callback_threads': calls,
                          'cancelled_before_fire': not cancelled.is_alive(), 'bounded_join': True}))
    ''')
    assert outcome == {'timer_dumps': 1, 'callback_threads': ['capacity-phase-watchdog'],
                       'cancelled_before_fire': True, 'bounded_join': True}
    assert 'dump_probe_threads' in stderr
    print(json.dumps(outcome, sort_keys=True))


def test_probe_preserves_fatal_handler_without_native_async_diagnostic_dumps():
    tree = ast.parse(WORKER.read_text(encoding='utf-8'))
    native_calls = {node.func.attr for node in ast.walk(tree)
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name) and node.func.value.id == 'faulthandler'}
    assert native_calls == {'enable', 'dump_traceback'}
    drive = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'drive')
    assert any(isinstance(node, ast.Try) and any(
        isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == 'stop_phase_watchdog'
        for statement in node.finalbody for call in ast.walk(statement)) for node in ast.walk(drive))


def test_drive_cancels_phase_watchdog_when_recovery_seed_fails(tmp_path):
    names = {'drive', 'fresh_resource_observation', 'dump_probe_threads',
             'start_phase_watchdog', 'stop_phase_watchdog'}
    tree = ast.parse(WORKER.read_text(encoding='utf-8'))
    definitions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    namespace = {'time': time, 'threading': threading, 'traceback': traceback,
                 'gc': SimpleNamespace(collect=lambda: None),
                 'PROFILE': SimpleNamespace(phase_seconds={'cold': .01}),
                 'state': {'critical': 0, 'running': True, 'phases': {}}, 'state_lock': threading.Lock(),
                 'retired_keys_seen': set(),
                 'ROOT': tmp_path, 'ROLE': 'web', 'json': json,
                 'resource_snapshot': lambda: {'resources': {'process': {'rss_bytes': 1}}},
                 'read_effective_resources': lambda: _resource_counters(1),
                 'get_capacity_manager': lambda: SimpleNamespace(snapshot=lambda: {}),
                 'catalogue_diagnostics': lambda: {},
                 'seed_recovery': lambda _: (_ for _ in ()).throw(RuntimeError('seed failed'))}
    from collections import deque
    namespace['deque'] = deque
    namespace['faulthandler'] = SimpleNamespace(dump_traceback=lambda **_: pytest.fail('cancelled timer fired'))
    exec(compile(ast.Module(body=definitions, type_ignores=[]), str(WORKER), 'exec'), namespace)
    original_start = namespace['start_phase_watchdog']
    watchdogs = []
    def record_start(delay):
        timer = original_start(delay)
        watchdogs.append(timer)
        return timer
    namespace['start_phase_watchdog'] = record_start
    namespace['drive']('cold')
    assert len(watchdogs) == 1 and watchdogs[0].daemon and not watchdogs[0].is_alive()
    assert namespace['state']['phases']['cold']['errors'] == ['RuntimeError: seed failed']
    assert not namespace['state']['phases']['cold']['workload_completed']
    assert json.loads((tmp_path / 'verification-state-web.json').read_text())['running'] is False


def _resource_counters(rss, *, peak=None):
    return {'process': {'rss_bytes': rss},
            'memory': {'current_bytes': rss, 'peak_bytes': rss if peak is None else peak},
            'host': {'memory_available_bytes': 10_000}}


def _run_retirement_probe(tmp_path, *, cached_rss, final_read_fails=False, worker_fails=False):
    from collections import deque
    from concurrent.futures import ThreadPoolExecutor

    tree = ast.parse(WORKER.read_text(encoding='utf-8'))
    definitions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                   and node.name in {'drive', 'fresh_resource_observation'}]
    lifecycle = {'pool_shutdown': False, 'active_workers': 0, 'retired': False,
                 'collected': False, 'reads': [], 'watchdogs_stopped': 0}
    lock = threading.Lock()
    keys = set()

    class Pool(ThreadPoolExecutor):
        def __exit__(self, *args):
            result = super().__exit__(*args)
            lifecycle['pool_shutdown'] = True
            return result

    def work(index, _phase):
        with lock:
            lifecycle['active_workers'] += 1
            lifecycle['retired'] = lifecycle['collected'] = False
        try:
            time.sleep(.03)
            if worker_fails and index == 0:
                raise RuntimeError('phase worker failed')
            with lock:
                keys.add(index)
            return True
        finally:
            with lock:
                lifecycle['active_workers'] -= 1

    def retire(**kwargs):
        assert kwargs == {'all_idle': True}
        assert lifecycle['active_workers'] == 0
        lifecycle['retired'], lifecycle['collected'] = True, False

    def collect():
        assert lifecycle['retired']
        lifecycle['collected'] = True

    def read():
        if not lifecycle['reads']:
            lifecycle['reads'].append('before')
            return _resource_counters(100)
        assert lifecycle['active_workers'] == 0
        assert lifecycle['retired'] and lifecycle['collected']
        if lifecycle['pool_shutdown']:
            lifecycle['reads'].append('after_pool_shutdown_retirement_gc')
            if final_read_fails:
                raise OSError('final counters unavailable')
            return _resource_counters(120)
        lifecycle['reads'].append('after_iteration_retirement_gc')
        return _resource_counters(110)

    def stop(_watchdog):
        lifecycle['watchdogs_stopped'] += 1
        return True

    namespace = {
        'time': time, 'traceback': traceback, 'json': json, 'deque': deque,
        'gc': SimpleNamespace(collect=collect), 'ThreadPoolExecutor': Pool,
        'PROFILE': SimpleNamespace(phase_seconds={'key_retirement': .24},
                                   concurrent_requests=2, retired_keys=2),
        'state': {'critical': 0, 'running': True, 'phases': {}},
        'state_lock': lock, 'retired_keys_seen': keys, 'ROOT': tmp_path, 'ROLE': 'web',
        'read_effective_resources': read,
        # This stale asynchronous observation represents a different lifecycle.
        'resource_snapshot': lambda: {'resources': _resource_counters(cached_rss, peak=2000)},
        'get_capacity_manager': lambda: SimpleNamespace(snapshot=lambda: {'bytes': 0}),
        'catalogue_diagnostics': lambda: {'entries': 0, 'clients': 0, 'bytes': 0},
        'seed_recovery': lambda _: None, 'background_recovery_progress': lambda: None,
        'start_phase_watchdog': lambda _: object(), 'stop_phase_watchdog': stop,
        'retire_catalogues': retire, 'work': work,
    }
    exec(compile(ast.Module(body=definitions, type_ignores=[]), str(WORKER), 'exec'), namespace)
    namespace['drive']('key_retirement')
    result = namespace['state']['phases']['key_retirement']
    assert lifecycle['watchdogs_stopped'] == 1 and lifecycle['pool_shutdown']
    assert json.loads((tmp_path / 'verification-state-web.json').read_text())['running'] is False
    return result, lifecycle


@pytest.mark.parametrize('cached_rss', [1, 1000])
def test_retirement_rss_uses_fresh_quiescent_boundaries_and_keeps_busy_peaks(tmp_path, cached_rss):
    result, lifecycle = _run_retirement_probe(tmp_path, cached_rss=cached_rss)
    assert result['errors'] == [] and result['workload_completed']
    assert result['sample_count'] >= 2
    assert result['collector_sample_count'] == result['sample_count']
    assert result['retired_keys_seen'] == 2
    assert result['rss_before_bytes'] == 100
    assert result['retirement_mid_rss_bytes'] == 110
    assert result['rss_after_bytes'] == 120
    assert result['rss_peak_bytes'] == max(cached_rss, 120)
    assert result['memory_peak_bytes'] == 2000
    assert result['rss_measurement_source'] == 'fresh_read_effective_resources'
    assert (result['rss_before_measured_at_unix_seconds']
            <= result['retirement_mid_measured_at_unix_seconds']
            <= result['rss_after_measured_at_unix_seconds'])
    assert result['retirement_mid_lifecycle'] == 'iteration_workers_completed_catalogues_retired_gc_completed'
    assert result['rss_after_lifecycle'] == 'phase_pool_shutdown_catalogues_retired_gc_completed'
    assert lifecycle['reads'][0] == 'before'
    assert lifecycle['reads'][-1] == 'after_pool_shutdown_retirement_gc'


def test_retirement_final_counter_failure_cancels_watchdog_without_cached_fallback(tmp_path):
    result, _ = _run_retirement_probe(tmp_path, cached_rss=1000, final_read_fails=True)
    assert result['errors'] == ['Final resource measurement OSError: final counters unavailable']
    assert result['workload_completed'] is False
    assert result['rss_after_bytes'] is None
    assert result['rss_after_measured_at_unix_seconds'] is None
    assert result['rss_peak_bytes'] == 1000


def test_retirement_worker_failure_joins_workers_before_final_measurement(tmp_path):
    result, lifecycle = _run_retirement_probe(tmp_path, cached_rss=1000, worker_fails=True)
    assert result['errors'] == ['RuntimeError: phase worker failed']
    assert result['workload_completed'] is False
    assert result['rss_after_bytes'] == 120
    assert result['retirement_mid_rss_bytes'] is None
    assert lifecycle['active_workers'] == 0


@pytest.fixture
def probe(tmp_path, monkeypatch, pg):
    from backend.agent import unified_trading

    monkeypatch.setattr(database, 'DB_NAME', str(tmp_path / 'probe.sqlite'))
    with database.get_db_conn() as conn:
        pass  # Native schema installed by the isolated PostgreSQL fixture.
    monkeypatch.setattr(config, 'get_config_by_id', config.get_config_by_id)
    monkeypatch.setattr(unified_trading, 'query_unified_operation', unified_trading.query_unified_operation)
    monkeypatch.setattr(runtime_capacity, '_manager', runtime_capacity.CapacityManager())
    # This fixture contains task receipts only, so unrelated MCP recovery has
    # no work. The original task reconcile and SQLite writes remain real.
    monkeypatch.setattr(background_receipts, 'reconcile_mcp', lambda **_: {'processed': 0})
    source = Path('scripts/capacity/worker.py')
    tree = ast.parse(source.read_text(encoding='utf-8'))
    selected = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                and node.name in {'seed_recovery', 'background_recovery_progress'}]
    assert len(selected) == 2
    namespace = {'json': json, 'CapacityBusy': runtime_capacity.CapacityBusy,
                 'PROFILE': SimpleNamespace(phase_seconds=dict.fromkeys(PHASES, 2)),
                 'CFG': {'config_id': 'verification', 'mode': 'STRATEGY',
                         'exit_mode': 'independent_exits', 'symbol': 'ETH/USDT'},
                 'state': {'critical': 0, 'busy': 0}, 'state_lock': threading.Lock(),
                 'recovery_target': None, 'recovery_seen': set()}
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(source), 'exec'), namespace)
    return namespace


def test_scheduler_completion_counts_durable_original_when_second_helper_processed_zero(probe):
    probe['seed_recovery']('slow_dependencies')
    rowid, operation_id = probe['recovery_target']
    # The real scheduler helper wins the race before the explicit probe call.
    first = background_receipts.reconcile(limit=1, after_cursor=rowid-1)
    assert first['processed'] == 1
    second = background_receipts.reconcile(limit=1, after_cursor=rowid-1)
    assert second['processed'] == 0
    with database.get_db_conn() as conn:
        latest = json.loads(conn.execute('SELECT result FROM trade_action_runs WHERE storage_id=%s', (rowid,)).fetchone()[0])
    assert latest['operation_id'] == operation_id and latest['status'] == 'completed'
    assert probe['state']['critical'] == 0
    probe['background_recovery_progress']()
    probe['background_recovery_progress']()
    assert probe['state']['critical'] == 1
    assert probe['recovery_seen'] == {operation_id}


def test_pending_original_never_counts_even_when_helper_reports_processed(probe, monkeypatch):
    probe['seed_recovery']('concurrent_refresh')
    monkeypatch.setattr(background_receipts, 'reconcile', lambda **_: {'processed': 1})
    probe['background_recovery_progress']()
    assert probe['state']['critical'] == 0
    assert not probe['recovery_seen']


def test_completed_other_identity_cannot_count_as_seeded_original(probe, monkeypatch):
    probe['seed_recovery']('slow_dependencies')
    rowid, _ = probe['recovery_target']
    with database.get_db_conn() as conn:
        conn.execute('UPDATE trade_action_runs SET result=%s WHERE storage_id=%s',
                     (json.dumps({'operation_id': 'another-original', 'status': 'completed'}), rowid))
        conn.commit()
    monkeypatch.setattr(background_receipts, 'reconcile', lambda **_: {'processed': 0})
    with pytest.raises(AssertionError, match='original identity'):
        probe['background_recovery_progress']()
    assert probe['state']['critical'] == 0
    assert not probe['recovery_seen']
