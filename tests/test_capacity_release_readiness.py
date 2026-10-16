"""A healthy app can precede its first resource collector observation."""
import importlib.util
from pathlib import Path
import pytest


def load_probe():
    source = Path(__file__).parents[1] / 'scripts/verify-runtime-capacity.py'
    spec = importlib.util.spec_from_file_location('capacity_release_readiness', source)
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    return probe


def test_release_waits_for_a_real_resource_observation(monkeypatch):
    probe = load_probe()
    observed = {'resources': {'resources': {'memory': {'events': {'oom_kill': 0}}}}}
    replies = iter([{'success': True}, {'resources': {}}, {'success': True}, observed])
    calls, elapsed = [], [0.0]

    def request(url):
        calls.append(url)
        return next(replies)

    monkeypatch.setattr(probe, 'request', request)
    monkeypatch.setattr(probe.time, 'monotonic', lambda: elapsed[0])
    monkeypatch.setattr(probe.time, 'sleep', lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds))
    assert probe.wait_ready('http://offline', seconds=2) is observed
    assert calls == ['http://offline/health', 'http://offline/verification/result'] * 2
    assert elapsed[0] == .5


@pytest.mark.parametrize('memory_enabled,limit,active,byte_peak,expected', [
    (False, None, 2, 200, True), (False, None, 7, 200, False),
    (False, 100, 2, 50, False), (None, None, 2, 50, False),
    (True, None, 2, 50, False), (True, 100, 2, 200, False),
    (True, 100, 2, 50, True),
])
def test_explicit_byte_observation_does_not_remove_concurrency_checks(memory_enabled, limit, active, byte_peak, expected):
    item = {'capacity': {'settings': {'total_workers': 6, 'queue_limit': 16, 'byte_limit': limit},
                         'peaks': {'active': active, 'queued': 0, 'bytes': byte_peak}}}
    if memory_enabled is not None:
        item['capacity']['settings']['memory_limits_enabled'] = memory_enabled
    assert load_probe().aggregate_limits_held([item]) is expected
