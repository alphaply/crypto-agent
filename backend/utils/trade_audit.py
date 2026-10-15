"""Passive, version-fixed evidence pages. No execution or exchange calls."""
from typing import Literal


def _page(conn, table, identity, parameters, *, view, offset, length, version, value_column='payload'):
    if view not in {'original', 'latest'} or offset < 0 or not 1 <= length <= 2048:
        raise ValueError('Audit pages require view original/latest, nonnegative offset and length 1–2048 characters')
    order = 'ASC' if view == 'original' else 'DESC'
    if version is None:
        row = conn.execute(f'SELECT revision,observed_at FROM {table} WHERE {identity} ORDER BY revision {order} LIMIT 1',
                           parameters).fetchone()
        if row is None:
            raise FileNotFoundError('Original execution evidence not found in the authorized scope')
        version = str(row['revision'])
    if not str(version).isdigit():
        raise ValueError('Invalid evidence version')
    row = conn.execute(f'SELECT rowid,revision,observed_at '
                       f'FROM {table} WHERE {identity} AND revision=?',
                       (*parameters, int(version))).fetchone()
    if row is None:
        raise FileNotFoundError('Requested immutable evidence version not found')
    from backend.utils.sqlite_blob import text_page
    page = text_page(conn, table, value_column, row['rowid'], offset=offset, length=length)
    return {'view':view,'version':str(row['revision']),'observed_at':row['observed_at'],
            **page, 'read_only':True}


def task_audit(config_id: str, operation_id: str, *, view: Literal['original','latest']='original',
               offset: int=0, length: int=2048, version: str | None=None):
    from backend.database import get_db_conn
    with get_db_conn() as conn:
        return _page(conn,'trade_receipt_revisions','config_id=? AND operation_id=?',(config_id,operation_id),
                     view=view,offset=offset,length=length,version=version)


def live_audit(caller, profile_id, operation_id, *, view='original',offset=0,length=2048,version=None):
    from backend.mcp import service, store
    service.require(caller,'read',profile_id)
    with store.connection(recovery=True) as conn:
        # Authorization uses the external identity's original principal, never
        # an account hash or a caller-supplied internal task / child identity.
        found = conn.execute('SELECT 1 FROM mcp_operations WHERE profile_id=? AND operation_id=? AND principal=?',
                             (profile_id,operation_id,caller['id'])).fetchone()
        if not found:
            raise FileNotFoundError('Original execution evidence not found in the authorized scope')
        return _page(conn,'mcp_operation_revisions','profile_id=? AND operation_id=?',(profile_id,operation_id),
                     view=view,offset=offset,length=length,version=version,value_column='result')
