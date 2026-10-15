"""Exercise server script order and failure boundaries without a live host."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.parametrize('failure', ['', 'backup', 'migration', 'health'])
def test_update_stages_stop_on_failure(tmp_path, failure):
    bash = shutil.which('bash')
    if os.name == 'nt':
        candidate = Path('C:/Program Files/Git/bin/bash.exe')
        bash = str(candidate) if candidate.exists() else None
    if not bash:
        pytest.skip('Bash is required to exercise Linux deployment scripts')
    scripts = tmp_path / 'scripts'
    scripts.mkdir()
    for name in ('backup-postgresql.sh', 'update-postgresql.sh'):
        shutil.copyfile(Path('scripts') / name, scripts / name)
    (tmp_path / '.env').write_text('ADMIN_PASSWORD=private-test\n', encoding='utf-8')
    (tmp_path / 'compose.yaml').write_text('name: isolated-script-test\n', encoding='utf-8')
    binary = tmp_path / 'bin'
    binary.mkdir()
    (binary / 'sleep').write_text('#!/usr/bin/env bash\nexit 0\n', encoding='utf-8')
    (binary / 'docker').write_text('''#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$FAKE_DOCKER_LOG"
case "$*" in
  *"backend.storage.backup"*) [[ "$FAIL_STAGE" != backup ]] || exit 20 ;;
  *"run --rm -T --no-deps migrate") [[ "$FAIL_STAGE" != migration ]] || exit 21 ;;
  *"exec -T web python"*) [[ "$FAIL_STAGE" != health ]] || exit 22 ;;
  *"migrate cat "*) printf 'PGDMP' ;;
  *"migrate tar "*) printf 'private application data' ;;
  *"ps --all --quiet"*) printf 'test-web\\ntest-db\\n' ;;
  *"inspect --format"*) printf '{"container":"test","image_id":"sha256:old","reference":"test:old"}' ;;
  *"ps --status running --services scheduler") printf 'scheduler\\n' ;;
esac
exit 0
''', encoding='utf-8')
    for file in binary.iterdir():
        file.chmod(0o700)
    log = tmp_path / 'docker-actions.txt'
    # Bash converts Windows paths in cwd, but PATH needs its POSIX spelling.
    prefix = binary.as_posix()
    if os.name == 'nt':
        prefix = '/' + prefix[0].lower() + prefix[2:]
    env = dict(os.environ, FAKE_DOCKER_LOG=log.as_posix(), FAIL_STAGE=failure)
    result = subprocess.run([bash, '-c', 'export PATH="$1:$PATH"; bash scripts/update-postgresql.sh',
                             'update-test', prefix], cwd=tmp_path, env=env, capture_output=True,
                            text=True, timeout=20)
    actions = log.read_text(encoding='utf-8')
    assert 'pull web scheduler migrate' in actions
    assert 'stop web scheduler' in actions
    assert 'private-test' not in result.stdout + result.stderr
    assert ' down ' not in actions and ' db' not in actions
    if failure:
        assert result.returncode != 0
        assert 'Update failed at:' in result.stderr
        assert 'up -d --no-deps scheduler' not in actions
        if failure == 'backup':
            assert 'run --rm -T --no-deps migrate\n' not in actions
        if failure in {'backup', 'migration'}:
            assert 'up -d --no-deps web' not in actions
    else:
        assert result.returncode == 0, result.stderr
        assert actions.index('backend.storage.backup') < actions.index('up -d --no-deps web')
        assert actions.index('up -d --no-deps web') < actions.index('up -d --no-deps scheduler')
        assert list((tmp_path / 'backups').glob('*/COMPLETE'))
        import json
        metadata = next((tmp_path / 'backups').glob('*/images.json'))
        assert len(json.loads(metadata.read_text())) == 2
