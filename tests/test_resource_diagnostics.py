import json
from pathlib import Path
import threading
import time
import os
import subprocess
import sys

import pytest

from backend.utils import resource_diagnostics as diagnostics
from backend.utils.runtime_capacity import CapacityManager


def write(root, name, value):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(value), encoding='utf-8')


def counters(tmp_path, *, version=2):
    root, proc = tmp_path / 'cgroup', tmp_path / 'proc'
    root.mkdir()
    write(proc, 'meminfo', 'MemTotal: 8000000 kB\nMemAvailable: 3000000 kB\nSwapTotal: 1000000 kB\nSwapFree: 0 kB')
    write(proc, 'self/status', 'VmRSS: 120000 kB')
    if version == 2:
        for name, value in {'memory.max': 10000000, 'memory.current': 9000000,
                'memory.peak': 9500000, 'memory.swap.max': 1000000,
                'memory.swap.current': 10000, 'memory.events': 'oom 2\noom_kill 1\nhigh 4',
                'cpu.max': '100000 200000', 'cpu.stat': 'nr_throttled 3\nthrottled_usec 9000'}.items():
            write(root, name, value)
    else:
        for name, value in {'memory/memory.limit_in_bytes': 10000000,
                'memory/memory.usage_in_bytes': 9000000,
                'memory/memory.max_usage_in_bytes': 9500000,
                'memory/memory.memsw.limit_in_bytes': 11000000,
                'memory/memory.memsw.usage_in_bytes': 9010000,
                'memory/memory.oom_control': 'under_oom 0\noom_kill 1',
                'cpu/cpu.cfs_quota_us': 100000, 'cpu/cpu.cfs_period_us': 200000,
                'cpu/cpu.stat': 'nr_throttled 3\nthrottled_time 9000000'}.items():
            write(root, name, value)
    return root, proc


@pytest.mark.parametrize('version', [1, 2])
def test_effective_limits_not_intended_environment(tmp_path, monkeypatch, version):
    root, proc = counters(tmp_path, version=version)
    monkeypatch.setenv('MEMORY_LIMIT', '999999999')
    result = diagnostics.read_effective_resources(root, proc)
    assert result['cgroup_version'] == version
    assert result['memory']['limit_bytes'] == 10000000
    assert result['memory']['peak_bytes'] == 9500000
    assert result['memory']['swap_limit_bytes'] == 1000000
    assert result['memory']['swap_current_bytes'] == 10000
    assert result['memory']['events']['oom_kill'] == 1
    assert result['cpu']['quota_cpus'] == .5
    assert result['cpu']['statistics']['nr_throttled'] == 3
    assert result['process']['rss_bytes'] == 120000 * 1024
    assert result['host']['swap_free_bytes'] == 0


def test_unavailable_is_not_zero_or_safe_unlimited(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnostics, 'process_rss_bytes', lambda _: None)
    result = diagnostics.read_effective_resources(tmp_path / 'absent', tmp_path / 'absent')
    assert result['memory']['current_bytes'] is None
    assert result['memory']['limit_status'] == 'unavailable'
    assert result['cpu']['quota_cpus'] is None
    assert result['host']['swap_free_bytes'] is None
    assert result['process']['rss_bytes'] is None


@pytest.mark.parametrize('version', [1, 2])
def test_explicit_unlimited_and_missing_swap_distinct(tmp_path, version):
    root, proc = counters(tmp_path, version=version)
    if version == 2:
        write(root, 'memory.max', 'max')
        write(root, 'cpu.max', 'max 100000')
        (root / 'memory.swap.max').unlink()
    else:
        write(root, 'memory/memory.limit_in_bytes', 9223372036854771712)
        write(root, 'cpu/cpu.cfs_quota_us', '-1')
        (root / 'memory/memory.memsw.limit_in_bytes').unlink()
    result = diagnostics.read_effective_resources(root, proc)
    assert result['memory']['limit_status'] == 'unlimited'
    assert result['memory']['limit_bytes'] is None
    assert result['memory']['swap_limit_status'] == 'unavailable'
    assert result['cpu']['limit_status'] == 'unlimited'


def collector(tmp_path, monkeypatch, **kwargs):
    root, proc = counters(tmp_path)
    manager = CapacityManager(byte_limit=1048576)
    monkeypatch.setattr('backend.utils.runtime_capacity.get_capacity_manager', lambda: manager)
    return diagnostics.ResourceCollector(tmp_path / 'evidence',
        reader=lambda: diagnostics.read_effective_resources(root, proc), **kwargs), root, manager


def test_delta_identity_pressure_and_configuration(tmp_path, monkeypatch):
    item, root, manager = collector(tmp_path, monkeypatch)
    monkeypatch.setenv('IMAGE_REVISION', 'fake-commit')
    first = item.sample()
    assert first['cgroup_oom_kill_delta'] is None
    assert first['capacity']['pressure'] is True
    assert manager.snapshot()['pressure'] is True
    write(root, 'memory.events', 'oom_kill 3')
    write(root, 'memory.current', '1000')
    second = item.sample()
    assert second['cgroup_oom_kill_delta'] == 2
    assert first['startup_id'] == second['startup_id']
    assert second['capacity']['pressure'] is False
    assert first['configuration_fingerprint'] == second['configuration_fingerprint']


def test_declared_limits_mismatch_and_oversized_application_budget_are_explicit(tmp_path, monkeypatch):
    item, root, manager = collector(tmp_path, monkeypatch)
    monkeypatch.setenv('RESOURCE_EXPECTED_MEMORY_BYTES','20000000')
    monkeypatch.setenv('RESOURCE_EXPECTED_MEMORY_SWAP_TOTAL_BYTES','11000000')
    monkeypatch.setenv('RESOURCE_EXPECTED_CPU_CPUS','0.5')
    manager.byte_limit=20000000
    write(root,'memory.current','1000')
    observed=item.sample()
    assert observed['container_limit_comparison']=={'memory_bytes':'mismatched','memory_swap_total_bytes':'matched','cpu_cpus':'matched'}
    assert observed['declared_container_limits']['memory_bytes']==20000000
    assert observed['capacity_bytes_exceed_container']
    assert manager.snapshot()['pressure_reason']=='capacity_bytes_exceed_container'
    with pytest.raises(ValueError,match='busy'):
        manager.acquire('agent',timeout=0)
    item.stop()


def test_stage_and_file_budgets_do_not_leak_payload(tmp_path, monkeypatch):
    item, _, _ = collector(tmp_path, monkeypatch, max_bytes=8192, backups=2, buffer_size=8)
    secret = 'sk-secret-not-diagnostic'
    monkeypatch.setenv('API_KEY', secret)
    assert not item.record_stage(secret, 'completed')
    for index in range(50):
        item.record_stage('execution', 'started', role='not-a-role',
            request_id=f'request-{index}', operation_id=secret + '/invalid')
    first = item.sample()
    assert len(first['stages']) <= 8
    assert first['stage_records_dropped'] >= 42
    for _ in range(40):
        item.sample()
    item.stop()
    paths = list(item.directory.iterdir())
    assert len(paths) <= 3
    for path in paths:
        assert path.stat().st_size <= 8192
        text = path.read_text(encoding='utf-8')
        assert secret not in text
        for line in text.splitlines():
            assert json.loads(line)['startup_id'] == item.startup_id


def test_io_failure_is_bounded_and_collector_retries(tmp_path, monkeypatch):
    item, _, _ = collector(tmp_path, monkeypatch, interval=.05)
    original = item._setup
    ready = threading.Event()

    def failing():
        if not ready.is_set():
            raise OSError('do not copy this message')
        original()

    monkeypatch.setattr(item, '_setup', failing)
    started = time.monotonic()
    item.start()
    assert time.monotonic() - started < .5
    assert item.last_snapshot['availability'] == 'unavailable'
    # Worker stage capture does not perform any failed filesystem access.
    item.record_stage('execution', 'unknown', operation_id='original-operation')
    ready.set()
    for _ in range(100):
        if item.path.exists():
            break
        time.sleep(.01)
    item.stop()
    lines = [json.loads(line) for line in item.path.read_text().splitlines()]
    assert any(line['persistence_failures'] >= 1 for line in lines)
    assert any(stage.get('operation_id') == 'original-operation'
               for line in lines for stage in line['stages'])
    assert 'do not copy this' not in item.path.read_text()


@pytest.mark.parametrize('kwargs', [{'interval': float('nan')}, {'interval': float('inf')},
    {'max_bytes': 1024.5}, {'backups': 0}, {'buffer_size': float('inf')}])
def test_invalid_capacity_configuration_rejected(tmp_path, kwargs):
    with pytest.raises(ValueError):
        diagnostics.ResourceCollector(tmp_path, **kwargs)


def test_incident_evidence_does_not_confuse_victim_or_restart():
    result = diagnostics.classify_incident(kernel_lines=['kernel: global_oom victim python'],
        restarted_oom_killed=False)
    assert result['classification'] == 'global_oom_confirmed'
    assert result['attribution'] == 'container_unmapped'
    assert diagnostics.classify_incident(cgroup_oom_kill_delta=2)['classification'] == 'cgroup_oom_confirmed'
    assert diagnostics.classify_incident(pressure_samples=[{'memory_pressure': True}])['classification'] == 'resource_pressure_observed'
    assert diagnostics.classify_incident(restarted_oom_killed=False)['classification'] == 'insufficient_evidence'
    seen = []

    def unbounded():
        for index in range(1000000):
            seen.append(index)
            yield 'missing event'

    diagnostics.classify_incident(kernel_lines=unbounded())
    assert len(seen) == 256


def test_resource_endpoint_requires_login(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.app.api.resources import router
    from backend.app.core.deps import get_current_user
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        assert client.get('/api/resources/snapshot').status_code == 401
        app.dependency_overrides[get_current_user] = lambda: {'username': 'test'}
        assert client.get('/api/resources/snapshot').status_code == 200


def test_saved_evidence_survives_worker_death_and_restart(tmp_path):
    script = """import os, sys
from backend.utils.resource_diagnostics import ResourceCollector
collector = ResourceCollector(sys.argv[1], role='verification')
collector.record_stage('execution','unknown',operation_id='original-unknown')
collector.sample()
os._exit(9)
"""
    evidence = tmp_path / 'independent-evidence'
    for _ in range(2):
        result = subprocess.run([sys.executable, '-c', script, str(evidence)],
                                capture_output=True, text=True, timeout=10, env=dict(os.environ))
        assert result.returncode == 9, result.stderr
    records = [json.loads(line) for line in (evidence / 'verification.jsonl').read_text().splitlines()]
    assert len(records) == 2
    assert records[0]['startup_id'] != records[1]['startup_id']
    assert all(row['stages'][0]['operation_id'] == 'original-unknown' for row in records)
