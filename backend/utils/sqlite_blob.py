"""Incremental SQLite reads: inspect blob length before allocating its value."""
from contextlib import contextmanager

from backend.utils.runtime_capacity import CapacityBusy, reserve_bytes


@contextmanager
def bounded_text(conn, table, column, rowid, *, limit, kind='parse'):
    with conn.blobopen(table, column, int(rowid), readonly=True) as blob:
        size = len(blob)
        if size > limit:
            raise CapacityBusy('storage', 'record_bytes')
        lease = reserve_bytes(kind, max(4096, size * 8))
        try:
            value = blob.read().decode('utf-8')
        except BaseException:
            lease.release()
            raise
    try:
        yield value
    finally:
        lease.release()


def text_page(conn, table, column, rowid, *, offset, length):
    """Byte cursor, at most length Unicode characters; never split UTF-8."""
    with conn.blobopen(table, column, int(rowid), readonly=True) as blob:
        total = len(blob)
        blob.seek(min(offset, total))
        raw = blob.read(min(length * 4, total - min(offset, total)))
    # The previous page always supplies a character boundary. A fabricated
    # cursor into a multi-byte character is rejected, not silently repaired.
    import codecs
    decoder = codecs.getincrementaldecoder('utf-8')('strict')
    text = decoder.decode(raw, final=offset + len(raw) >= total)[:length]
    following = offset + len(text.encode('utf-8'))
    return {'text': text, 'offset': offset, 'offset_unit': 'bytes',
            'next_offset': following if following < total else None,
            'has_more': following < total, 'total_bytes': total}
