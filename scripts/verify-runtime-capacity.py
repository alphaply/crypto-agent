"""Measure two bounded isolated application containers against a frozen profile.

Usage: uv run python scripts/verify-runtime-capacity.py --image IMAGE --profile PATH
       --mode calibration|acceptance --output DIRECTORY
Every run retains its predeclared manifest and report, including failed runs.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from datetime import datetime, timezone
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.utils.capacity_profile import PHASES, load_profile, assess_report


def command(*args, timeout=30):
    result = subprocess.run(list(args), cwd=ROOT, capture_output=True, text=True,
                            encoding='utf-8', errors='replace', timeout=timeout)
    if result.returncode:
        raise RuntimeError(f'{args[0]} exited {result.returncode}: {result.stderr[-2048:]}')
    if len(result.stdout.encode('utf-8')) > 2 * 1024 * 1024:
        raise ValueError('Verification command output exceeds budget')
    return result.stdout.strip()


def request(url, *, post=False):
    req = urllib.request.Request(url, data=b'' if post else None)
    with urllib.request.urlopen(req, timeout=10 if '/verification/result' in url else 3) as response:
        raw = response.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError('Verification response exceeds budget')
    return json.loads(raw)


def wait_ready(url, seconds=90):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            result = request(url + '/health')
            if result.get('success'):
                return request(url + '/verification/result')
        except Exception:
            pass
        time.sleep(.5)
    raise RuntimeError('Isolated application did not become healthy')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True)
    parser.add_argument('--profile', type=Path, required=True)
    parser.add_argument('--mode', choices=['calibration', 'acceptance'], required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    profile = load_profile(args.profile)
    image_id = command('docker', 'image', 'inspect', args.image, '--format', '{{.Id}}')
    revision = command('docker', 'image', 'inspect', args.image,
        '--format', '{{index .Config.Labels "org.opencontainers.image.revision"}}')
    if not revision:
        raise ValueError('Verification image must declare its source revision label')
    info = json.loads(command('docker', 'info', '--format', '{{json .}}'))
    run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8]
    destination = args.output.resolve() / run_id
    destination.mkdir(parents=True, exist_ok=False)
    manifest = {'run_id': run_id, 'declared_at_utc': datetime.now(timezone.utc).isoformat(),
        'profile': profile.model_dump(), 'profile_sha256': profile.fingerprint(), 'mode': args.mode,
        'worker_sha256': hashlib.sha256((ROOT / 'scripts/capacity/worker.py').read_bytes()).hexdigest(),
        'image_id': image_id, 'image_reference': args.image, 'source_revision': revision,
        'docker': {key: info.get(key) for key in ('ServerVersion', 'OSType', 'Architecture', 'CgroupVersion', 'MemTotal', 'NCPU')}}
    (destination / 'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    report = {key: manifest[key] for key in ('run_id', 'profile_sha256', 'mode', 'image_id', 'source_revision')}
    report['containers'] = {}
    report['production_verified'] = False
    containers, urls, latencies, errors = {}, {}, [], 0
    initial_oom = None
    min_available = None
    temporary_workspace = tempfile.TemporaryDirectory(prefix='crypto-capacity-')
    try:
        # First measure the Linux host through a small bounded, read-only helper.
        # It imports no application config and has no data/secret mounts.
        probe = json.loads(command('docker', 'run', '--rm', '--memory', '64m',
            '--memory-swap', '64m', '--cpus', '0.1', '--network', 'none', args.image,
            '/app/.venv/bin/python', '-c',
            'import json; from backend.utils.resource_diagnostics import read_effective_resources; print(json.dumps(read_effective_resources()))'))
        host = probe['host']
        profile.check_host(info['MemTotal'], host['memory_available_bytes'], info['NCPU'])
        initial_oom, min_available = host.get('oom_kill_counter'), host['memory_available_bytes']
        with nullcontext(temporary_workspace.name) as temporary:
            temporary = Path(temporary)
            frozen = temporary / 'verification'
            frozen.mkdir()
            (frozen / 'profile.json').write_text(json.dumps(profile.model_dump()), encoding='utf-8')
            (frozen / 'worker.py').write_bytes((ROOT / 'scripts/capacity/worker.py').read_bytes())
            for role in ('web', 'scheduler'):
                budget = getattr(profile, role)
                data = temporary / role
                data.mkdir()
                name = 'crypto-capacity-' + run_id.lower() + '-' + role
                args_run = ['docker', 'run', '-d', '--name', name,
                    '--memory', str(budget.memory_bytes), '--memory-swap', str(budget.memory_swap_total_bytes),
                    '--cpus', str(budget.cpu_cpus), '-p', '127.0.0.1::7860',
                    '--mount', f'type=bind,source={frozen},target=/verification,readonly',
                    '--mount', f'type=bind,source={data},target=/app/data',
                    '-e', f'VERIFICATION_ROLE={role}', '-e', 'DATA_DIR=/app/data',
                    '-e', f'RESOURCE_EXPECTED_MEMORY_BYTES={budget.memory_bytes}',
                    '-e', f'RESOURCE_EXPECTED_MEMORY_SWAP_TOTAL_BYTES={budget.memory_swap_total_bytes}',
                    '-e', f'RESOURCE_EXPECTED_CPU_CPUS={budget.cpu_cpus}',
                    '-e', 'TRADING_DB_PATH=/app/data/trading.sqlite',
                    '-e', 'CHAT_CHECKPOINT_DB=/app/data/chat.sqlite',
                    '-e', 'ADMIN_PASSWORD=isolated-verification', '-e', 'RUN_SCHEDULER_IN_WEB=' + ('true' if role == 'scheduler' else 'false'),
                    '-e', 'JWT_SECRET=' + uuid.uuid4().hex, '-e', 'CONFIG_MASTER_KEY=' + uuid.uuid4().hex,
                    '-e', 'CAPACITY_PROFILE_SHA256=' + profile.fingerprint()]
                for key, value in profile.application.items():
                    args_run += ['-e', f'{key}={value}']
                args_run += [args.image, '/app/.venv/bin/python', '/verification/worker.py']
                command(*args_run)
                containers[role] = name
                port = command('docker', 'port', name, '7860/tcp').split(':')[-1]
                urls[role] = 'http://127.0.0.1:' + port
            with ThreadPoolExecutor(max_workers=2) as pool:
                snapshots = dict(zip(urls, pool.map(wait_ready, urls.values())))
            first_counters = {role: value['resources']['resources']['memory']['events'].get('oom_kill')
                              for role, value in snapshots.items()}
            for phase in PHASES:
                print(f'{args.mode}: {phase}', flush=True)
                if phase == 'restart':
                    # Kill only task-created, already bounded test containers.
                    for role, name in containers.items():
                        command('docker', 'kill', '--signal', 'KILL', name)
                        command('docker', 'start', name)
                        port = command('docker', 'port', name, '7860/tcp').split(':')[-1]
                        urls[role] = 'http://127.0.0.1:' + port
                        wait_ready(urls[role])
                for url in urls.values():
                    request(url + '/verification/start/' + phase, post=True)
                deadline = time.monotonic() + profile.phase_seconds[phase] + 30
                done = set()
                while len(done) < 2 and time.monotonic() < deadline:
                    begin = time.perf_counter()
                    try:
                        if not request(urls['web'] + '/health').get('success'):
                            errors += 1
                        latencies.append((time.perf_counter() - begin) * 1000)
                    except Exception:
                        errors += 1
                    for role, url in urls.items():
                        try:
                            current = request(url + '/verification/result')
                        except Exception as exc:
                            raise RuntimeError(f'{role} phase={phase} result request: {type(exc).__name__}: {exc}') from exc
                        report['containers'].setdefault(role, {})['phases'] = current.get('phases', {})
                        if not current['running'] and phase in current['phases']:
                            done.add(role)
                        observed = current.get('resources', {}).get('resources', {}).get('host', {}).get('memory_available_bytes')
                        if observed is not None:
                            min_available = min(min_available, observed)
                    time.sleep(.2)
                if len(done) < 2:
                    raise RuntimeError(f'Phase {phase} did not complete in its declared bounded deadline')
            for role, url in urls.items():
                result = request(url + '/verification/result')
                inspect = json.loads(command('docker', 'inspect', '--format', '{{json .HostConfig}}', containers[role]))
                effective = result['resources']['resources']
                mem, cpu = effective['memory'], effective['cpu']
                if mem['limit_bytes'] != inspect['Memory'] or cpu['quota_cpus'] != inspect['NanoCpus'] / 1e9:
                    raise ValueError('Docker inspect and effective cgroup limits disagree')
                if mem['swap_limit_bytes'] is None or mem['swap_limit_bytes'] + mem['limit_bytes'] != inspect['MemorySwap']:
                    raise ValueError('Effective Docker memory+swap total does not match intended limits')
                phases = result['phases']
                aggregate = all(item['capacity']['peaks']['active'] <= item['capacity']['settings']['total_workers']
                    and item['capacity']['peaks']['queued'] <= item['capacity']['settings']['queue_limit']
                    and item['capacity']['peaks']['bytes'] <= item['capacity']['settings']['byte_limit'] for item in phases.values())
                retirement = phases['key_retirement']
                growth = max(0, retirement['rss_after_bytes'] - retirement['rss_before_bytes'])
                before_oom, after_oom = first_counters[role], mem['events'].get('oom_kill')
                report['containers'][role] = {'container_id': command('docker', 'inspect', '--format', '{{.Id}}', containers[role]),
                    'effective_limits': {'memory_bytes': mem['limit_bytes'],
                        'memory_swap_total_bytes': mem['limit_bytes'] + mem['swap_limit_bytes'], 'cpu_cpus': cpu['quota_cpus']},
                    'phases': phases, 'aggregate_limits_held': aggregate,
                    'critical_progress': result['critical'] > 0,
                    'retirement_growth_bytes': growth, 'retired_objects_reclaimed': retirement['catalogue']['entries'] == 0 and retirement['catalogue']['clients'] == 0,
                    'retirement_steady_growth_bytes': max(0, retirement['rss_after_bytes'] - retirement['retirement_mid_rss_bytes']) if retirement.get('retirement_mid_rss_bytes') is not None else None,
                    'retired_keys_seen': retirement.get('retired_keys_seen', 0),
                    'restart_continuity': result['restart_continuity'],
                    'financial_writes_replayed': result['writes_replayed'], 'unattempted_tail_executed': result['tail_executed'],
                    'busy_requests': result['busy'], 'oom_kill_delta': after_oom - before_oom if None not in (before_oom, after_oom) else None,
                    'final_resources': effective}
                evidence = temporary / role / 'resource_evidence'
                if evidence.exists():
                    for path in evidence.glob('*.jsonl*'):
                        (destination / (role + '-' + path.name)).write_bytes(path.read_bytes())
            final_host_oom = request(urls['web'] + '/verification/result')['resources']['resources']['host'].get('oom_kill_counter')
            report['host_oom_kill_delta'] = final_host_oom - initial_oom if None not in (initial_oom, final_host_oom) else None
    except Exception as exc:
        report['verification_error'] = str(exc)[:2048]
    finally:
        for role, name in containers.items():
            # Capture process exit/OOM/error state before removal even if HTTP
            # polling failed. A broken connection alone cannot establish OOM.
            try:
                container_state = json.loads(command('docker', 'inspect', '--format', '{{json .State}}', name))
                report['containers'].setdefault(role, {})['docker_state'] = container_state
                (destination / f'{role}-docker-state.json').write_text(json.dumps(container_state, indent=2), encoding='utf-8')
                if container_state.get('Running') and report.get('verification_error'):
                    command('docker', 'kill', '--signal', 'USR1', name)
                    time.sleep(.2)
            except Exception as exc:
                report['containers'].setdefault(role, {})['docker_state_capture_error'] = str(exc)[:1024]
            evidence = Path(temporary_workspace.name) / role / 'resource_evidence'
            if evidence.exists():
                for path in evidence.glob('*.jsonl*'):
                    (destination / (role + '-' + path.name)).write_bytes(path.read_bytes())
            try:
                log = subprocess.run(['docker', 'logs', '--tail', '2000', name],
                    capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=10)
                (destination / f'{role}-container.log').write_text((log.stdout + log.stderr)[-256*1024:], encoding='utf-8')
            except Exception:
                pass
            try:
                command('docker', 'rm', '-f', name)
            except Exception:
                pass
        temporary_workspace.cleanup()
    ordered = sorted(latencies)
    report['web_latency'] = {'samples': len(ordered), 'errors': errors,
        'p95_ms': ordered[min(len(ordered) - 1, int(len(ordered) * .95))] if ordered else None,
        'max_ms': max(ordered) if ordered else None}
    report['host_min_available_bytes'] = min_available
    # An unavailable metric fails explicitly, never as fake zero or healthy.
    if report['web_latency']['p95_ms'] is None:
        report['web_latency'].update(p95_ms=1e30, max_ms=1e30)
    if min_available is None:
        report['host_min_available_bytes'] = -1
    verdict = assess_report(profile, report)
    if report.get('verification_error'):
        verdict['verdict'] = 'failed'
        verdict['failures'].append('verification_error')
    report.update(verdict)
    report['finished_at_utc'] = datetime.now(timezone.utc).isoformat()
    (destination / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps({'report': str(destination / 'report.json'), **verdict}, ensure_ascii=False), flush=True)
    if verdict['verdict'] == 'failed':
        sys.exit(1)


if __name__ == '__main__':
    main()
