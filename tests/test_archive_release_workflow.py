"""Exercise publication ordering without invoking Git, Docker, npm or OpenSpec."""

import json
from pathlib import Path
import shutil
import subprocess
import tempfile

import pytest


POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")
SCRIPT = Path(__file__).resolve().parents[1] / "scripts/release-archived-change.ps1"
pytestmark = pytest.mark.skipif(not POWERSHELL, reason="PowerShell is not installed")


def run_release(scenario):
    script_path = str(SCRIPT).replace("'", "''")
    harness = r"""
$global:commands = @()
$global:probes = 0
function Mock-Command {
    param([string]$Name, [object[]]$Values)
    $line = $Name + ' ' + ($Values -join ' ')
    $global:commands += $line
    $global:LASTEXITCODE = 0
    if ($Name -eq 'git') {
        switch ($Values[0]) {
            'branch' { 'beta' }
            'ls-tree' { '040000 tree archive openspec/changes/archive/2026-10-13-example' }
            'rev-parse' { if ($Values -contains '--short') { 'abc1234' } else { 'abc1234full' } }
            'status' { if ($scenario -eq 'dirty_inputs') { '?? backend/extra.py' } }
            'push' { if ($scenario -eq 'git_failure') { $global:LASTEXITCODE = 1 } }
        }
    }
    if ($Name -eq 'npm' -and $Values[0] -eq 'run' -and $scenario -eq 'frontend_failure') {
        $global:LASTEXITCODE = 1
    }
    if ($Name -eq 'uv') {
        if ($Values -contains 'scripts/capacity/check-profile.py' -and $scenario -eq 'host_headroom_failure') {
            $global:LASTEXITCODE = 1
        }
        if ($Values -contains 'scripts/verify-runtime-capacity.py' -and $scenario -eq 'capacity_failure') {
            $global:LASTEXITCODE = 1
        }
    }
    if ($Name -eq 'docker') {
        if ($Values[0] -eq 'info') {
            if ($Values -contains '{{json .}}') { '{"MemTotal":34359738368,"NCPU":32}' }
            else { '28.4.0' }
        }
        if ($Values[0] -eq 'run') {
            if ($Values -contains '--rm') { '{"host":{"memory_available_bytes":25769803776}}' }
            else { 'isolated-smoke-id' }
        }
        if ($Values[0] -eq 'build' -and $scenario -eq 'build_failure') { $global:LASTEXITCODE = 1 }
        if ($Values[0] -eq 'exec') {
            if (($Values -join ' ') -like '*urllib.request*') {
                $global:probes++
                if ($scenario -eq 'startup_retry' -and $global:probes -eq 1) { throw 'Container is starting' }
                'health and frontend OK; scheduler disabled'
            }
            else {
                if ($scenario -eq 'effective_limits_failure') { $global:LASTEXITCODE = 1 }
                'effective cgroup memory/swap/CPU match'
            }
        }
        if ($Values[0] -eq 'inspect') {
            if ($scenario -eq 'inspect_limits_failure') { '{"Memory":0,"MemorySwap":0,"NanoCpus":0}' }
            else { '{"Memory":2147483648,"MemorySwap":2415919104,"NanoCpus":1000000000}' }
        }
        if ($Values[0] -eq 'push' -and $Values[1] -like '*:git-*' -and $scenario -eq 'commit_image_failure') {
            $global:LASTEXITCODE = 1
        }
        if ($Values[0] -eq 'push' -and $Values[1] -like '*:latest' -and $scenario -eq 'latest_failure') {
            $global:LASTEXITCODE = 1
        }
        if ($Values[0] -eq 'image') { '["alphaply712/crypto-agent@sha256:example"]' }
    }
}
function git { Mock-Command git $args }
function npm { Mock-Command npm $args }
function npm.cmd { Mock-Command npm $args }
function docker { Mock-Command docker $args }
function openspec { Mock-Command openspec $args }
function uv { Mock-Command uv $args }
function Start-Sleep {}
$failed = $false
try { & '__SCRIPT__' -ArchiveName '2026-10-13-example' -CapacityProfile '__PROFILE__' }
catch { $failed = $true }
Write-Output ('TEST_RESULT=' + (@{failed=$failed; commands=$global:commands} | ConvertTo-Json -Compress))
""".replace("__SCRIPT__", script_path)
    with tempfile.TemporaryDirectory(prefix='crypto-release-ordering-') as directory:
        profile = Path(directory) / 'verified-fixture.json'
        profile.write_text(json.dumps({'web': {'memory_bytes': 2147483648,
                                             'memory_swap_total_bytes': 2415919104, 'cpu_cpus': 1},
                                       'application': {'RUNTIME_CAPACITY_TOTAL_WORKERS': '6'}}), encoding='utf-8')
        harness = harness.replace('__PROFILE__', str(profile).replace("'", "''"))
        result = subprocess.run(
            [POWERSHELL, "-NoProfile", "-Command", f"$scenario='{scenario}'\n{harness}"],
            capture_output=True,
            text=True,
            timeout=25,
            check=True,
        )
    payload = next(line for line in result.stdout.splitlines() if line.startswith("TEST_RESULT="))
    return json.loads(payload.removeprefix("TEST_RESULT="))


@pytest.mark.parametrize("scenario", ["success", "startup_retry"])
def test_release_checks_build_and_smoke_before_publishing(scenario):
    result = run_release(scenario)
    assert not result["failed"]
    commands = result["commands"]
    starts = [command.split()[0:2] for command in commands]
    assert starts.index(["npm", "run"]) < starts.index(["docker", "build"])
    assert starts.index(["docker", "exec"]) < starts.index(["git", "push"])
    assert starts.index(["docker", "rm"]) < starts.index(["git", "push"])
    assert starts.index(["git", "push"]) < starts.index(["docker", "push"])
    pushes = [command for command in commands if command.startswith("docker push")]
    assert pushes == [
        "docker push alphaply712/crypto-agent:git-abc1234",
        "docker push alphaply712/crypto-agent:latest",
    ]
    smoke = next(command for command in commands if command.startswith("docker run") and 'RUN_SCHEDULER_IN_WEB=false' in command)
    assert "RUN_SCHEDULER_IN_WEB=false" in smoke
    assert "--mount" not in smoke and " -v " not in smoke
    assert '--memory 2147483648' in smoke and '--memory-swap 2415919104' in smoke and '--cpus 1' in smoke
    host_validation = next(index for index, command in enumerate(commands) if command.startswith('uv ') and 'check-profile.py' in command)
    smoke_start = commands.index(smoke)
    inspect = next(index for index, command in enumerate(commands) if command.startswith('docker inspect'))
    cgroup = next(index for index, command in enumerate(commands) if command.startswith('docker exec') and 'read_effective_resources' in command)
    acceptance = next(index for index, command in enumerate(commands) if command.startswith('uv ') and 'verify-runtime-capacity.py' in command)
    git_push = next(index for index, command in enumerate(commands) if command.startswith('git push'))
    assert host_validation < smoke_start < inspect < cgroup < acceptance < git_push
    assert "org.opencontainers.image.revision=abc1234full" in next(
        command for command in commands if command.startswith("docker build")
    )
    if scenario == "startup_retry":
        assert starts.count(["docker", "exec"]) == 3


@pytest.mark.parametrize(
    "scenario,git_pushed,image_push_attempts",
    [
        ("dirty_inputs", False, 0),
        ("frontend_failure", False, 0),
        ("build_failure", False, 0),
        ("host_headroom_failure", False, 0),
        ("inspect_limits_failure", False, 0),
        ("effective_limits_failure", False, 0),
        ("capacity_failure", False, 0),
        ("git_failure", True, 0),
        ("commit_image_failure", True, 1),
        ("latest_failure", True, 2),
    ],
)
def test_release_stops_dependent_publication_on_failure(scenario, git_pushed, image_push_attempts):
    result = run_release(scenario)
    assert result["failed"]
    commands = result["commands"]
    assert any(command.startswith("git push") for command in commands) == git_pushed
    assert sum(command.startswith("docker push") for command in commands) == image_push_attempts
    if scenario in {"dirty_inputs", "frontend_failure", "build_failure"}:
        assert not any(command.startswith("docker run") for command in commands)
