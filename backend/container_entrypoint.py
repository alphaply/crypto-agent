"""Apply pending schema migrations before starting container services."""
from __future__ import annotations

import os
import sys


def main() -> int:
    command = sys.argv[1:]
    if not command:
        print('Container service command is required', file=sys.stderr)
        return 1
    service_modules = {'backend.app', 'backend.app.main', 'backend.app.core.scheduler'}
    starts_service = any(
        argument == '-m' and command[index + 1] in service_modules
        for index, argument in enumerate(command[:-1])
    ) or 'backend.app.main:app' in command
    if starts_service:
        from backend.storage.migrate import main as migrate_main

        if migrate_main([]) != 0:
            return 1
    os.execvp(command[0], command)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
