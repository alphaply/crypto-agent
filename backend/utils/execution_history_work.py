"""Durable coalesced history work; never invoked inline by a financial save."""
from contextvars import ContextVar
import time
from backend.database import get_db_conn

historical_publication = ContextVar('historical_publication', default=False)


def request_history(config_id, scope, symbol, *, conn=None):
    def enqueue(connection):
        connection.execute('''INSERT INTO execution_history_work(config_id,account_scope,symbol)
            VALUES(%s,%s,%s) ON CONFLICT(config_id,account_scope,symbol)
            DO UPDATE SET version=execution_history_work.version+1''', (config_id,scope,symbol))
    if conn is not None:
        enqueue(conn)
    else:
        with get_db_conn() as connection:
            enqueue(connection)
            connection.commit()


def drain_history(config_id, scope, symbol):
    from backend.utils.management_lock import scope_file_lock, ManagementLockBusy
    from backend.utils.execution_ledger import rebuild_execution_position_history
    from backend.utils.execution_timing import stage
    try:
        with scope_file_lock('history-work', scope, symbol + '\0' + config_id):
            with get_db_conn() as conn:
                row = conn.execute('SELECT version FROM execution_history_work WHERE config_id=%s '
                                   'AND account_scope=%s AND symbol=%s', (config_id,scope,symbol)).fetchone()
            if not row:
                return
            with get_db_conn() as conn:
                conn.execute('UPDATE execution_history_work SET attempted_at=%s WHERE config_id=%s '
                             'AND account_scope=%s AND symbol=%s', (time.time(),config_id,scope,symbol))
                conn.commit()
            with stage('history_rebuild'):
                result = rebuild_execution_position_history(config_id, scope, symbol)
            if result.get('published'):
                with get_db_conn() as conn:
                    conn.execute('DELETE FROM execution_history_work WHERE config_id=%s AND account_scope=%s '
                                 'AND symbol=%s AND version=%s', (config_id,scope,symbol,row['version']))
                    conn.commit()
    except ManagementLockBusy:
        return


def drain_scope_history(scope, symbol):
    """One queued owner per scheduler slice, including retired source actors.

    Rotating by last attempt prevents one incomplete owner starving the others.
    This computes evidence only and cannot submit an exchange action.
    """
    with get_db_conn() as conn:
        row = conn.execute('SELECT config_id FROM execution_history_work WHERE account_scope=%s '
                           'AND symbol=%s ORDER BY attempted_at,config_id LIMIT 1', (scope,symbol)).fetchone()
    if row:
        drain_history(row['config_id'],scope,symbol)
