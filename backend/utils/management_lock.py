"""Process-lifetime financial locks on the shared SQLite data volume."""
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path


class ManagementLockBusy(ValueError):
    pass


@contextmanager
def scope_file_lock(account_scope, symbol, side):
    from backend import database
    directory = Path(database.DB_NAME).resolve().parent / '.management-locks'
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    identity = hashlib.sha256(f'{account_scope}\0{symbol}\0{side}'.encode()).hexdigest()
    descriptor = os.open(str(directory / (identity + '.lock')), os.O_CREAT | os.O_RDWR, 0o600)
    locked = False
    try:
        if os.name == 'nt':
            import msvcrt
            if os.fstat(descriptor).st_size == 0:
                try:
                    os.write(descriptor, b'0')
                except PermissionError:
                    # A competing initializer may have written and locked byte
                    # zero after our size read. This is the same normal busy
                    # outcome as LK_NBLCK contention, never a raw permission leak.
                    raise ManagementLockBusy('Another exchange action is running in this scope') from None
            os.lseek(descriptor, 0, os.SEEK_SET)
            try:
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            except OSError:
                raise ManagementLockBusy('Another exchange action is running in this scope') from None
        else:
            import fcntl
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ManagementLockBusy('Another exchange action is running in this scope') from None
        locked = True
        yield
    finally:
        try:
            if locked:
                if os.name == 'nt':
                    import msvcrt
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
