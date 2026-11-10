"""Durable model intent. Receipts may evolve; original reasons never do."""
from datetime import datetime, timezone
import json

from backend import database
from backend.utils.logger import setup_logger
from backend.utils.trade_operations import tool_result_status

logger = setup_logger(__name__)
ACTION_TO_TOOL = dict(open='open_position', close='close_position', edit='edit_order',
                      cancel='cancel_order', protect='set_position_protection')


def record_trade_call(config_id, operation_id, tool_name, arguments, symbol, result=None, *, status=None, tool_call_id=None):
    action = next((key for key, value in ACTION_TO_TOOL.items() if value == tool_name), None)
    if not action or not config_id or not operation_id:
        return
    args = arguments if isinstance(arguments, dict) else {}
    encoded = json.dumps(arguments, ensure_ascii=False, sort_keys=True, default=str)
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except ValueError:
            result = {'status': tool_result_status(result), 'message': result}
    state = status or ((result.get('status') or tool_result_status(result)) if isinstance(result, dict) else 'pending')
    now = datetime.now(timezone.utc).isoformat()
    try:
        with database.get_db_conn() as conn:
            conn.execute('''INSERT INTO agent_trade_calls
                (config_id,operation_id,tool_name,symbol,action,reason,arguments_json,result_json,status,created_at,updated_at,tool_call_id)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(config_id,operation_id) DO UPDATE SET result_json=excluded.result_json,
                    status=excluded.status,updated_at=excluded.updated_at
                WHERE agent_trade_calls.arguments_json=excluded.arguments_json
                  AND excluded.result_json IS NOT NULL
                  AND (agent_trade_calls.result_json IS NULL
                    OR agent_trade_calls.status IN ('pending','running','unknown','awaiting_approval')
                    OR agent_trade_calls.result_json=excluded.result_json)''',
                (config_id, operation_id, tool_name, args.get('symbol') or symbol, action,
                 args.get('reason') if isinstance(args.get('reason'), str) else None, encoded,
                 json.dumps(result, ensure_ascii=False, default=str) if result is not None else None, state, now, now,
                 tool_call_id))
            conn.commit()
    except Exception:
        # Do not turn a financial acknowledgement into a failed execution when
        # the display index is unavailable. The durable execution request remains.
        logger.exception('Trade intent index unavailable: %s/%s', config_id, operation_id)


def list_trade_calls(config_id: str, *, page=1, per_page=20, action=None, status=None,
                     start=None, end=None) -> dict:
    conditions, parameters = ['c.config_id=%s'], [config_id]
    for column, value, operator in [('action', action, '='), ('status', status, '='),
                                     ('created_at', start, '>='), ('created_at', end, '<=')]:
        if value:
            if column == 'status' and value == 'unresolved':
                conditions.append("c.status IN ('pending','unknown','running','awaiting_approval')")
                continue
            conditions.append(f'c.{column}{operator}%s')
            parameters.append(value)
    with database.get_db_conn() as conn:
        # Recovery writes the canonical receipt. Reflect it without another
        # financial invocation and without assigning a new operation identity.
        conn.execute('''UPDATE agent_trade_calls c SET result_json=r.result,
            status=r.status,updated_at=to_char(to_timestamp(r.updated_at) AT TIME ZONE 'UTC',
              'YYYY-MM-DD"T"HH24:MI:SS.US"+00:00"')
            FROM trade_action_runs r WHERE c.config_id=%s AND r.config_id=c.config_id
              AND r.operation_id=c.operation_id AND r.result IS NOT NULL
              AND c.status IN ('pending','unknown','running','submitted','completed','filled','failed')
              AND (c.result_json IS DISTINCT FROM r.result OR c.status IS DISTINCT FROM r.status)''', (config_id,))
        where = ' AND '.join(conditions)
        count = conn.execute('SELECT COUNT(*) FROM agent_trade_calls c WHERE '+where, parameters).fetchone()[0]
        rows = conn.execute('SELECT c.* FROM agent_trade_calls c WHERE '+where+
                            ' ORDER BY created_at DESC,operation_id LIMIT %s OFFSET %s',
                            (*parameters, per_page, (page-1)*per_page)).fetchall()
        conn.commit()
    items = []
    for row in rows:
        item = dict(row)
        item['arguments'] = json.loads(item.pop('arguments_json'))
        item['result'] = json.loads(item.pop('result_json') or 'null')
        items.append(item)
    return dict(items=items, total=count, page=page, per_page=per_page)
