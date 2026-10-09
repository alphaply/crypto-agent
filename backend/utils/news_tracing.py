"""Optional LangSmith spans for the shared news pipeline and its HTTP scorer."""
from contextlib import contextmanager
from contextvars import ContextVar
import os

pipeline_run_id = ContextVar('news_pipeline_run_id', default=None)


class SafeSpan:
    def __init__(self, run):
        self.run = run

    def end(self, **kwargs):
        try:
            self.run.end(**kwargs)
        except Exception:
            pass


@contextmanager
def news_span(name, *, inputs=None, run_type='chain', metadata=None):
    """Trace only explicit safe inputs, never a provider config or HTTP headers."""
    try:
        from langsmith import trace, utils
        enabled = utils.tracing_is_enabled()
        if enabled and (enabled == 'local' or os.getenv('LANGSMITH_API_KEY') or os.getenv('LANGCHAIN_API_KEY')):
            manager = trace(name, run_type=run_type, inputs=inputs or {}, metadata=metadata or {}, tags=['shared-news'])
            run = manager.__enter__()
        else:
            run = None
    except Exception:
        run = None
    if run is None:
        yield None
        return
    try:
        yield SafeSpan(run)
    except BaseException as exc:
        try:
            manager.__exit__(type(exc), exc, exc.__traceback__)
        except Exception:
            pass
        raise
    else:
        try:
            manager.__exit__(None, None, None)
        except Exception:
            pass


def trace_identity(run):
    if run is None:
        return {'trace_id': None, 'trace_url': None, 'trace_status': 'disabled'}
    try:
        trace_id = str(run.run.id)
    except Exception:
        return {'trace_id': None, 'trace_url': None, 'trace_status': 'unavailable'}
    try:
        url = run.run.get_url()
    except Exception:
        url = None
    return {'trace_id': trace_id, 'trace_url': url,
            'trace_status': 'enabled' if url else 'unavailable'}
