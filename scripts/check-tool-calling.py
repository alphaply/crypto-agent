"""Probe tool transport with an inert function; never run a trading tool."""
import argparse
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from langchain_core.messages import HumanMessage

from backend.utils.llm_utils import build_chat_model, extract_message_text


PROBE_TOOL = {
    'type': 'function',
    'function': {
        'name': 'diagnostic_echo',
        'description': 'A harmless connectivity check. No trading or external action is performed.',
        'parameters': {'type': 'object', 'properties': {'marker': {'type': 'string'}},
                       'required': ['marker']},
    },
}


def probe(settings: dict, *, streaming: bool, choice: str) -> dict:
    started = time.monotonic()
    result = {'streaming': streaming, 'tool_choice': choice}
    try:
        model = build_chat_model(
            model=settings['model'], api_key=settings['api_key'], base_url=settings['api_base'],
            compatibility_mode=settings.get('compatibility_mode', 'auto'),
            extra_body=settings.get('extra_body'), temperature=settings.get('temperature'),
            thinking_enabled=settings.get('thinking_enabled'), reasoning_effort=settings.get('reasoning_effort'),
            streaming=streaming, timeout_seconds=90,
        ).bind_tools([PROBE_TOOL], tool_choice=choice)
        response = model.invoke([HumanMessage(content=(
            'Call diagnostic_echo with marker=argon_probe. Use a function call, not JSON in text. '
            'This is a connectivity check; no real action will run.'
        ))])
        calls = response.tool_calls or []
        valid = len(calls) == 1 and calls[0]['name'] == 'diagnostic_echo' and calls[0]['args'] == {'marker': 'argon_probe'}
        result.update(outcome='tool_call_returned' if valid else 'no_valid_probe_call',
                      tool_calls=len(calls), invalid_tool_calls=len(response.invalid_tool_calls or []),
                      text_characters=len(extract_message_text(response)),
                      finish_reason=response.response_metadata.get('finish_reason')
                      or response.response_metadata.get('stop_reason'))
    except Exception as exc:
        # Do not print raw provider errors: gateways can echo headers or payloads.
        result.update(outcome='request_failed', error_type=type(exc).__name__,
                      http_status=getattr(exc, 'status_code', None))
    result['seconds'] = round(time.monotonic() - started, 2)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--provider-id', help='Use this saved provider, without changing it')
    parser.add_argument('--model', help='Explicit model ID (required without --provider-id)')
    parser.add_argument('--base-url', help='Explicit API base URL (required without --provider-id)')
    parser.add_argument('--api-key-env', default='BAI_API_KEY', help='Environment variable containing the key')
    args = parser.parse_args()
    if args.provider_id:
        from backend.config_store import load_effective_runtime_snapshot
        settings = next((dict(p) for p in load_effective_runtime_snapshot().get('llm_providers', [])
                         if p['provider_id'] == args.provider_id), None)
        if not settings:
            parser.error('Saved provider was not found.')
        if args.model:
            settings['model'] = args.model
        if args.base_url:
            settings['api_base'] = args.base_url
    else:
        if not args.model or not args.base_url:
            parser.error('--model and --base-url are required without --provider-id.')
        settings = {'model': args.model, 'api_base': args.base_url, 'api_key': os.getenv(args.api_key_env)}
    if not settings.get('api_key'):
        parser.error('API key unavailable. Set the named environment variable; do not pass keys as arguments.')
    failed = False
    for streaming, choice in [(True, 'auto'), (False, 'auto'), (True, 'required'), (False, 'required')]:
        result = probe(settings, streaming=streaming, choice=choice)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        failed |= result['outcome'] != 'tool_call_returned'
        if result['outcome'] == 'request_failed':
            # An unavailable service/credential cannot establish tool capability.
            break
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
