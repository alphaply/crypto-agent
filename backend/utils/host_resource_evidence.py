"""Bounded host-side observations, independent of application workers.

Docker inspection deliberately selects fields, never Config.Env or credentials.
No host/container modification, restart, execution or trade is performed.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess

from backend.utils.resource_diagnostics import classify_incident, read_effective_resources

_NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$')
_KERNEL = re.compile(r'global_oom|oom-kill|Out of memory|Killed process|Memory cgroup out of memory|Free swap', re.I)


def kernel_tail(path, *, byte_limit=256 * 1024, line_limit=64):
    """Only inspect a bounded tail; do not load multi-GB host logs."""
    try:
        with Path(path).open('rb') as handle:
            handle.seek(0, 2)
            size = handle.tell()
            start = max(0, size - byte_limit)
            handle.seek(start)
            data = handle.read(byte_limit)
        text = data.decode('utf-8', errors='replace')
        if start:
            text = text.partition('\n')[2]
        lines = deque((line[:4096] for line in text.splitlines() if _KERNEL.search(line)), maxlen=line_limit)
        return {'availability': 'available', 'file': str(path), 'source_size_bytes': size,
                'tail_start_byte': start, 'lines': list(lines),
                'clock': 'original source timestamp; no year or timezone inferred'}
    except OSError:
        return {'availability': 'unavailable', 'file': str(path), 'lines': []}


def inspect_container(name, *, runner=subprocess.run):
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise ValueError('Invalid container name')
    # --format suppresses all environment and mounted path contents at source.
    template = '{{json .Id}}|{{json .Image}}|{{json .Created}}|{{json .State}}|{{json .HostConfig.Memory}}|{{json .HostConfig.MemorySwap}}|{{json .HostConfig.NanoCpus}}|{{json (index .Config.Labels "org.opencontainers.image.revision")}}'
    try:
        result = runner(['docker', 'inspect', '--format', template, name],
                        capture_output=True, text=True, timeout=5, check=False)
        if result.returncode or len(result.stdout.encode('utf-8')) > 16384:
            return {'name': name, 'availability': 'unavailable'}
        fields = result.stdout.strip().split('|')
        if len(fields) != 8:
            return {'name': name, 'availability': 'unavailable'}
        identity, image, created, state, memory, memswap, nano, revision = [json.loads(field) for field in fields]
        # State.Error is arbitrary error text; omit it rather than echoing it.
        safe_state = {key: state.get(key) for key in ('Status', 'Running', 'OOMKilled', 'Pid',
                                                     'ExitCode', 'StartedAt', 'FinishedAt')}
        return {'name': name, 'availability': 'available', 'container_id': identity,
                'image_id': image, 'created_at': created, 'image_revision': revision,
                'state': safe_state, 'intended_limits': {'memory_bytes': memory,
                'memory_swap_total_bytes': memswap, 'cpu_cpus': nano / 1e9 if nano else None}}
    except (OSError, subprocess.TimeoutExpired, ValueError, TypeError):
        return {'name': name, 'availability': 'unavailable'}


def host_observation(names, *, kernel_path='/var/log/kern.log', reader=read_effective_resources,
                     inspector=inspect_container):
    if not 1 <= len(names) <= 8:
        raise ValueError('Observe 1 to 8 explicitly named containers')
    sources = kernel_tail(kernel_path)
    resources = reader()
    containers = [inspector(name) for name in names]
    pressure = [{'swap_exhausted': resources['host'].get('swap_total_bytes') not in (None, 0)
                                 and resources['host'].get('swap_free_bytes') == 0}]
    incident = classify_incident(kernel_lines=sources['lines'], pressure_samples=pressure)
    return {'observed_at_utc': datetime.now(timezone.utc).isoformat(),
            'observer': 'independent_host', 'resources': resources, 'containers': containers,
            'kernel_evidence': sources, 'classification_of_available_evidence': incident,
            'scope': 'bounded log tail, current counters and named container snapshots; historical events are not attributed to the current container'}
