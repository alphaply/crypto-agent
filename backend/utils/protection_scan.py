"""Indexed, bounded discovery of unfinished protection plans.

The original JSON stays authoritative. This small projection makes DONE history
absent from the polling work set; malformed or oversized plans remain unresolved.
"""
from backend.utils.runtime_capacity import positive_setting, capacity_limits_enabled


def initialize_schema(cursor):
    maximum = positive_setting('RUNTIME_MAX_RECORD_BYTES', 4 * 1024 * 1024)
    limits_enabled = capacity_limits_enabled()
    cursor.execute('''CREATE TABLE IF NOT EXISTS protection_plan_activity (
        plan_rowid INTEGER PRIMARY KEY, config_id TEXT NOT NULL,
        state TEXT NOT NULL, updated_at REAL, payload_bytes INTEGER NOT NULL)''')
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_protection_active_page "
                   "ON protection_plan_activity(plan_rowid) WHERE state!='DONE'")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_protection_config_active_page "
                   "ON protection_plan_activity(config_id,plan_rowid) WHERE state!='DONE'")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_protection_recent_done "
                   "ON protection_plan_activity(updated_at,plan_rowid) WHERE state='DONE'")
    cursor.execute('''CREATE TABLE IF NOT EXISTS protection_plan_scan_progress (
        id INTEGER PRIMARY KEY CHECK(id=1), cursor INTEGER NOT NULL, complete INTEGER NOT NULL)''')
    cursor.execute('INSERT OR IGNORE INTO protection_plan_scan_progress VALUES(1,0,0)')
    cursor.execute('''CREATE TABLE IF NOT EXISTS protection_plan_scan_mode (
        id INTEGER PRIMARY KEY CHECK(id=1), limits_enabled INTEGER NOT NULL)''')
    previous = cursor.execute('SELECT limits_enabled FROM protection_plan_scan_mode WHERE id=1').fetchone()
    if (previous is None and not limits_enabled) or (previous is not None and bool(previous[0]) != limits_enabled):
        # Old oversized rows may have been projected as UNKNOWN. Revisit only
        # this derived index, using the same durable pages as legacy migration.
        cursor.execute('UPDATE protection_plan_scan_progress SET cursor=0,complete=0 WHERE id=1')
    cursor.execute('INSERT INTO protection_plan_scan_mode VALUES(1,?) '
                   'ON CONFLICT(id) DO UPDATE SET limits_enabled=excluded.limits_enabled',
                   (int(limits_enabled),))
    def expressions(prefix):
        payload = prefix + 'payload'
        length = f'length(CAST({payload} AS BLOB))'
        valid = (f'{length}<={maximum} AND ' if limits_enabled else '') + f'json_valid({payload})'
        state = f"CASE WHEN {valid} THEN coalesce(json_extract({payload},'$.state'),'UNKNOWN') ELSE 'UNKNOWN' END"
        updated = f"CASE WHEN {valid} THEN json_extract({payload},'$.updated_at') END"
        return state, updated, length
    state, updated, length = expressions('NEW.')
    for event in ('INSERT', 'UPDATE'):
        cursor.execute(f'DROP TRIGGER IF EXISTS protection_activity_{event.lower()}')
        cursor.execute(f'''CREATE TRIGGER protection_activity_{event.lower()}
            AFTER {event} ON real_protection_plans BEGIN
            UPDATE protection_plan_activity SET config_id=NEW.config_id,state={state},
                updated_at={updated},payload_bytes={length} WHERE plan_rowid=NEW.rowid;
            INSERT INTO protection_plan_activity
            SELECT NEW.rowid,NEW.config_id,{state},{updated},{length}
            WHERE NOT EXISTS(SELECT 1 FROM protection_plan_activity WHERE plan_rowid=NEW.rowid); END''')
    cursor.execute('''CREATE TRIGGER IF NOT EXISTS protection_activity_delete
        AFTER DELETE ON real_protection_plans BEGIN
        DELETE FROM protection_plan_activity WHERE plan_rowid=OLD.rowid; END''')
    migrate_activity_page(cursor)


def migrate_activity_page(cursor):
    """Advance legacy indexing without loading all history at startup/tick."""
    progress = cursor.execute('SELECT cursor,complete FROM protection_plan_scan_progress WHERE id=1').fetchone()
    if progress[1]:
        return True
    maximum = positive_setting('RUNTIME_MAX_RECORD_BYTES', 4 * 1024 * 1024)
    batch = positive_setting('RUNTIME_SCAN_BATCH_SIZE', 32)
    rows = cursor.execute('SELECT rowid,config_id FROM real_protection_plans WHERE rowid>? ORDER BY rowid LIMIT ?',
                          (progress[0], batch)).fetchall()
    for reference in rows:
        import json
        from backend.utils.sqlite_blob import bounded_text
        connection = cursor.connection
        with connection.blobopen('real_protection_plans', 'payload', reference[0], readonly=True) as blob:
            size = len(blob)
        state, updated = 'UNKNOWN', None
        if not capacity_limits_enabled() or size <= maximum:
            try:
                with bounded_text(connection, 'real_protection_plans', 'payload', reference[0], limit=maximum) as raw:
                    parsed = json.loads(raw)
                    value = parsed.get('state')
                    state = value if isinstance(value, str) and value else 'UNKNOWN'
                    value = parsed.get('updated_at')
                    updated = value if isinstance(value, (int, float)) else None
            except (ValueError, TypeError, AttributeError):
                pass
        cursor.execute('INSERT INTO protection_plan_activity VALUES(?,?,?,?,?) '
                       'ON CONFLICT(plan_rowid) DO UPDATE SET config_id=excluded.config_id,state=excluded.state,'
                       'updated_at=excluded.updated_at,payload_bytes=excluded.payload_bytes',
                       (reference[0], reference[1], state, updated, size))
    complete = len(rows) < batch
    cursor.execute('UPDATE protection_plan_scan_progress SET cursor=?,complete=? WHERE id=1',
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
        clause = ' AND a.config_id=?'
        parameters.append(config_id)
    parameters.append(limit)
    return conn.execute('SELECT a.plan_rowid AS rowid,a.config_id,p.symbol,p.side,'
                        'a.state,a.updated_at,a.payload_bytes '
                        'FROM protection_plan_activity a JOIN real_protection_plans p ON p.rowid=a.plan_rowid '
                        "WHERE a.state!='DONE' AND a.plan_rowid>?" + clause +
                        ' ORDER BY a.plan_rowid LIMIT ?', parameters).fetchall()


def activity_scan_complete(conn):
    row = conn.execute('SELECT complete FROM protection_plan_scan_progress WHERE id=1').fetchone()
    return bool(row and row[0])


def recent_completed_page(conn, *, since, after_cursor=(0, 0), limit=32):
    """Metadata only: late-fill accounting continues without parsing DONE JSON."""
    if type(limit) is not int or not 1 <= limit <= positive_setting('RUNTIME_SCAN_BATCH_SIZE', 32):
        raise ValueError('Completed-plan page exceeds scan budget')
    return conn.execute("SELECT config_id,plan_rowid,updated_at FROM protection_plan_activity WHERE state='DONE' "
                        'AND updated_at>? AND (updated_at,plan_rowid)>(?,?) '
                        'ORDER BY updated_at,plan_rowid LIMIT ?', (since, *after_cursor, limit)).fetchall()
