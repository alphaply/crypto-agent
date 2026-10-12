"""Explicit, bounded provider probe. Does not save credentials or raw responses."""
import argparse
import json
import os

import httpx


def probe(*, base_url: str, model: str, api_key: str, client=None, limit: int = 6) -> list[dict]:
    if not 1 <= limit <= 6:
        raise ValueError('Probe limit must be between 1 and 6 requests')
    jobs = [('openai', 'low'), ('openai', 'high'), ('anthropic', 'low'), ('anthropic', 'high'),
            ('openai', 'high'), ('anthropic', 'high')][:limit]
    own_client = client is None
    client = client or httpx.Client(timeout=45, transport=httpx.HTTPTransport(retries=0))
    results = []
    try:
        for protocol, effort in jobs:
            payload = {'model': model, 'max_tokens': 2048,
                       'messages': [{'role': 'user', 'content': 'Compute 17*23+19*31. Give only the final number.'}]}
            headers = {'User-Agent': 'crypto-agent/0.1.0'}
            if protocol == 'anthropic':
                payload.update(thinking={'type': 'adaptive'}, output_config={'effort': effort})
                headers.update({'x-api-key': api_key, 'anthropic-version': '2023-06-01'})
                path = '/messages'
            else:
                payload['reasoning_effort'] = effort
                headers['Authorization'] = 'Bearer ' + api_key
                path = '/chat/completions'
            try:
                response = client.post(base_url.rstrip('/') + path, json=payload, headers=headers, timeout=45)
                value = response.json() if response.status_code == 200 else {}
                usage = value.get('usage') or {}
                details = usage.get('completion_tokens_details') or usage.get('output_tokens_details') or {}
                tokens = details.get('reasoning_tokens', details.get('thinking_tokens', 0))
                blocks = value.get('content') or []
                text = any(isinstance(b, dict) and b.get('thinking') for b in blocks)
                if protocol == 'openai':
                    msg = ((value.get('choices') or [{}])[0].get('message') or {})
                    text = bool(msg.get('reasoning_content') or msg.get('reasoning'))
                results.append({'protocol': protocol, 'effort_sent': effort, 'http_status': response.status_code,
                                'text_available': bool(text), 'reported_reasoning_tokens': tokens,
                                'effort_compliance': 'unverified'})
            except (httpx.HTTPError, ValueError):
                results.append({'protocol': protocol, 'effort_sent': effort, 'request_outcome': 'error'})
    finally:
        if own_client:
            client.close()
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-online', action='store_true', required=True, help='Explicitly allow up to six paid model requests')
    parser.add_argument('--base-url', default='https://api.bankofai.io/v1')
    parser.add_argument('--model', default='claude-sonnet-5.5')
    parser.add_argument('--limit', type=int, default=4, choices=range(1, 7))
    args = parser.parse_args()
    key = os.environ.get('BAI_API_KEY')
    if not key:
        parser.error('Set BAI_API_KEY in the process environment; never pass it as a command argument')
    print(json.dumps(probe(base_url=args.base_url, model=args.model, api_key=key, limit=args.limit), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
