from copy import deepcopy

import pytest
from pydantic import ValidationError

from backend.utils.capacity_profile import PHASES, assess_report, load_profile, CapacityProfile


@pytest.fixture
def profile():
    return load_profile('scripts/capacity/local-calibration-v1.json')


def complete_report(profile, mode='acceptance'):
    report = {'mode': mode, 'profile_sha256': profile.fingerprint(),
        'image_id': 'sha256:fixture-image', 'source_revision': 'fixture-commit',
        'host_min_available_bytes': profile.host_headroom_bytes, 'host_oom_kill_delta': 0,
        'web_latency': {'samples': 100, 'errors': 0, 'p95_ms': 25, 'max_ms': 100},
        'containers': {}}
    for role in ('web', 'scheduler'):
        budget = getattr(profile, role)
        report['containers'][role] = {'effective_limits': {'memory_bytes': budget.memory_bytes,
            'memory_swap_total_bytes': budget.memory_swap_total_bytes, 'cpu_cpus': budget.cpu_cpus},
            'phases': {phase: {'elapsed_seconds': profile.phase_seconds[phase], 'sample_count': 4,
                'memory_peak_bytes': budget.measured_rss_lower_bytes + 1, 'rss_peak_bytes': budget.measured_rss_lower_bytes,
                'workload_completed': True, 'accepted_requests': 2, 'critical_receipts_updated':1, 'catalogue': {'bytes': 1024}} for phase in PHASES},
            'aggregate_limits_held': True, 'critical_progress': True, 'retirement_growth_bytes': 0,
            'retirement_steady_growth_bytes':0, 'retired_keys_seen':profile.retired_keys,
            'retired_objects_reclaimed': True, 'restart_continuity': True,
            'financial_writes_replayed': 0, 'unattempted_tail_executed': 0, 'oom_kill_delta': 0}
    return report


def test_predeclared_full_workload_required(profile):
    data = profile.model_dump()
    del data['phase_seconds']['concurrent_refresh']
    with pytest.raises(ValidationError):
        CapacityProfile.model_validate(data)
    data = profile.model_dump()
    data['web']['cpu_cpus'] = float('nan')
    with pytest.raises(ValidationError):
        CapacityProfile.model_validate(data)
    data = profile.model_dump()
    data['application']['API_KEY'] = 'never-in-profile'
    with pytest.raises(ValidationError):
        CapacityProfile.model_validate(data)


def test_host_budget_covers_both_containers_and_headroom(profile):
    total = profile.web.memory_bytes + profile.scheduler.memory_bytes + profile.other_services_reserved_bytes + profile.host_headroom_bytes
    assert profile.check_host(total, total, 2) == total
    with pytest.raises(ValueError, match='exceed host capacity'):
        profile.check_host(total - 1, total, 2)
    with pytest.raises(ValueError, match='Available host memory'):
        profile.check_host(total, profile.web.memory_bytes + profile.scheduler.memory_bytes, 2)
    with pytest.raises(ValueError, match='CPU'):
        profile.check_host(total, total, 1)
    with pytest.raises(ValueError, match='evidence'):
        profile.check_host(total, None, 2)


def test_calibration_is_never_accepted_and_modified_standard_invalidates_report(profile):
    report = complete_report(profile, 'calibration')
    assert assess_report(profile, report)['verdict'] == 'calibrated'
    assert assess_report(profile, complete_report(profile))['verdict'] == 'accepted'
    data = profile.model_dump()
    data['web_p95_ms'] += 1
    changed = CapacityProfile.model_validate(data)
    assert 'profile_changed_or_mismatched' in assess_report(changed, report)['failures']


@pytest.mark.parametrize('change,expected', [
    (lambda r: r['containers'].pop('scheduler'), 'scheduler:missing_phases'),
    (lambda r: r['containers']['web']['phases'].pop('concurrent_refresh'), 'web:missing_phases'),
    (lambda r: r['containers']['web']['phases']['idle'].update(elapsed_seconds=0), 'web:idle:duration_short'),
    (lambda r: r['containers']['web'].update(critical_progress=False), 'web:admission_or_critical_progress'),
    (lambda r: r['containers']['web'].update(financial_writes_replayed=1), 'web:financial_continuity'),
    (lambda r: r['containers']['web'].update(oom_kill_delta=None), 'web:oom_evidence_missing_or_increased'),
    (lambda r: r['containers']['web'].update(retired_objects_reclaimed=False), 'web:retirement_not_stable'),
    (lambda r: r['web_latency'].update(p95_ms=501), 'web_slo'),
    (lambda r: r.update(host_oom_kill_delta=None), 'host_oom_counter_missing_or_increased'),
])
def test_incomplete_or_failed_evidence_cannot_pass(profile, change, expected):
    report = complete_report(profile)
    change(report)
    result = assess_report(profile, report)
    assert result['verdict'] == 'failed'
    assert expected in result['failures']
