"""Bounded error diagnostics and credential redaction for private logs."""
from __future__ import annotations

import os
import re
import threading
import traceback
from collections import deque

_lock = threading.RLock()
_secrets: deque[str] = deque(maxlen=1024)
_secret_name = re.compile(r'(?:api[_-]?key|master[_-]?key|private[_-]?key|secret|password|passphrase|authorization|(?:^|[_-])token(?:$|[_-])|access[_-]?token|refresh[_-]?token)', re.I)
_assignment = re.compile(
    r'''((?:[\w-]*(?:api[_-]?key|master[_-]?key|private[_-]?key|secret|password|passphrase|token)|authorization)["']?\s*[:=]\s*)'''
    r'''(?:"[^"\n]*"|'[^'\n]*'|[^\s,;&}\]]+)''', re.I)
_authorization = re.compile(r'\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+', re.I)
_url_password = re.compile(r'(\b[a-z][a-z0-9+.-]*://)[^\s/@]+:[^\s/@]+@', re.I)


def register_log_secrets(*values: str) -> None:
    with _lock:
        for value in values:
            if isinstance(value, str) and len(value) >= 6 and value not in _secrets:
                _secrets.append(value)


def register_snapshot_secrets(value: object) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if _secret_name.search(str(key)) and isinstance(item, str):
                register_log_secrets(item)
            elif isinstance(item, (dict, list)):
                register_snapshot_secrets(item)
    elif isinstance(value, list):
        for item in value:
            register_snapshot_secrets(item)


def redact_log_text(value: object) -> str:
    text = str(value)
    with _lock:
        secrets = set(_secrets)
    secrets.update(v for k, v in os.environ.items() if _secret_name.search(k) and len(v) >= 6)
    for secret in sorted(secrets, key=len, reverse=True):
        text = text.replace(secret, '[redacted]')
    text = _authorization.sub(r'\1 [redacted]', text)
    text = _url_password.sub(r'\1[redacted]@', text)
    return _assignment.sub(r'\1[redacted]', text)


def exception_diagnostic(exc: BaseException, *, stage: str | None = None,
                         run_id: str | None = None) -> dict:
    chain, seen = [], set()
    current = exc
    while current is not None and id(current) not in seen and len(chain) < 8:
        seen.add(id(current))
        chain.append(current)
        current = (getattr(current, 'original', None) or current.__cause__
                   or (None if current.__suppress_context__ else current.__context__))
    root = chain[-1]
    code = next((getattr(e, 'code', None) or getattr(e, 'error_type', None)
                 for e in chain if getattr(e, 'code', None) or getattr(e, 'error_type', None)), None)
    if isinstance(root, ValueError):
        code = 'invalid_response' if any(getattr(e, 'purpose', None) for e in chain) else 'validation_error'
    elif isinstance(root, (TimeoutError, ConnectionError)):
        code = 'timeout' if isinstance(root, TimeoutError) else 'connection_error'
    diagnostic = {
        'code': code or 'processing_error', 'stage': stage, 'pipeline_run_id': run_id,
        'exception_type': type(exc).__name__, 'cause_type': type(root).__name__,
        'message': redact_log_text(str(root) or type(root).__name__)[:2000],
        'traceback': redact_log_text(''.join(traceback.format_exception(exc)))[-16000:],
    }
    for field in ('attempts', 'call_run_id', 'model', 'provider_id', 'purpose'):
        value = next((getattr(e, field, None) for e in chain if getattr(e, field, None) is not None), None)
        if value is not None:
            diagnostic[field] = redact_log_text(value) if isinstance(value, str) else value
    status = next((getattr(e, 'status_code', None) for e in chain if getattr(e, 'status_code', None)), None)
    if status is not None:
        diagnostic['http_status'] = int(status)
    return diagnostic
