"""Present current execution independently from pending scheduler work."""
from __future__ import annotations

import json
from datetime import datetime

from backend.app.services.common import TZ_CN


def scheduler_timestamp_iso(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")
        return TZ_CN.localize(parsed).isoformat()
    except (TypeError, ValueError):
        return str(value)


def execution_view(row: dict | None) -> dict | None:
    if not row:
        return None
    from backend.utils.decision_record import read_decision_record

    result = dict(row)
    result.pop('storage_id', None)
    try:
        result['tool_calls'] = json.loads(result.pop('tool_calls_json', None) or '[]')
    except (TypeError, json.JSONDecodeError):
        result['tool_calls'] = []
    result['decision'] = read_decision_record(result)
    result.pop('decision_json', None)
    result['started_at_iso'] = scheduler_timestamp_iso(result.get('started_at') or result.get('created_at'))
    return result


def runtime_view(state: dict) -> dict:
    return {
        'running': execution_view(state.get('running')),
        'queued_count': state.get('queued_count', 0),
        'next_queued': state.get('next_queued'),
        'last_run': execution_view(state.get('last_run')),
    }
