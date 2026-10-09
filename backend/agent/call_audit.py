"""Best-effort inspection of model calls with optional response validation."""
from typing import Any, Callable
from contextlib import contextmanager
import sys

from backend.utils.llm_utils import extract_message_text, extract_usage, extract_reasoning_content
from backend.utils.logger import setup_logger

logger = setup_logger('AgentCallAudit')


def _response_audit_fields(response) -> dict:
    if response is None:
        return {}
    usage = extract_usage(response)
    from backend.database_agent_runs import _json_value
    extra = getattr(response, 'additional_kwargs', {}) or {}
    return _json_value({
        'output': extract_message_text(response),
        'usage': usage,
        'details': {
            'tool_calls': getattr(response, 'tool_calls', []),
            'usage': usage,
            'response_metadata': getattr(response, 'response_metadata', {}),
            'completion_status': {key: extra[key] for key in
                                  ('finish_reason', 'stop_reason', 'status', 'incomplete_details', 'refusal') if key in extra},
            'reasoning_content': extract_reasoning_content(response),
        },
    })


def request_diagnostics(settings: dict) -> dict:
    from backend.database_agent_runs import _json_value
    keys = ('max_tokens', 'max_completion_tokens', 'max_output_tokens', 'thinking', 'thinking_enabled', 'reasoning_effort')
    return _json_value({key: value for source in (settings, settings.get('extra_body') or {})
                        for key, value in source.items() if key in keys and value is not None})


@contextmanager
def model_call_trace(purpose, *, config_id, model, messages, run_id=None, settings=None):
    """Include validation in the trace; tracing failures never replay model work."""
    from langsmith import trace
    from backend.database_agent_runs import _serialize_message
    context, run = None, None
    try:
        context = trace(purpose, run_type='chain', run_id=run_id,
                        inputs={'messages': [_serialize_message(msg) for msg in messages]},
                        metadata={'config_id': config_id, 'model': model,
                                  'purpose': purpose, 'request_settings': request_diagnostics(settings or {})})
        run = context.__enter__()
    except Exception as exc:
        context = None
        logger.warning('Cannot start %s trace: %s', purpose, exc)
    try:
        yield run
    except BaseException:
        if context:
            try:
                context.__exit__(*sys.exc_info())
            except Exception as exc:
                logger.warning('Cannot finish failed trace: %s', exc)
        raise
    else:
        if context:
            try:
                context.__exit__(None, None, None)
            except Exception as exc:
                logger.warning('Cannot finish trace: %s', exc)


def record_trace_response(run, response):
    if run is not None:
        try:
            run.end(outputs=_response_audit_fields(response))
        except Exception as exc:
            logger.warning('Cannot record trace response: %s', exc)


def audited_invoke(
    operation, *, config_id, purpose, model, messages, tools=None,
    response_validator: Callable[[Any], None] | None = None,
    provider_id: str | None = None,
    parent_run_id: str | None = None,
    request_settings: dict | None = None,
):
    """Validate inside the audit boundary while retaining rejected model output."""
    from backend.database_agent_runs import start_agent_run, finish_agent_run

    run_id = None
    try:
        run_id = start_agent_run(config_id or 'unknown', purpose, model or 'unknown', messages, tools, provider_id=provider_id, parent_run_id=parent_run_id)
    except Exception as exc:
        logger.warning('Cannot record model input: %s', exc)
    response = None
    try:
        from contextlib import nullcontext
        from langsmith import tracing_context
        from backend.config import config as global_config
        # Agent/chat calls retain normal tracing. Auxiliary calls still have
        # local audit/cost records even when remote tracing is suppressed.
        auxiliary = purpose not in {'decision', 'agent', 'chat', 'strategy_summary', 'memory_review'} and not str(purpose).startswith('news_')
        trace_context = tracing_context(enabled=False) if auxiliary and not getattr(global_config, 'langchain_background_tracing', False) else nullcontext()
        with trace_context:
            trace = model_call_trace(purpose, config_id=config_id, model=model, messages=messages,
                                     run_id=run_id, settings=request_settings) if purpose in {'strategy_summary', 'memory_review'} else nullcontext()
            with trace as remote_run:
                response = operation()
                record_trace_response(remote_run, response)
                if response_validator is not None:
                    response_validator(response)
    except Exception as exc:
        if run_id:
            try:
                fields = _response_audit_fields(response)
                fields['details'] = {**fields.get('details', {}), 'request_settings': request_diagnostics(request_settings or {})}
                finish_agent_run(run_id, status='error', error=str(exc), **fields)
            except Exception as audit_exc:
                logger.warning('Cannot record model error: %s', audit_exc)
        raise
    if run_id:
        try:
            fields = _response_audit_fields(response)
            fields['details'] = {**fields.get('details', {}), 'request_settings': request_diagnostics(request_settings or {})}
            finish_agent_run(run_id, status='success', **fields)
        except Exception as exc:
            logger.warning('Cannot record model result: %s', exc)
    return response
