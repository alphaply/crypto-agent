from copy import deepcopy

import pytest
from pydantic import ValidationError

from backend.utils.capacity_profile import PHASES, CapacityProfile, assess_report, load_profile


def test_postgres_commitment_includes_database_and_host_headroom():
    profile = load_profile('scripts/capacity/postgresql-local-v1.json')
    applications = profile.web.memory_bytes + profile.scheduler.memory_bytes
    total = applications + profile.db.memory_bytes + profile.other_services_reserved_bytes + profile.host_headroom_bytes
    assert profile.check_host(total, total, 3) == total
    with pytest.raises(ValueError, match='exceed host capacity'):
        profile.check_host(total - 1, total, 3)
    with pytest.raises(ValueError, match='Available host memory'):
        profile.check_host(total, applications + profile.host_headroom_bytes, 3)
    with pytest.raises(ValueError, match='CPU'):
        profile.check_host(total, total, 2)


def test_postgres_missing_or_excessive_evidence_cannot_pass():
    profile = load_profile('scripts/capacity/postgresql-local-v1.json')
    report = {'database': {'image_reference': profile.db.image,
        'effective_limits': {'memory_bytes': profile.db.memory_bytes,
            'memory_swap_total_bytes': profile.db.memory_swap_total_bytes, 'cpu_cpus': profile.db.cpu_cpus},
        'oom_kill_delta': 0,
        'phases': {phase: {'sample_count': 2, 'memory_peak_bytes': 128 * 1024**2,
                         'connections_peak': 4} for phase in PHASES}}}
    assert not any(item.startswith('db:') for item in assess_report(profile, report)['failures'])
    altered = deepcopy(report)
    altered['database']['phases']['restart']['connections_peak'] = profile.db.connection_peak_upper + 1
    altered['database']['oom_kill_delta'] = None
    failures = assess_report(profile, altered)['failures']
    assert 'db:restart:connection_bounds' in failures
    assert 'db:oom_evidence_missing_or_increased' in failures
    altered = deepcopy(report)
    altered['database']['phases'].pop('idle')
    assert 'db:missing_phases' in assess_report(profile, altered)['failures']


def test_postgres_profile_requires_pinned_version_and_connection_headroom():
    raw = load_profile('scripts/capacity/postgresql-local-v1.json').model_dump()
    raw['db']['image'] = 'postgres:latest'
    with pytest.raises(ValidationError):
        CapacityProfile.model_validate(raw)
    raw = load_profile('scripts/capacity/postgresql-local-v1.json').model_dump()
    raw['db']['connection_peak_upper'] = raw['db']['max_connections']
    with pytest.raises(ValidationError, match='headroom'):
        CapacityProfile.model_validate(raw)
