from types import SimpleNamespace
import json

import pytest

from backend.utils.host_resource_evidence import kernel_tail, inspect_container, host_observation


def test_log_tail_has_bounded_bytes_and_keeps_source_time(tmp_path):
    path = tmp_path / 'kern.log'
    path.write_text('old global_oom\n' + 'x' * 10000 + '\nOct 13 21:02:11 global_oom Killed process python\n')
    result = kernel_tail(path, byte_limit=256)
    assert result['lines'] == ['Oct 13 21:02:11 global_oom Killed process python']
    assert result['tail_start_byte'] > 0
    assert 'no year or timezone inferred' in result['clock']
    assert kernel_tail(tmp_path / 'missing')['availability'] == 'unavailable'


def test_docker_read_fields_no_environment_or_error_leak():
    captured = []

    def runner(args, **kwargs):
        captured.append(args)
        assert kwargs['timeout'] == 5
        return SimpleNamespace(returncode=0, stdout='|'.join(json.dumps(value) for value in [
            'old-id', 'image-id', '2026-10-13T00:00:00Z',
            {'Status': 'exited', 'OOMKilled': False, 'Error': 'secret-error'},
            1024, 2048, 500000000, 'commit']))

    result = inspect_container('web', runner=runner)
    assert result['container_id'] == 'old-id'
    assert result['state']['OOMKilled'] is False
    assert result['intended_limits']['cpu_cpus'] == .5
    assert 'secret' not in json.dumps(result)
    assert '.Config.Env' not in captured[0][3]
    with pytest.raises(ValueError):
        inspect_container('--all', runner=runner)


def test_unavailable_inspect_not_an_oom_negative():
    assert inspect_container('deleted', runner=lambda *a, **k:
        SimpleNamespace(returncode=1, stdout='')) == {'name': 'deleted', 'availability': 'unavailable'}


def test_old_oom_log_does_not_map_to_restarted_container(tmp_path):
    path = tmp_path / 'kern.log'
    path.write_text('Oct 13 21:02:11 global_oom\n')
    result = host_observation(['web'], kernel_path=path,
        reader=lambda: {'host': {'swap_free_bytes': 0, 'swap_total_bytes': 1}},
        inspector=lambda name: {'name': name, 'container_id': 'new-id', 'state': {'OOMKilled': False}})
    assert result['classification_of_available_evidence']['classification'] == 'global_oom_confirmed'
    assert result['classification_of_available_evidence']['attribution'] == 'container_unmapped'
    assert 'historical events are not attributed' in result['scope']
