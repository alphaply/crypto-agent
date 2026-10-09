"""Best-effort inspection of model calls with optional response validation."""
from typing import Any, Callable

from backend.utils.llm_utils import extract_message_text, extract_usage
from backend.utils.logger import setup_logger

logger = setup_logger('AgentCallAudit')


def _response_audit_fields(response) -> dict:
    if response is None:
        return {}
    usage = extract_usage(response)
    return {
        'output': extract_message_text(response),
        'usage': usage,
        'details': {'tool_calls': getattr(response, 'tool_calls', [])},
    }


def audited_invoke(
    operation, *, config_id, purpose, model, messages, tools=None,
    response_validator: Callable[[Any], None] | None = None,
    provider_id: str | None = None,
    parent_run_id: str | None = None,
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
        response = operation()
        if response_validator is not None:
            response_validator(response)
    except Exception as exc:
        if run_id:
            try:
                finish_agent_run(run_id, status='error', error=str(exc), **_response_audit_fields(response))
            except Exception as audit_exc:
                logger.warning('Cannot record model error: %s', audit_exc)
        raise
    if run_id:
        try:
            finish_agent_run(run_id, status='success', **_response_audit_fields(response))
        except Exception as exc:
            logger.warning('Cannot record model result: %s', exc)
    return response
