"""Indexed, bounded discovery of unfinished protection plans.

The original JSON stays authoritative. This small projection makes DONE history
absent from the polling work set; malformed plans remain unresolved.
"""
from backend.utils.runtime_capacity import positive_setting


def initialize_schema(cursor):
    """Schema is installed by the explicit PostgreSQL migration command."""
    limits_enabled = False
    previous = cursor.execute('SELECT limits_enabled FROM protection_plan_scan_mode WHERE id=1').fetchone()
    if previous is not None and bool(previous[0]) != limits_enabled:
        cursor.execute('UPDATE protection_plan_scan_progress SET cursor=0,complete=0 WHERE id=1')
    cursor.execute('UPDATE protection_plan_scan_mode SET limits_enabled=%s WHERE id=1', (int(limits_enabled),))


def migrate_activity_page(cursor):
    """Advance legacy indexing without loading all history at startup/tick."""
    progress = cursor.execute('SELECT cursor,complete FROM protection_plan_scan_progress WHERE id=1').fetchone()
    if progress[1]:
        return True
    maximum = 4 * 1024 * 1024
    batch = positive_setting('RUNTIME_SCAN_BATCH_SIZE', 32)
    rows = cursor.execute('SELECT storage_id AS rowid,config_id FROM real_protection_plans WHERE storage_id>%s ORDER BY storage_id LIMIT %s',
                          (progress[0], batch)).fetchall()
    for reference in rows:
        import json
        from backend.utils.sqlite_blob import bounded_text
        connection = cursor.connection
        from backend.utils.sqlite_blob import blob_size
        size = blob_size(connection, 'real_protection_plans', 'payload', reference[0])
        state, updated = 'UNKNOWN', None
        try:
            with bounded_text(connection, 'real_protection_plans', 'payload', reference[0], limit=maximum) as raw:
                parsed = json.loads(raw)
                value = parsed.get('state')
                state = value if isinstance(value, str) and value else 'UNKNOWN'
                value = parsed.get('updated_at')
                updated = value if isinstance(value, (int, float)) else None
        except (ValueError, TypeError, AttributeError):
            pass
        cursor.execute('INSERT INTO protection_plan_activity(plan_rowid,config_id,state,updated_at,payload_bytes) VALUES(%s,%s,%s,%s,%s) ON CONFLICT(plan_rowid) DO UPDATE SET config_id=excluded.config_id,state=excluded.state,updated_at=excluded.updated_at,payload_bytes=excluded.payload_bytes',
                       (reference[0], reference[1], state, updated, size))
    complete = len(rows) < batch
    cursor.execute('UPDATE protection_plan_scan_progress SET cursor=%s,complete=%s WHERE id=1',
                   (rows[-1][0] if rows else progress[0], int(complete)))
    return complete


def active_plan_page(conn, *, after_cursor=0, limit=32, config_id=None):
    if type(limit) is not int or limit < 1 or type(after_cursor) is not int or after_cursor < 0:
        raise ValueError('Plan page requires positive limit and nonnegative cursor')
    maximum = positive_setting('RUNTIME_SCAN_BATCH_SIZE', 32)
    if limit > maximum:
        raise ValueError('Plan page exceeds RUNTIME_SCAN_BATCH_SIZE')
    migrate_activity_page(conn.cursor())
    clause, parameters = '', [after_cursor]
    if config_id is not None:
        clause = ' AND a.config_id=%s'
        parameters.append(config_id)
    parameters.append(limit)
    return conn.execute('SELECT a.plan_rowid AS rowid,a.config_id,p.symbol,p.side,'
                        'a.state,a.updated_at,a.payload_bytes '
                        'FROM protection_plan_activity a JOIN real_protection_plans p ON p.storage_id=a.plan_rowid '
                        "WHERE a.state!='DONE' AND a.plan_rowid>%s" + clause +
                        ' ORDER BY a.plan_rowid LIMIT %s', parameters).fetchall()


def activity_scan_complete(conn):
    row = conn.execute('SELECT complete FROM protection_plan_scan_progress WHERE id=1').fetchone()
    return bool(row and row[0])


def recent_completed_page(conn, *, since, after_cursor=(0, 0), limit=32):
    """Metadata only: late-fill accounting continues without parsing DONE JSON."""
    if type(limit) is not int or not 1 <= limit <= positive_setting('RUNTIME_SCAN_BATCH_SIZE', 32):
        raise ValueError('Completed-plan page exceeds scan budget')
    return conn.execute("SELECT config_id,plan_rowid,updated_at FROM protection_plan_activity WHERE state='DONE' "
                        'AND updated_at>%s AND (updated_at,plan_rowid)>(%s,%s) '
                        'ORDER BY updated_at,plan_rowid LIMIT %s', (since, *after_cursor, limit)).fetchall()
