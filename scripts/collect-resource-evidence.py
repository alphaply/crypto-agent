"""Run outside Web/scheduler to retain read-only evidence if either dies."""
import argparse
import json
import logging
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.utils.host_resource_evidence import host_observation
from backend.utils.resource_diagnostics import EvidenceHandler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--container', action='append', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--kernel-log', default='/var/log/kern.log')
    parser.add_argument('--interval', type=float, default=5)
    parser.add_argument('--max-bytes', type=int, default=1024 * 1024)
    parser.add_argument('--backup-files', type=int, default=4)
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    if not 1 <= args.interval <= 3600 or args.max_bytes < 65536 or not 1 <= args.backup_files <= 32:
        parser.error('interval 1..3600, max-bytes >=65536 and backup-files 1..32 required')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    handler = EvidenceHandler(args.output, maxBytes=args.max_bytes,
                              backupCount=args.backup_files, encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(message)s'))
    logger = logging.Logger('host-resource-evidence')
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    try:
        while True:
            observation = host_observation(args.container, kernel_path=args.kernel_log)
            payload = json.dumps(observation, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
            if len(payload.encode('utf-8')) + 1 > args.max_bytes:
                raise ValueError('Host evidence record exceeds configured file budget')
            logger.info(payload)
            handler.flush()
            if args.once:
                return
            time.sleep(args.interval)
    finally:
        handler.close()


if __name__ == '__main__':
    main()
