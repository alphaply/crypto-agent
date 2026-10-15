"""Bounded PostgreSQL UTF-8 byte reads (legacy import name retained)."""
from contextlib import contextmanager

from psycopg import sql
from backend.utils.runtime_capacity import CapacityBusy, reserve_bytes


def blob_size(conn, table, column, storage_id):
    query = sql.SQL('SELECT octet_length({column}) FROM {table} WHERE storage_id=%s').format(
        column=sql.Identifier(column), table=sql.Identifier(table))
    row = conn.execute(query, (int(storage_id),)).fetchone()
    if row is None:
        raise ValueError('Stored record no longer exists')
    return row[0] or 0


def blob_slice(conn, table, column, storage_id, *, offset=0, length, expected_size=None):
    if offset < 0 or length < 0:
        raise ValueError('Byte offset and length must be nonnegative')
    query = sql.SQL("SELECT substring(convert_to({column},'UTF8') FROM %s FOR %s), COALESCE(octet_length({column}),0) FROM {table} WHERE storage_id=%s").format(
        column=sql.Identifier(column), table=sql.Identifier(table))
    row = conn.execute(query, (int(offset) + 1, int(length), int(storage_id))).fetchone()
    if row is None:
        raise ValueError('Stored record no longer exists')
    if expected_size is not None and row[1] != expected_size:
        raise ValueError('Stored record changed during bounded read; retry the original read')
    return bytes(row[0] or b'')


@contextmanager
def bounded_text(conn, table, column, rowid, *, limit, kind='parse'):
    size = blob_size(conn, table, column, rowid)
    lease = reserve_bytes(kind, max(4096, size * 8))
    try:
        value = blob_slice(conn, table, column, rowid, length=size, expected_size=size).decode('utf-8')
        yield value
    finally:
        lease.release()


def text_page(conn, table, column, rowid, *, offset, length):
    """Byte cursor, at most length Unicode characters; never split UTF-8."""
    total = blob_size(conn, table, column, rowid)
    raw = blob_slice(conn, table, column, rowid, offset=min(offset, total),
                     length=min(length * 4, total - min(offset, total)), expected_size=total)
    import codecs
    decoder = codecs.getincrementaldecoder('utf-8')('strict')
    text = decoder.decode(raw, final=offset + len(raw) >= total)[:length]
    following = offset + len(text.encode('utf-8'))
    return {'text': text, 'offset': offset, 'offset_unit': 'bytes',
            'next_offset': following if following < total else None,
            'has_more': following < total, 'total_bytes': total}
