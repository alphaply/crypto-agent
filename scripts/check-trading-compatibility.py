"""Inspect a copied database before rollback; never start an agent or exchange."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.utils.runtime_compatibility import trading_compatibility_report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--database', required=True, type=Path)
    parser.add_argument('--supported-contract-version', required=True, type=int, choices=[2, 3])
    args = parser.parse_args()
    path = args.database.resolve()
    if not path.is_file():
        parser.error('Database must be an existing file or verified backup')
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as conn:
        report = trading_compatibility_report(conn, supported_contract_version=args.supported_contract_version)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 2 if report['status'] == 'blocked' else 0


if __name__ == '__main__':
    raise SystemExit(main())
