"""Read a PostgreSQL database before rollback; never start an agent or exchange.

For backup inspection, pg_restore the dump to an isolated database first, then
set the selected environment variable to that database's connection URL.
"""
import argparse
import json
from pathlib import Path
import os
import sys

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.utils.runtime_compatibility import trading_compatibility_report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--database-url-env', default='DATABASE_URL',
                        help='environment variable containing the PostgreSQL URL (never put passwords in command arguments)')
    parser.add_argument('--supported-contract-version', required=True, type=int, choices=[2, 3])
    args = parser.parse_args()
    connection_url = os.environ.get(args.database_url_env)
    if not connection_url or not connection_url.startswith(('postgresql://', 'postgres://')):
        parser.error('Select an environment variable containing an explicit PostgreSQL URL')
    try:
        with psycopg.connect(connection_url, connect_timeout=10,
                             options='-c default_transaction_read_only=on -c statement_timeout=30000') as conn:
            report = trading_compatibility_report(conn, supported_contract_version=args.supported_contract_version)
    except psycopg.Error as exc:
        print('Compatibility inspection failed: ' + type(exc).__name__, file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 2 if report['status'] == 'blocked' else 0


if __name__ == '__main__':
    raise SystemExit(main())
