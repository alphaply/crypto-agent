"""Allowlisted evidence about requests, never hidden thoughts or raw payloads."""
from contextvars import ContextVar
from urllib.parse import urlparse

current_request: ContextVar[dict | None] = ContextVar('reasoning_request_diagnostics', default=None)


def capture_request(payload: dict, *, protocol: str, base_url: str | None, requested: dict | None = None) -> dict:
    requested = requested or {}
    # The SDK overlays extra_body on the final wire JSON.
    actual = {**payload, **(payload.get('extra_body') or {})}
    sent = {}
    for key in ('reasoning_effort', 'temperature', 'top_p', 'max_tokens', 'max_completion_tokens', 'max_output_tokens'):
        value = actual.get(key)
        if isinstance(value, (str, int, float, bool)):
            if key != 'reasoning_effort' or value in {'none', 'low', 'medium', 'high', 'xhigh', 'max'}:
                sent[key] = value
    for key, allowed in [('thinking', {'type', 'budget_tokens', 'display'}), ('output_config', {'effort'})]:
        value = actual.get(key)
        if isinstance(value, dict):
            sent[key] = {name: item for name, item in value.items() if name in allowed and isinstance(item, (str, int, bool))}
    provider = urlparse(str(base_url or '')).hostname or ('anthropic' if protocol == 'anthropic' else 'openai')
    diagnostic = {'provider': provider, 'model': str(actual.get('model') or ''), 'protocol': protocol,
                  'requested_effort': requested.get('effort'), 'thinking_enabled': requested.get('thinking_enabled'),
                  'sent_parameters': sent, 'request_outcome': 'sent', 'capability': 'unverified'}
    if 'claude-sonnet-5.5' in diagnostic['model'].lower() and ('temperature' in sent or 'top_p' in sent):
        diagnostic['configuration_warning'] = '此模型的采样参数支持情况未获网关确认。'
    current_request.set(diagnostic)
    return diagnostic


def response_diagnostics(message, request: dict | None = None) -> dict:
    from backend.utils.llm_utils import extract_reasoning_content, extract_reasoning_token_count
    result = dict(request or current_request.get() or {})
    text_available = bool(extract_reasoning_content(message).strip())
    tokens = extract_reasoning_token_count(message)
    sent = result.get('sent_parameters') or {}
    disabled = sent.get('reasoning_effort') == 'none' or (sent.get('thinking') or {}).get('type') == 'disabled'
    requested = bool(result.get('requested_effort') or sent.get('reasoning_effort') or sent.get('thinking') or result.get('thinking_enabled'))
    state = 'text_available' if text_available else 'usage_only' if tokens > 0 else 'disabled' if disabled else 'requested_no_evidence' if requested else 'not_requested'
    result.update(request_outcome='accepted', text_available=text_available, reported_reasoning_tokens=tokens, evidence_state=state)
    return result


def attach_diagnostics(message):
    raw = (getattr(message, 'additional_kwargs', {}) or {}).get('reasoning_diagnostics')
    if raw or current_request.get():
        message.additional_kwargs['reasoning_diagnostics'] = response_diagnostics(message, raw)
    return message


def extract_diagnostics(message) -> dict:
    value = (getattr(message, 'additional_kwargs', {}) or {}).get('reasoning_diagnostics')
    return dict(value) if isinstance(value, dict) else {}
