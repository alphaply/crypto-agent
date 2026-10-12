import json

import httpx
import pytest

from backend.utils.probe_reasoning import probe


def test_optional_probe_bounds_and_no_secret_output(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    calls = []
    def handler(request):
        calls.append(request)
        if len(calls) == 2:
            raise httpx.ReadTimeout('synthetic timeout')
        return httpx.Response(500, json={'error': 'synthetic failure'})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    output = probe(base_url='https://example.invalid/v1', model='synthetic', api_key='offline-secret', client=client)
    assert len(calls) == len(output) == 6
    assert all(json.loads(request.content)['max_tokens'] == 2048 for request in calls)
    assert all(set(request.extensions['timeout'].values()) == {45} for request in calls)
    assert 'offline-secret' not in json.dumps(output)
    assert list(tmp_path.iterdir()) == []
    with pytest.raises(ValueError):
        probe(base_url='https://example.invalid/v1', model='synthetic', api_key='offline', client=client, limit=7)
    assert len(calls) == 6
