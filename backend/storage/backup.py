"""PostgreSQL custom-format backups without credentials in process arguments."""
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from psycopg.conninfo import conninfo_to_dict
from psycopg.pq import Conninfo

from backend.storage import database_url


def dump_database(path: str | Path, *, timeout: float = 90) -> Path:
    target = Path(path)
    executable = shutil.which('pg_dump')
    if not executable:
        raise RuntimeError('pg_dump is required; install the matching PostgreSQL client')
    env = {key: value for key, value in os.environ.items() if not key.startswith('PG')}
    # PGDATABASE is a database name, not an expanded connection URI. Use libpq's
    # native environment names so percent-encoded passwords are decoded once.
    names = {item.keyword.decode(): item.envvar.decode()
             for item in Conninfo.get_defaults() if item.envvar}
    parameters = conninfo_to_dict(database_url())
    extra = {}
    for key, value in parameters.items():
        if key in names:
            env[names[key]] = value
        else:
            extra[key] = value
    env.update(PGCONNECT_TIMEOUT='10', PGAPPNAME='crypto-agent-backup')
    try:
        target.touch(mode=0o600, exist_ok=True)
        target.chmod(0o600)
        with tempfile.TemporaryDirectory(prefix='crypto-pg-client-') as directory:
            if extra:
                # A few libpq options (e.g. keepalives) have no environment
                # equivalent. A private service file keeps those out of argv.
                if 'PGSERVICE' in env:
                    raise RuntimeError('Backup cannot combine a named service with additional service-file options')
                lines = ['[crypto_backup]']
                for key, value in extra.items():
                    line = key + '=' + value
                    if '\n' in line or '\r' in line or value != value.rstrip() or len(line.encode()) >= 1022:
                        raise RuntimeError('Backup connection option cannot be represented in a PostgreSQL service file')
                    lines.append(line)
                service = Path(directory) / 'pg_service.conf'
                service.touch(mode=0o600)
                service.write_text('\n'.join(lines) + '\n', encoding='utf-8')
                env.update(PGSERVICE='crypto_backup', PGSERVICEFILE=str(service))
            result = subprocess.run([executable, '--format=custom', '--no-owner', '--no-privileges',
                                     '--file', str(target)], env=env, capture_output=True,
                                    timeout=timeout, check=False)
        if result.returncode:
            raise RuntimeError('PostgreSQL backup failed; check availability and client version')
        if target.stat().st_size == 0:
            raise RuntimeError('PostgreSQL backup produced an empty file')
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return target


def export_database_bytes() -> tuple[bytes, str]:
    filename = f'trading_data_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}.dump'
    with tempfile.TemporaryDirectory(prefix='crypto-pg-export-') as directory:
        return dump_database(Path(directory) / filename).read_bytes(), filename


def main():
    import argparse
    import sys
    from dotenv import load_dotenv
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', type=Path)
    args = parser.parse_args()
    if args.destination.exists():
        parser.error('Destination already exists; choose a new backup filename')
    args.destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        dump_database(args.destination)
    except Exception as exc:
        print(f'Backup failed ({type(exc).__name__}); no completed dump', file=sys.stderr)
        return 1
    print(f'Backup complete: {args.destination.name}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
