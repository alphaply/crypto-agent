"""B.AI TypeSafe Decisions adapter. Jev produces scores, never prose summaries."""
from __future__ import annotations

import hashlib
import json
import math
import re
import time

import httpx

from backend import database_news
from backend.app.schemas.news import DEFAULT_SCORER_INSTRUCTIONS, DEFAULT_SCORER_CRITERIA

RUBRIC_VERSION = 'crypto-market-relevance-v1'
CRITERIA = DEFAULT_SCORER_CRITERIA


class JevError(ValueError):
    pass


def validate_provider(provider: dict) -> None:
    if provider.get('api_protocol') != 'decisions':
        raise JevError('News scoring requires a provider using the Decisions API protocol')
    if not re.fullmatch(r'jev-[a-zA-Z0-9][a-zA-Z0-9._-]{0,100}', str(provider.get('model') or '')):
        raise JevError('News scoring requires a Jev model identifier (for example jev-1.13.0 or jev-latest)')
    if not provider.get('api_key'):
        raise JevError('The selected Jev provider has no API key')


def build_request(item: dict, model: str, *, instructions: str = DEFAULT_SCORER_INSTRUCTIONS, criteria: list[str] | None = None) -> dict:
    return {'model': model, 'state': {'title': str(item.get('title', ''))[:1000], 'content': str(item.get('content') or item.get('description') or '')[:12000], 'source': str(item.get('source', ''))[:150]},
            'questions': {'market_relevance': {'type': 'score', 'instructions': instructions, 'criteria': criteria or CRITERIA}}}


def parse_response(payload: dict, level_count: int = 5) -> dict:
    if not isinstance(payload, dict) or not isinstance(payload.get('model'), str) or not payload['model']:
        raise JevError('Jev returned an invalid model response')
    answers = payload.get('answers')
    if not isinstance(answers, dict) or set(answers) != {'market_relevance'}:
        raise JevError('Jev response does not match the requested question')
    answer = answers['market_relevance']
    if not isinstance(answer, dict) or answer.get('type') != 'score':
        raise JevError('Jev did not return a Score answer')
    def number(value, maximum):
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and 0 <= value <= maximum
    if not number(answer.get('score'), level_count - 1) or not number(answer.get('confidence'), 1):
        raise JevError('Jev score or confidence is outside its allowed range')
    probabilities = answer.get('probabilities')
    if not isinstance(probabilities, dict) or set(probabilities) != {str(i) for i in range(level_count)} or not all(number(p, 1) for p in probabilities.values()) or abs(sum(probabilities.values()) - 1) > 0.0001:
        raise JevError('Jev returned an invalid probability distribution')
    if abs(sum(int(i) * p for i, p in probabilities.items()) - answer['score']) > 0.001:
        raise JevError('Jev score does not match its probability distribution')
    legend = answer.get('legend')
    if not isinstance(legend, dict) or set(legend) != set(probabilities) or not all(isinstance(v, str) and v for v in legend.values()):
        raise JevError('Jev returned an invalid score legend')
    usage = payload.get('usage')
    if not isinstance(usage, dict) or any(not isinstance(usage.get(k), int) or isinstance(usage.get(k), bool) or usage[k] < 0 for k in ('input_tokens', 'output_tokens')):
        raise JevError('Jev returned invalid usage')
    return {'score': round(answer['score'] * 100 / (level_count - 1), 4),
            'raw_score': answer['score'], 'level_count': level_count,
            'confidence': answer['confidence'], 'probabilities': probabilities,
            'legend': legend, 'model': payload['model'], 'rubric_version': RUBRIC_VERSION}


def score_news(item: dict, provider: dict, *, instructions: str = DEFAULT_SCORER_INSTRUCTIONS, criteria: list[str] | None = None) -> dict:
    from backend.database_agent_runs import start_agent_run, finish_agent_run
    validate_provider(provider)
    body = build_request(item, provider['model'], instructions=instructions, criteria=criteria)
    fingerprint = hashlib.sha256(json.dumps({'request': body, 'provider_id': provider.get('provider_id'), 'api_base': provider.get('api_base'), 'rubric': RUBRIC_VERSION}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    cached = database_news.get_score(fingerprint)
    if cached:
        return {**cached, 'cached': True}
    base = str(provider.get('api_base') or 'https://api.b.ai/v1').rstrip('/')
    if not base.endswith('/v1'):
        base += '/v1'
    for attempt in range(2):
        run_id = start_agent_run('news-intelligence', 'news_score', provider['model'], [{'role': 'user', 'content': json.dumps(body, ensure_ascii=False)}], provider_id=provider.get('provider_id'))
        payload = {}
        try:
            response = httpx.post(base + '/decisions', headers={'Authorization': f"Bearer {provider['api_key']}", 'User-Agent': 'crypto-agent/0.2'}, json=body, timeout=15)
            if response.status_code == 429 or response.status_code >= 500:
                raise httpx.HTTPStatusError('Jev is temporarily unavailable', request=response.request, response=response)
            if response.status_code >= 400:
                raise JevError(f'Jev request failed (HTTP {response.status_code}); check credentials, quota, and model')
            if len(response.content) > 1024 * 1024:
                raise JevError('Jev response is too large')
            payload = response.json()
            result = parse_response(payload, len(body['questions']['market_relevance']['criteria']))
            finish_agent_run(run_id, status='success', output=json.dumps(payload.get('answers'), ensure_ascii=False), usage=payload['usage'])
            database_news.save_score(fingerprint, result)
            return {**result, 'cached': False}
        except Exception as exc:
            safe_error = str(exc) if isinstance(exc, JevError) else 'Jev scoring request failed'
            finish_agent_run(run_id, status='error', error=safe_error, usage=payload.get('usage') if isinstance(payload, dict) else {})
            if attempt == 0 and isinstance(exc, (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError)):
                time.sleep(1)
                continue
            raise JevError(safe_error) from exc
    raise JevError('Jev scoring failed')
