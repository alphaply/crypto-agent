"""Observational task watchdog; never cancels workers or releases trade locks."""
import sys
import threading
import time
from contextlib import contextmanager


STALL_WARNING_SECONDS = 600
_lock = threading.RLock()
_active = {}


@contextmanager
def track_task(config_id, scheduled_at):
    key = (config_id, scheduled_at)
    state = {'thread_id': threading.get_ident(), 'last_progress': time.monotonic(),
             'last_warning': None, 'phase': 'preparing'}
    with _lock:
        _active[key] = state

    def progress(event):
        with _lock:
            state.update(last_progress=time.monotonic(), phase=event.get('phase', 'working'))

    try:
        yield progress
    finally:
        with _lock:
            _active.pop(key, None)


def warn_stalled_tasks(logger):
    now = time.monotonic()
    with _lock:
        stalled = []
        for key, state in _active.items():
            idle = now - state['last_progress']
            if idle < STALL_WARNING_SECONDS:
                continue
            if state['last_warning'] is not None and now - state['last_warning'] < STALL_WARNING_SECONDS:
                continue
            state['last_warning'] = now
            stalled.append((key, dict(state), int(idle)))
    frames = sys._current_frames() if stalled else {}
    for (config_id, scheduled_at), state, idle in stalled:
        frame = frames.get(state['thread_id'])
        stack = []
        while frame is not None and len(stack) < 24:
            # No locals, source lines, arguments or credentials in diagnostics.
            stack.append(f'{frame.f_code.co_filename}:{frame.f_lineno}:{frame.f_code.co_name}')
            frame = frame.f_back
        logger.error('[TaskStalled] config_id=%s scheduled_at=%s phase=%s idle_seconds=%s '
                     'worker_still_running=true stack=%s',
                     config_id, scheduled_at, state['phase'], idle, ' <- '.join(stack))
