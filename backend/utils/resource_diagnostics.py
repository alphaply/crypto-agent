"""Bounded, read-only resource evidence. Missing counters are never fake zeros."""
from __future__ import annotations

import ctypes
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
from pathlib import Path
import re
import sys
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from itertools import islice


_MAX_READ = 64 * 1024
_STAGES = frozenset({'startup', 'authentication', 'queue', 'capacity', 'metadata',
                     'exchange', 'persistence', 'execution', 'recovery', 'shutdown'})
_OUTCOMES = frozenset({'started', 'completed', 'busy', 'timeout', 'failed', 'unknown'})
_IDENTITY = re.compile(r'^[a-zA-Z0-9_.:-]{1,160}$')
_collector = None
_collector_lock = threading.Lock()


class EvidenceHandler(RotatingFileHandler):
    def handleError(self, record):
        # logging normally swallows write errors. Evidence must expose a gap,
        # without printing account data or exceptions into stderr.
        raise OSError('Resource evidence could not be persisted')


def _read(path: Path):
    try:
        with path.open('r', encoding='utf-8', errors='replace') as handle:
            text = handle.read(_MAX_READ + 1)
        return text.strip() if len(text) <= _MAX_READ else None
    except (OSError, ValueError):
        return None


def _integer(text):
    try:
        number = int(text)
        return number if number >= 0 else None
    except (TypeError, ValueError):
        return None


def _limit(text, *, legacy=False):
    if text == 'max':
        return None, 'unlimited'
    value = _integer(text)
    if value is None:
        return None, 'unavailable'
    if legacy and value >= (1 << 60):
        return None, 'unlimited'
    return value, 'finite'


def _pairs(text):
    result = {}
    for line in (text or '').splitlines():
        fields = line.split()
        if len(fields) == 2 and _integer(fields[1]) is not None:
            result[fields[0]] = int(fields[1])
    return result


def _memory_info(proc_root):
    fields = {}
    for line in (_read(proc_root / 'meminfo') or '').splitlines():
        parts = line.replace(':', '').split()
        if len(parts) >= 2 and _integer(parts[1]) is not None:
            fields[parts[0]] = int(parts[1]) * (1024 if parts[-1] == 'kB' else 1)
    return {key: fields.get(source) for key, source in {
        'memory_total_bytes': 'MemTotal', 'memory_available_bytes': 'MemAvailable',
        'swap_total_bytes': 'SwapTotal', 'swap_free_bytes': 'SwapFree'}.items()}


def process_rss_bytes(proc_root=Path('/proc')):
    for line in (_read(Path(proc_root) / 'self' / 'status') or '').splitlines():
        if line.startswith('VmRSS:'):
            parts = line.split()
            amount = _integer(parts[1]) if len(parts) > 1 else None
            return amount * 1024 if amount is not None else None
    if os.name != 'nt':
        return None
    try:
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD)] + [
                (name, ctypes.c_size_t) for name in ('PeakWorkingSetSize', 'WorkingSetSize',
                'QuotaPeakPagedPoolUsage', 'QuotaPagedPoolUsage', 'QuotaPeakNonPagedPoolUsage',
                'QuotaNonPagedPoolUsage', 'PagefileUsage', 'PeakPagefileUsage')]
        counter = Counters()
        counter.cb = ctypes.sizeof(counter)
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        psapi = ctypes.WinDLL('psapi', use_last_error=True)
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        if psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counter), counter.cb):
            return int(counter.WorkingSetSize)
    except (OSError, AttributeError, ValueError):
        pass
    return None


def read_effective_resources(cgroup_root=Path('/sys/fs/cgroup'), proc_root=Path('/proc')):
    """cgroup v1/v2 fixtureable readers; limits describe this visible cgroup."""
    root, proc = Path(cgroup_root), Path(proc_root)
    memory, cpu, version = {}, {}, None
    if (root / 'memory.max').exists():
        version = 2
        limit, state = _limit(_read(root / 'memory.max'))
        swap_limit, swap_state = _limit(_read(root / 'memory.swap.max'))
        memory = {'current_bytes': _integer(_read(root / 'memory.current')),
                  'peak_bytes': _integer(_read(root / 'memory.peak')),
                  'limit_bytes': limit, 'limit_status': state,
                  'swap_current_bytes': _integer(_read(root / 'memory.swap.current')),
                  'swap_limit_bytes': swap_limit, 'swap_limit_status': swap_state,
                  'events': _pairs(_read(root / 'memory.events'))}
        maximum = (_read(root / 'cpu.max') or '').split()
        quota = _integer(maximum[0]) if maximum and maximum[0] != 'max' else None
        period = _integer(maximum[1]) if len(maximum) == 2 else None
        cpu = {'quota_us': quota, 'period_us': period,
               'quota_cpus': quota / period if quota is not None and period else None,
               'limit_status': 'unlimited' if maximum and maximum[0] == 'max' else
                               'finite' if quota is not None and period else 'unavailable',
               'statistics': _pairs(_read(root / 'cpu.stat'))}
    elif (root / 'memory' / 'memory.limit_in_bytes').exists():
        version = 1
        mem_root, cpu_root = root / 'memory', root / 'cpu'
        if not cpu_root.exists():
            cpu_root = root / 'cpu,cpuacct'
        limit, state = _limit(_read(mem_root / 'memory.limit_in_bytes'), legacy=True)
        memsw_limit, memsw_state = _limit(_read(mem_root / 'memory.memsw.limit_in_bytes'), legacy=True)
        current = _integer(_read(mem_root / 'memory.usage_in_bytes'))
        memsw_current = _integer(_read(mem_root / 'memory.memsw.usage_in_bytes'))
        memory = {'current_bytes': current,
                  'peak_bytes': _integer(_read(mem_root / 'memory.max_usage_in_bytes')),
                  'limit_bytes': limit, 'limit_status': state,
                  'memory_swap_total_limit_bytes': memsw_limit,
                  'swap_limit_bytes': max(0, memsw_limit - limit) if memsw_limit is not None and limit is not None else None,
                  'swap_limit_status': memsw_state,
                  'swap_current_bytes': max(0, memsw_current - current) if memsw_current is not None and current is not None else None,
                  'events': _pairs(_read(mem_root / 'memory.oom_control'))}
        quota_text = _read(cpu_root / 'cpu.cfs_quota_us')
        quota, period = _integer(quota_text), _integer(_read(cpu_root / 'cpu.cfs_period_us'))
        cpu = {'quota_us': quota, 'period_us': period,
               'quota_cpus': quota / period if quota is not None and period else None,
               'limit_status': 'unlimited' if quota_text == '-1' else
                               'finite' if quota is not None and period else 'unavailable',
               'statistics': _pairs(_read(cpu_root / 'cpu.stat'))}
    else:
        memory = {key: None for key in ('current_bytes', 'peak_bytes', 'limit_bytes',
                                      'swap_current_bytes', 'swap_limit_bytes')}
        memory.update(limit_status='unavailable', swap_limit_status='unavailable', events={})
        cpu = {'quota_cpus': None, 'limit_status': 'unavailable', 'statistics': {}}
    try:
        affinity = len(os.sched_getaffinity(0))
    except (OSError, AttributeError):
        affinity = None
    quota_cpus = cpu.get('quota_cpus')
    cpu['affinity_cpus'] = affinity
    known = [amount for amount in (quota_cpus, affinity) if amount is not None]
    cpu['effective_cpus'] = min(known) if known else None
    host = _memory_info(proc)
    host['oom_kill_counter'] = _pairs(_read(proc / 'vmstat')).get('oom_kill')
    return {'cgroup_version': version, 'memory': memory, 'cpu': cpu,
            'process': {'pid': os.getpid(), 'rss_bytes': process_rss_bytes(proc)},
            'host': host, 'availability': {'cgroup': 'available' if version else 'unavailable',
            'host': 'available' if host['memory_total_bytes'] is not None else 'unavailable'}}


def _finite_environment(name, default, minimum, *, integer=False):
    value = float(os.getenv(name, default))
    if not math.isfinite(value) or value < minimum or integer and not value.is_integer():
        raise ValueError(f'{name} must be a finite {"integer" if integer else "number"} >= {minimum}')
    return int(value) if integer else value


class ResourceCollector:
    """A daemon samples outside workers, writes bounded files, never emits secrets."""
    def __init__(self, directory, *, role='web', reader=read_effective_resources,
                 interval=None, max_bytes=None, backups=None, buffer_size=None):
        if role not in {'web', 'scheduler', 'verification'}:
            raise ValueError('Unknown resource collector role')
        self.role, self.reader, self.directory = role, reader, Path(directory)
        self.interval = interval if interval is not None else _finite_environment('RESOURCE_SAMPLE_SECONDS', 3, 0.1)
        self.max_bytes = max_bytes if max_bytes is not None else _finite_environment('RESOURCE_EVIDENCE_BYTES', 1024 * 1024, 1024, integer=True)
        self.backups = backups if backups is not None else _finite_environment('RESOURCE_EVIDENCE_FILES', 4, 1, integer=True)
        self.buffer_size = buffer_size if buffer_size is not None else _finite_environment('RESOURCE_STAGE_BUFFER', 64, 1, integer=True)
        if (not math.isfinite(self.interval) or self.interval <= 0
                or type(self.max_bytes) is not int or self.max_bytes < 1024
                or type(self.backups) is not int or self.backups < 1
                or type(self.buffer_size) is not int or self.buffer_size < 1):
            raise ValueError('Invalid resource collector bounds')
        self.startup_id, self.started_at = uuid.uuid4().hex, time.time()
        self.container_id = os.getenv('CONTAINER_ID') or (os.getenv('HOSTNAME') if os.name != 'nt' else None)
        self.image_revision = os.getenv('IMAGE_REVISION') or None
        self.stop_event, self.stage_lock = threading.Event(), threading.Lock()
        self.stages, self.stage_dropped = deque(maxlen=self.buffer_size), 0
        self.previous_oom = None
        self.path = self.directory / f'{role}.jsonl'
        self.logger = logging.Logger(f'resource-evidence-{role}-{self.startup_id}')
        self.handler = None
        self.thread = None
        self.last_snapshot = None
        self.sample_lock = threading.Lock()
        self.persistence_failures = 0

    def _setup(self):
        if self.handler is not None:
            return
        self.directory.mkdir(parents=True, exist_ok=True)
        self.handler = EvidenceHandler(self.path, maxBytes=self.max_bytes,
                                          backupCount=self.backups, encoding='utf-8', delay=True)
        self.handler.setFormatter(logging.Formatter('%(message)s'))
        self.logger.addHandler(self.handler)
        self.logger.setLevel(logging.INFO)

    def record_stage(self, stage, outcome, *, role=None, request_id=None, operation_id=None):
        if stage not in _STAGES or outcome not in _OUTCOMES:
            return False
        entry = {'stage': stage, 'outcome': outcome, 'at': time.time()}
        if role:
            from backend.utils.runtime_capacity import ROLE_DEFAULTS
            if role in ROLE_DEFAULTS:
                entry['role'] = role
        for key, identity in (('request_id', request_id), ('operation_id', operation_id)):
            if isinstance(identity, str) and _IDENTITY.fullmatch(identity):
                entry[key] = identity
        with self.stage_lock:
            if len(self.stages) == self.buffer_size:
                self.stage_dropped += 1
            self.stages.append(entry)
        return True

    def sample(self):
        with self.sample_lock:
            return self._sample()

    def _sample(self):
        resources = self.reader()
        from backend.utils.runtime_capacity import get_capacity_manager
        capacity = get_capacity_manager().snapshot()
        caches = {}
        for key, module_name, getter in (
                ('catalogue', 'backend.utils.public_catalogue', 'catalogue_diagnostics'),
                ('mcp', 'backend.mcp.exchange_runtime', 'diagnostics')):
            module = sys.modules.get(module_name)
            caches[key] = getattr(module, getter)() if module else {'availability': 'not_loaded'}
        configured = {'capacity': capacity['settings'], 'environment': {
            name: os.getenv(name) for name in ('RUNTIME_CATALOGUE_BYTES',
            'RUNTIME_CATALOGUE_REFRESH_BYTES', 'RUNTIME_HTTP_RESPONSE_BYTES',
            'RUNTIME_CATALOGUE_ENTRIES', 'RUNTIME_CACHE_IDLE_SECONDS',
            'RESOURCE_PRESSURE_FRACTION', 'CAPACITY_PROFILE_SHA256',
            'RESOURCE_EXPECTED_MEMORY_BYTES', 'RESOURCE_EXPECTED_MEMORY_SWAP_TOTAL_BYTES',
            'RESOURCE_EXPECTED_CPU_CPUS') if os.getenv(name) is not None}}
        profile_fingerprint = hashlib.sha256(json.dumps(configured, sort_keys=True).encode()).hexdigest()
        maximum, current = resources['memory'].get('limit_bytes'), resources['memory'].get('current_bytes')
        intended = {}
        for key, env in (('memory_bytes','RESOURCE_EXPECTED_MEMORY_BYTES'),
                         ('memory_swap_total_bytes','RESOURCE_EXPECTED_MEMORY_SWAP_TOTAL_BYTES'),
                         ('cpu_cpus','RESOURCE_EXPECTED_CPU_CPUS')):
            intended[key] = _finite_environment(env, 1, .000001) if os.getenv(env) is not None else None
        swap = resources['memory'].get('swap_limit_bytes')
        effective = {'memory_bytes':maximum,'memory_swap_total_bytes':maximum+swap if maximum is not None and swap is not None else None,
                     'cpu_cpus':resources['cpu'].get('quota_cpus')}
        comparison = {key: 'undeclared' if value is None else 'unavailable' if effective[key] is None else 'matched' if value == effective[key] else 'mismatched'
                      for key,value in intended.items()}
        byte_budget_exceeds_limit = False
        configured['container_limits'] = intended
        threshold = _finite_environment('RESOURCE_PRESSURE_FRACTION', 0.85, 0.01)
        if threshold > 1:
            raise ValueError('RESOURCE_PRESSURE_FRACTION must be <= 1')
        pressure = byte_budget_exceeds_limit or current is not None and maximum is not None and maximum > 0 and current / maximum >= threshold
        reason = 'capacity_bytes_exceed_container' if byte_budget_exceeds_limit else 'cgroup_memory' if pressure else None
        get_capacity_manager().set_pressure(pressure, reason)
        capacity['pressure'] = pressure
        capacity['pressure_reason'] = reason
        oom = resources['memory'].get('events', {}).get('oom_kill')
        oom_delta = max(0, oom - self.previous_oom) if oom is not None and self.previous_oom is not None else None
        self.previous_oom = oom
        with self.stage_lock:
            stages = list(self.stages)
            self.stages.clear()
            dropped, self.stage_dropped = self.stage_dropped, 0
        record = {'timestamp': datetime.now(timezone.utc).isoformat(), 'startup_id': self.startup_id,
                  'started_at': self.started_at, 'role': self.role, 'container_id': self.container_id,
                  'image_revision': self.image_revision, 'configuration_fingerprint': profile_fingerprint,
                  'resources': resources, 'capacity': capacity, 'caches': caches,
                  'declared_container_limits':intended,'container_limit_comparison':comparison,
                  'capacity_bytes_exceed_container':byte_budget_exceeds_limit,
                  'cgroup_oom_kill_delta': oom_delta,
                  'stages': stages, 'stage_records_dropped': dropped,
                  'persistence_failures': self.persistence_failures}
        payload = json.dumps(record, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
        while len(payload.encode('utf-8')) + 1 > self.max_bytes and record['stages']:
            record['stages'].pop(0)
            record['stage_records_dropped'] += 1
            payload = json.dumps(record, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
        if len(payload.encode('utf-8')) + 1 > self.max_bytes:
            raise OSError('Resource evidence record exceeds the file budget')
        self.last_snapshot = record
        if self.handler is None:
            self._setup()
        self.logger.info(payload)
        self.handler.flush()
        return record

    def start(self):
        if self.thread and self.thread.is_alive():
            return self
        self.stop_event.clear()
        self.record_stage('startup', 'started')
        try:
            self.sample()
        except OSError:
            self._record_failure()
        self.thread = threading.Thread(target=self._run, name='resource-evidence', daemon=True)
        self.thread.start()
        return self

    def _run(self):
        while not self.stop_event.wait(self.interval):
            try:
                self.sample()
            except Exception:
                # I/O and missing metrics cannot block financial workers or
                # repeatedly copy arbitrary exception messages into evidence.
                self._record_failure()

    def _record_failure(self):
        self.persistence_failures += 1
        self.last_snapshot = {'availability': 'unavailable', 'role': self.role,
                              'startup_id': self.startup_id,
                              'persistence_failures': self.persistence_failures}
        self.record_stage('persistence', 'failed')

    def stop(self):
        self.stop_event.set()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=min(self.interval + 1, 3))
        if self.thread and self.thread.is_alive():
            self.record_stage('shutdown', 'timeout')
            return
        try:
            self.record_stage('shutdown', 'completed')
            self.sample()
        except Exception:
            pass
        if not self.thread or not self.thread.is_alive():
            if self.handler:
                self.handler.close()
                self.logger.removeHandler(self.handler)
                self.handler = None


def start_resource_collector(role='web'):
    global _collector
    with _collector_lock:
        if _collector is None:
            from backend.storage_paths import DATA_DIR
            collector = ResourceCollector(Path(os.getenv('RESOURCE_EVIDENCE_DIR', DATA_DIR / 'resource_evidence')), role=role)
            # Invalid configuration is a startup error. A filesystem sampling
            # failure is reported as unavailable; the application still runs.
            collector.start()
            _collector = collector
        return _collector


def stop_resource_collector():
    global _collector
    with _collector_lock:
        collector, _collector = _collector, None
    if collector:
        collector.stop()


def record_resource_stage(stage, outcome, **identities):
    collector = _collector
    return collector.record_stage(stage, outcome, **identities) if collector else False


def resource_snapshot():
    collector = _collector
    if collector and collector.last_snapshot:
        return collector.last_snapshot
    return {'resources': read_effective_resources(), 'collector': 'not_started'}


def classify_incident(*, kernel_lines=(), cgroup_oom_kill_delta=None, pressure_samples=(),
                      affected_container=None, restarted_oom_killed=None):
    """Evidence classification, not attribution of a victim as the cause."""
    evidence = []
    lines = [str(line)[:4096] for line in islice(kernel_lines, 256)]
    if any('global_oom' in line for line in lines):
        classification = 'global_oom_confirmed'
        evidence.append('kernel_global_oom')
    elif cgroup_oom_kill_delta is not None and cgroup_oom_kill_delta > 0:
        classification = 'cgroup_oom_confirmed'
        evidence.append('cgroup_oom_kill_counter_increase')
    elif any(sample.get('memory_pressure') or sample.get('swap_exhausted') for sample in islice(pressure_samples, 256)):
        classification = 'resource_pressure_observed'
        evidence.append('resource_samples')
    else:
        classification = 'insufficient_evidence'
    return {'classification': classification, 'evidence': evidence,
            'affected_container': affected_container, 'restarted_oom_killed': restarted_oom_killed,
            'attribution': 'container_unmapped' if classification == 'global_oom_confirmed' and not affected_container else
                           'cause_not_attributed',
            'limitations': ['A restarted container state does not prove the earlier incident state.',
                           'A killed process is not proof of the source of memory growth.']}
