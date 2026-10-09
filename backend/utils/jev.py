"""Official TypeSafe System One and B.AI Decisions adapters for Jev scoring."""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
from decimal import Decimal
from urllib.parse import urlsplit

import httpx

from backend import database_news
from backend.app.schemas.news import DEFAULT_SCORER_INSTRUCTIONS, DEFAULT_SCORER_CRITERIA
from backend.utils.news_tracing import news_span, pipeline_run_id, trace_identity

RUBRIC_VERSION = 'crypto-market-relevance-v2'
CRITERIA = DEFAULT_SCORER_CRITERIA


class JevError(ValueError):
    def __init__(self, message, *, code='invalid_response', details=None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


def validate_provider(provider: dict) -> None:
    if provider.get('api_protocol') != 'decisions':
        raise JevError('News scoring requires a provider using the Decisions API protocol')
    if provider.get('decisions_api', 'bai') not in {'bai', 'typesafe'}:
        raise JevError('Choose the B.AI or official TypeSafe Decisions API')
    if not re.fullmatch(r'jev-[a-zA-Z0-9][a-zA-Z0-9._-]{0,100}', str(provider.get('model') or '')):
        raise JevError('News scoring requires a Jev model identifier (for example jev-1.13.0 or jev-latest)')
    if not provider.get('api_key'):
        raise JevError('The selected Jev provider has no API key')


def api_base(provider: dict) -> str:
    default = 'https://api.typesafe.ai/v1' if provider.get('decisions_api') == 'typesafe' else 'https://api.b.ai/v1'
    base = str(provider.get('api_base') or default).rstrip('/')
    parsed = urlsplit(base)
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise JevError('Jev API Base must be HTTP(S), without credentials, query, or fragment', code='configuration')
    return base if base.endswith('/v1') else base + '/v1'


def list_models(provider: dict) -> list[dict]:
    validate_provider(provider)
    try:
        response = httpx.get(api_base(provider) + '/models', headers={'Authorization': f"Bearer {provider['api_key']}", 'User-Agent': 'crypto-agent/0.2'}, timeout=15)
        if response.status_code != 200:
            raise JevError(f'Jev model discovery failed (HTTP {response.status_code})', code='http_error')
        if len(response.content) > 1024 * 1024:
            raise JevError('Jev model listing is too large')
        payload = response.json()
        values = payload.get('models', payload.get('data'))
        if not isinstance(values, list):
            raise JevError('Jev returned an invalid model listing')
        result = []
        for row in values:
            if not isinstance(row, dict):
                continue
            model = row.get('name') or row.get('id')
            if isinstance(model, str) and re.fullmatch(r'jev-[a-zA-Z0-9][a-zA-Z0-9._-]{0,100}', model):
                result.append({'id': model, 'name': model, 'description': str(row.get('description') or '')[:500]})
        return result
    except JevError:
        raise
    except Exception as exc:
        raise JevError('Jev model discovery request failed', code='connection_error') from exc


def build_request(item: dict, model: str, *, instructions: str = DEFAULT_SCORER_INSTRUCTIONS, criteria: list[str] | None = None) -> dict:
    return {'model': model, 'state': {'title': str(item.get('title', ''))[:1000], 'content': str(item.get('content') or item.get('description') or '')[:12000], 'source': str(item.get('source', ''))[:150]},
            'questions': {'market_relevance': {'type': 'score', 'instructions': instructions, 'criteria': criteria or CRITERIA}}}


def _precision_bounds(score, probabilities):
    """Bound serialization rounding, subject to probabilities summing to one.

    TypeSafe defines score as the weighted mean. Its JSON decimal score and
    probabilities may have different precision; a fixed epsilon rejects valid
    rounded scores. Use the actual decimal places, never repair the response.
    Integer literals are exact; each decimal uses its own serialized precision.
    """
    decimals = [Decimal(str(value)) for value in probabilities.values()]
    halves = [Decimal(5).scaleb(value.as_tuple().exponent - 1) if value.as_tuple().exponent < 0 else Decimal(0) for value in decimals]
    lows = [max(Decimal(0), value - half) for value, half in zip(decimals, halves)]
    highs = [min(Decimal(1), value + half) for value, half in zip(decimals, halves)]
    def endpoint(reverse):
        values = list(lows)
        remaining = Decimal(1) - sum(values)
        for index in sorted(range(len(values)), reverse=reverse):
            added = min(max(remaining, Decimal(0)), highs[index] - values[index])
            values[index] += added
            remaining -= added
        return sum(Decimal(index) * value for index, value in enumerate(values))
    raw = Decimal(str(score))
    exponent = raw.as_tuple().exponent
    score_half = Decimal(5).scaleb(exponent - 1) if exponent < 0 else Decimal(0)
    return float(endpoint(False) - score_half), float(endpoint(True) + score_half)


def parse_response(payload: dict, level_count: int = 5) -> dict:
    if not 2 <= level_count <= 10:
        raise JevError('Jev score requires 2–10 rubric levels')
    if not isinstance(payload, dict) or not isinstance(payload.get('model'), str) or not payload['model']:
        raise JevError('Jev returned an invalid model response')
    answers = payload.get('answers')
    if not isinstance(answers, dict) or set(answers) != {'market_relevance'}:
        raise JevError('Jev response does not match the requested question')
    answer = answers['market_relevance']
    if not isinstance(answer, dict) or answer.get('type') != 'score':
        raise JevError('Jev did not return a Score answer')
    def number(value, maximum):
        return isinstance(value, (int, float, Decimal)) and not isinstance(value, bool) and math.isfinite(value) and 0 <= value <= maximum
    if not number(answer.get('score'), level_count - 1) or not number(answer.get('confidence'), 1):
        raise JevError('Jev score or confidence is outside its allowed range')
    probabilities = answer.get('probabilities')
    if not isinstance(probabilities, dict) or set(probabilities) != {str(i) for i in range(level_count)} or not all(number(p, 1) for p in probabilities.values()) or abs(sum(probabilities.values()) - 1) > 0.0001:
        raise JevError('Jev returned an invalid probability distribution')
    ordered = {str(i): probabilities[str(i)] for i in range(level_count)}
    weighted = sum(int(i) * p for i, p in ordered.items())
    lower, upper = _precision_bounds(answer['score'], ordered)
    if not lower - 1e-9 <= answer['score'] <= upper + 1e-9:
        raise JevError('Jev score does not match its probability distribution at the returned precision',
                       code='score_distribution_mismatch',
                       details={'raw_score': float(answer['score']), 'weighted_score': float(weighted),
                                'score_lower_bound': lower, 'score_upper_bound': upper})
    legend = answer.get('legend')
    if not isinstance(legend, dict) or set(legend) != set(probabilities) or not all(isinstance(v, str) and v for v in legend.values()):
        raise JevError('Jev returned an invalid score legend')
    usage = payload.get('usage')
    if not isinstance(usage, dict) or any(not isinstance(usage.get(k), int) or isinstance(usage.get(k), bool) or usage[k] < 0 for k in ('input_tokens', 'output_tokens')) or sum(usage[k] for k in ('input_tokens', 'output_tokens')) > 2147483647:
        raise JevError('Jev returned invalid usage')
    return {'score': round(float(answer['score']) * 100 / (level_count - 1), 4),
            'raw_score': float(answer['score']), 'level_count': level_count,
            'confidence': float(answer['confidence']), 'probabilities': {key: float(value) for key, value in probabilities.items()},
            'legend': legend, 'model': payload['model'], 'rubric_version': RUBRIC_VERSION,
            'weighted_score': float(weighted), 'score_rounding_delta': float(answer['score']) - float(weighted)}


def score_news(item: dict, provider: dict, *, instructions: str = DEFAULT_SCORER_INSTRUCTIONS, criteria: list[str] | None = None) -> dict:
    from backend.database_agent_runs import start_agent_run, finish_agent_run
    validate_provider(provider)
    body = build_request(item, provider['model'], instructions=instructions, criteria=criteria)
    base = api_base(provider)
    adapter = provider.get('decisions_api', 'bai')
    fingerprint = hashlib.sha256(json.dumps({'request': body, 'provider_id': provider.get('provider_id'), 'api_base': base, 'adapter': adapter, 'rubric': RUBRIC_VERSION}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    cached = database_news.get_score(fingerprint)
    if cached:
        return {**cached, 'cached': True}
    endpoint = base + ('/systemone' if adapter == 'typesafe' else '/decisions')
    for attempt in range(2):
        run_id = start_agent_run('news-intelligence', 'news_score', provider['model'], [{'role': 'user', 'content': json.dumps(body, ensure_ascii=False)}], provider_id=provider.get('provider_id'), parent_run_id=pipeline_run_id.get())
        payload = {}
        trace_info = {'trace_id': None, 'trace_url': None, 'trace_status': 'disabled'}
        details = {'attempt': attempt + 1, 'decisions_api': adapter, 'pipeline_run_id': pipeline_run_id.get()}
        try:
            with news_span('jev.score', run_type='llm', inputs=body,
                           metadata={'provider_id': provider.get('provider_id'), 'model': provider['model'], **details}) as span:
                trace_info = trace_identity(span)
                response = httpx.post(endpoint, headers={'Authorization': f"Bearer {provider['api_key']}", 'User-Agent': 'crypto-agent/0.2'}, json=body, timeout=15)
                details['http_status'] = response.status_code
                if response.status_code == 429 or response.status_code >= 500:
                    raise httpx.HTTPStatusError('Jev is temporarily unavailable', request=response.request, response=response)
                if response.status_code >= 400:
                    raise JevError(f'Jev request failed (HTTP {response.status_code}); check credentials, quota, and model', code='http_error')
                if len(response.content) > 1024 * 1024:
                    raise JevError('Jev response is too large')
                # Preserve trailing decimal places while checking serialization
                # precision (2.10 and 2.1 do not imply the same rounding bound).
                payload = response.json(parse_float=Decimal)
                if span and isinstance(payload, dict):
                    span.end(outputs=json.loads(json.dumps({'answers': payload.get('answers'), 'usage_metadata': payload.get('usage')}, default=float).replace(provider['api_key'], '[redacted]')))
                result = parse_response(payload, len(body['questions']['market_relevance']['criteria']))
            result.update(run_id=run_id, attempts=attempt + 1, **trace_info)
            finish_agent_run(run_id, status='success', output=json.dumps(payload.get('answers'), ensure_ascii=False, default=float).replace(provider['api_key'], '[redacted]'), usage=payload['usage'], details={**details, **trace_info})
            database_news.save_score(fingerprint, result)
            return {**result, 'cached': False}
        except Exception as exc:
            safe_error = str(exc) if isinstance(exc, JevError) else 'Jev scoring request failed'
            code = exc.code if isinstance(exc, JevError) else ('timeout' if isinstance(exc, httpx.TimeoutException) else 'connection_error' if isinstance(exc, httpx.NetworkError) else 'http_error' if isinstance(exc, httpx.HTTPStatusError) else 'invalid_response')
            diagnostics = {**details, **trace_info, 'error_code': code,
                           **(exc.details if isinstance(exc, JevError) else {})}
            # Keep rejected answers and usage for diagnosis. Never store raw HTTP
            # errors or headers, which may echo provider credentials.
            output = json.dumps(payload.get('answers'), ensure_ascii=False, default=float) if isinstance(payload, dict) and payload.get('answers') is not None else ''
            finish_agent_run(run_id, status='error', error=safe_error, output=output.replace(provider['api_key'], '[redacted]'), usage=payload.get('usage') if isinstance(payload, dict) else {}, details=diagnostics)
            if attempt == 0 and isinstance(exc, (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError)):
                time.sleep(1)
                continue
            raise JevError(safe_error, code=code, details={**diagnostics, 'run_id': run_id, 'attempts': attempt + 1}) from exc
    raise JevError('Jev scoring failed')
