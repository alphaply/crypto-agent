"""Predeclared, immutable capacity acceptance and bounded report validation."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

PHASES = ('cold', 'idle', 'typical', 'concurrent_refresh', 'slow_dependencies',
          'key_retirement', 'restart')


class ContainerBudget(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    memory_bytes: int = Field(ge=64 * 1024 * 1024)
    memory_swap_total_bytes: int = Field(ge=64 * 1024 * 1024)
    cpu_cpus: float = Field(gt=0, le=16)
    measured_rss_lower_bytes: int = Field(ge=0)
    memory_peak_upper_bytes: int = Field(gt=0)

    @model_validator(mode='after')
    def ordered_limits(self):
        if self.memory_swap_total_bytes < self.memory_bytes:
            raise ValueError('Docker memory+swap total cannot be below memory')
        if not self.measured_rss_lower_bytes < self.memory_peak_upper_bytes < self.memory_bytes:
            raise ValueError('Measured bounds must be strictly inside the container memory limit')
        return self


class CapacityProfile(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    version: str = Field(pattern=r'^[A-Za-z0-9_.-]{1,80}$')
    target_environment: str = Field(min_length=1, max_length=512)
    scope: str = Field(min_length=1, max_length=2048)
    phase_seconds: dict[str, int]
    catalogue_symbols: int = Field(ge=2, le=10000)
    concurrent_requests: int = Field(ge=2, le=128)
    retired_keys: int = Field(ge=2, le=128)
    web: ContainerBudget
    scheduler: ContainerBudget
    other_services_reserved_bytes: int = Field(ge=0)
    host_headroom_bytes: int = Field(gt=0)
    web_p95_ms: float = Field(gt=0)
    web_max_ms: float = Field(gt=0)
    retirement_growth_upper_bytes: int = Field(ge=0)
    retirement_steady_growth_upper_bytes: int = Field(ge=0, default=16 * 1024 * 1024)
    application: dict[str, str]

    @model_validator(mode='after')
    def complete_workload(self):
        if set(self.phase_seconds) != set(PHASES):
            raise ValueError('All cold/idle/typical/refresh/slow/retirement/restart phases are required')
        if any(type(seconds) is not int or not 2 <= seconds <= 600 for seconds in self.phase_seconds.values()):
            raise ValueError('Each phase needs a duration of 2..600 seconds')
        if not self.web_p95_ms <= self.web_max_ms:
            raise ValueError('Web p95 cannot exceed the maximum latency SLO')
        allowed = {'RUNTIME_CAPACITY_TOTAL_WORKERS', 'RUNTIME_CAPACITY_RESERVED_WORKERS',
            'RUNTIME_CAPACITY_QUEUE_LIMIT', 'RUNTIME_CAPACITY_QUEUE_SECONDS',
            'RUNTIME_CAPACITY_BYTES', 'RUNTIME_CAPACITY_ROLE_LIMITS', 'RUNTIME_CATALOGUE_BYTES',
            'RUNTIME_CATALOGUE_REFRESH_BYTES', 'RUNTIME_CATALOGUE_ENTRIES',
            'RUNTIME_HTTP_RESPONSE_BYTES', 'RUNTIME_CACHE_IDLE_SECONDS',
            'RESOURCE_SAMPLE_SECONDS', 'PAPER_MAX_PAYLOAD_BYTES', 'PAPER_SCAN_BATCH_SIZE',
            'PAPER_HISTORY_SCAN_LIMIT', 'PAPER_MAINTENANCE_SECONDS',
            'RUNTIME_SCAN_BATCH_SIZE', 'RUNTIME_MAX_RECORD_BYTES', 'RUNTIME_FINANCIAL_PAYLOAD_BYTES',
            'RUNTIME_RECOVERY_SECONDS', 'RUNTIME_COORDINATOR_ENTRIES', 'RUNTIME_CLIENT_ENTRIES',
            'RUNTIME_CLIENT_BASE_BYTES', 'RUNTIME_QUOTE_ENTRIES', 'RUNTIME_QUOTE_BYTES',
            'RUNTIME_QUOTE_REFRESH_BYTES', 'RUNTIME_INDEX_SCAN_SECONDS', 'RUNTIME_STREAM_BYTES',
            'RUNTIME_STREAM_ITEMS', 'RUNTIME_NEWS_CACHE_ENTRIES', 'RUNTIME_NEWS_CACHE_BYTES',
            'RUNTIME_NEWS_WORK_BYTES', 'MCP_RECOVERY_CONCURRENCY', 'MCP_RECOVERY_QUEUE_LIMIT',
            'MCP_RECOVERY_BUDGET_SECONDS'}
        if set(self.application) - allowed:
            raise ValueError('Only fixed resource settings belong in a capacity profile')
        required = {'RUNTIME_CAPACITY_TOTAL_WORKERS', 'RUNTIME_CAPACITY_RESERVED_WORKERS',
                    'RUNTIME_CAPACITY_BYTES', 'RUNTIME_CATALOGUE_BYTES'}
        if not required <= self.application.keys():
            raise ValueError('Application capacity settings must be declared')
        # Enforce the same runtime validation without changing the process env.
        from backend.utils.runtime_capacity import CapacityManager
        try:
            CapacityManager(total=int(self.application['RUNTIME_CAPACITY_TOTAL_WORKERS']),
                reserved=int(self.application['RUNTIME_CAPACITY_RESERVED_WORKERS']),
                queue_limit=int(self.application.get('RUNTIME_CAPACITY_QUEUE_LIMIT', '16')),
                wait_seconds=float(self.application.get('RUNTIME_CAPACITY_QUEUE_SECONDS', '2')),
                byte_limit=int(self.application['RUNTIME_CAPACITY_BYTES']),
                role_limits=json.loads(self.application.get('RUNTIME_CAPACITY_ROLE_LIMITS', '{}')))
            for name, raw in self.application.items():
                if name == 'RUNTIME_CAPACITY_ROLE_LIMITS':
                    continue
                value = float(raw)
                if not __import__('math').isfinite(value) or value < 0:
                    raise ValueError(f'Invalid {name}')
        except (ValueError, TypeError) as exc:
            raise ValueError('Invalid finite application capacity settings') from exc
        return self

    def fingerprint(self):
        canonical = json.dumps(self.model_dump(), sort_keys=True, separators=(',', ':'), allow_nan=False)
        return hashlib.sha256(canonical.encode()).hexdigest()

    def check_host(self, memory_total, memory_available, cpu_count):
        if memory_total is None or memory_available is None or cpu_count is None:
            raise ValueError('Host memory, available headroom and CPU evidence are required')
        committed = self.web.memory_bytes + self.scheduler.memory_bytes + self.other_services_reserved_bytes + self.host_headroom_bytes
        if committed > memory_total:
            raise ValueError(f'Dual container commitments exceed host capacity by {committed - memory_total} bytes')
        if self.web.memory_bytes + self.scheduler.memory_bytes + self.host_headroom_bytes > memory_available:
            raise ValueError('Available host memory cannot cover isolated commitments and headroom')
        if self.web.cpu_cpus + self.scheduler.cpu_cpus > cpu_count:
            raise ValueError('Dual container CPU commitments exceed effective host capacity')
        return committed


def load_profile(path):
    raw = Path(path).read_bytes()
    if len(raw) > 64 * 1024:
        raise ValueError('Capacity profile exceeds 64 KiB')
    return CapacityProfile.model_validate_json(raw)


def assess_report(profile, report):
    """Never convert a calibration or incomplete run into acceptance."""
    failures = []
    if report.get('profile_sha256') != profile.fingerprint():
        failures.append('profile_changed_or_mismatched')
    if not report.get('image_id') or not report.get('source_revision'):
        failures.append('missing_image_or_source_identity')
    if report.get('mode') not in {'calibration', 'acceptance'}:
        failures.append('invalid_mode')
    for role in ('web', 'scheduler'):
        item = report.get('containers', {}).get(role, {})
        budget = getattr(profile, role)
        limits = item.get('effective_limits', {})
        if (limits.get('memory_bytes') != budget.memory_bytes
                or limits.get('memory_swap_total_bytes') != budget.memory_swap_total_bytes
                or limits.get('cpu_cpus') != budget.cpu_cpus):
            failures.append(f'{role}:effective_limits_mismatch')
        if set(item.get('phases', {})) != set(PHASES):
            failures.append(f'{role}:missing_phases')
        for phase in PHASES:
            evidence = item.get('phases', {}).get(phase, {})
            if evidence.get('elapsed_seconds', 0) < profile.phase_seconds[phase]:
                failures.append(f'{role}:{phase}:duration_short')
            if evidence.get('sample_count', 0) < 2:
                failures.append(f'{role}:{phase}:missing_series')
            if not evidence.get('workload_completed'):
                failures.append(f'{role}:{phase}:workload_incomplete')
            if phase in {'cold', 'typical', 'concurrent_refresh'} and (
                    evidence.get('accepted_requests', 0) < 1 or evidence.get('catalogue', {}).get('bytes', 0) < 1):
                failures.append(f'{role}:{phase}:catalogue_never_loaded')
            if phase in {'concurrent_refresh', 'slow_dependencies'} and evidence.get('critical_receipts_updated', 0) < 1:
                failures.append(f'{role}:{phase}:no_critical_receipt_progress')
            peak = evidence.get('memory_peak_bytes')
            rss = evidence.get('rss_peak_bytes')
            if peak is None or peak > budget.memory_peak_upper_bytes or rss is None or rss < budget.measured_rss_lower_bytes:
                failures.append(f'{role}:{phase}:memory_bounds')
        if not item.get('aggregate_limits_held') or not item.get('critical_progress'):
            failures.append(f'{role}:admission_or_critical_progress')
        growth = item.get('retirement_growth_bytes')
        if growth is None or growth > profile.retirement_growth_upper_bytes or not item.get('retired_objects_reclaimed'):
            failures.append(f'{role}:retirement_not_stable')
        steady_growth = item.get('retirement_steady_growth_bytes')
        if steady_growth is None or steady_growth > profile.retirement_steady_growth_upper_bytes or item.get('retired_keys_seen', 0) < profile.retired_keys:
            failures.append(f'{role}:retirement_plateau_or_key_coverage')
        if not item.get('restart_continuity') or item.get('financial_writes_replayed') != 0 or item.get('unattempted_tail_executed') != 0:
            failures.append(f'{role}:financial_continuity')
        if item.get('oom_kill_delta') != 0:
            failures.append(f'{role}:oom_evidence_missing_or_increased')
    web = report.get('web_latency', {})
    if (web.get('samples', 0) < 20 or web.get('errors') != 0
            or web.get('p95_ms', float('inf')) > profile.web_p95_ms
            or web.get('max_ms', float('inf')) > profile.web_max_ms):
        failures.append('web_slo')
    if report.get('host_min_available_bytes', -1) < profile.host_headroom_bytes:
        failures.append('host_headroom')
    if report.get('host_oom_kill_delta') != 0:
        failures.append('host_oom_counter_missing_or_increased')
    return {'verdict': 'failed' if failures else 'calibrated' if report.get('mode') == 'calibration' else 'accepted',
            'failures': failures, 'production_verified': False}
