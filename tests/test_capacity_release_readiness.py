"""A healthy app can precede its first resource collector observation."""
import importlib.util
from pathlib import Path


def test_release_waits_for_a_real_resource_observation(monkeypatch):
    source = Path(__file__).parents[1] / 'scripts/verify-runtime-capacity.py'
    spec = importlib.util.spec_from_file_location('capacity_release_readiness', source)
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
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
