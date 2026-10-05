"""Best-effort local inspection of actual model calls; never changes execution."""
from backend.utils.llm_utils import extract_message_text
from backend.utils.logger import setup_logger

logger = setup_logger('AgentCallAudit')


def audited_invoke(operation, *, config_id, purpose, model, messages, tools=None):
    from backend.database_agent_runs import start_agent_run, finish_agent_run

    run_id = None
    try:
        run_id = start_agent_run(config_id or 'unknown', purpose, model or 'unknown', messages, tools)
    except Exception as exc:
        logger.warning('Cannot record model input: %s', exc)
    try:
        response = operation()
    except Exception as exc:
        if run_id:
            try:
                finish_agent_run(run_id, status='error', error=str(exc))
            except Exception as audit_exc:
                logger.warning('Cannot record model error: %s', audit_exc)
        raise
    if run_id:
        try:
            usage = (getattr(response, 'usage_metadata', None)
                     or getattr(response, 'response_metadata', {}).get('token_usage', {}))
            finish_agent_run(run_id, status='success', output=extract_message_text(response), usage=usage,
                             details={'tool_calls': getattr(response, 'tool_calls', [])})
        except Exception as exc:
            logger.warning('Cannot record model result: %s', exc)
    return response
