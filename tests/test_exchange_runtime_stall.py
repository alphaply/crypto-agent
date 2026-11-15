"""Exercise real finalizers/locks in a child process so regressions cannot hang pytest."""
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest


@pytest.mark.parametrize('scenario', [
    'gc_during_account_creation', 'gc_during_diagnostics', 'diagnostics_lock_order',
])
def test_client_cleanup_cannot_deadlock_runtime(scenario):
    script = '''
import gc
import faulthandler
import os
import threading
from backend.mcp import exchange_runtime as runtime
from backend.utils import public_catalogue

class Exchange:
    def fetch(self, *args, **kwargs):
        return {}

gc.disable()
client = Exchange()
runtime.install_transport(client, 'old')
account = client._runtime_account

def await_threads(*threads):
    for thread in threads:
        thread.join(3)
    if any(thread.is_alive() for thread in threads):
        faulthandler.dump_traceback()
        os._exit(2)

if SCENARIO == 'gc_during_account_creation':
    del client
    original = runtime.Account
    class CollectingAccount(original):
        def __init__(self):
            gc.collect()
            super().__init__()
    runtime.Account = CollectingAccount
    worker = threading.Thread(target=lambda: runtime.install_transport(Exchange(), 'new'), daemon=True)
    worker.start()
    await_threads(worker)
    assert account.references == 0
elif SCENARIO == 'gc_during_diagnostics':
    del client
    original = runtime.catalogue_diagnostics
    def collect_while_sampling():
        with public_catalogue._lock:
            gc.collect()
            return original()
    runtime.catalogue_diagnostics = collect_while_sampling
    worker = threading.Thread(target=runtime.diagnostics, daemon=True)
    worker.start()
    await_threads(worker)
    assert account.references == 0
else:
    catalogue_held, diagnostic_started = threading.Event(), threading.Event()
    original = runtime.catalogue_diagnostics
    def diagnose_catalogue():
        diagnostic_started.set()
        return original()
    runtime.catalogue_diagnostics = diagnose_catalogue
    def collect_under_catalogue_lock():
        with public_catalogue._lock:
            catalogue_held.set()
            assert diagnostic_started.wait(2)
            runtime.close_runtime_client(client)
    cleanup = threading.Thread(target=collect_under_catalogue_lock, daemon=True)
    cleanup.start()
    assert catalogue_held.wait(2)
    diagnostic = threading.Thread(target=runtime.diagnostics, daemon=True)
    diagnostic.start()
    await_threads(cleanup, diagnostic)
    assert account.references == 0
print('cleanup and subsequent client access completed')
'''
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, '-c', f'SCENARIO = {scenario!r}\n' + textwrap.dedent(script)],
        cwd=root, env={**os.environ, 'PYTHON_DOTENV_DISABLED': '1'},
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert 'cleanup and subsequent client access completed' in result.stdout
