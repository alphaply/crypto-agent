param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^\d{4}-\d{2}-\d{2}-[a-z0-9][a-z0-9-]*$')]
    [string]$ArchiveName,
    [string]$CapacityProfile = (Join-Path $PSScriptRoot 'capacity/local-verified-v12.json'),
    [switch]$IsolatedCheckout
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$repoRoot = Split-Path $PSScriptRoot -Parent
$imageRepository = 'alphaply712/crypto-agent'
$stage = 'preflight'
$smokeCreated = $false
$smokeName = 'crypto-agent-release-' + [Guid]::NewGuid().ToString('N')
$gitPushed = $false
$commitImagePushed = $false
$latestPushed = $false

function Invoke-Checked {
    param([string]$Binary, [string[]]$Arguments)
    & $Binary @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Binary failed with exit code $LASTEXITCODE"
    }
}

function Read-Git {
    param([string[]]$Arguments)
    $result = & git @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "git failed with exit code $LASTEXITCODE"
    }
    return ($result -join "`n").Trim()
}

Push-Location $repoRoot
try {
    $branch = Read-Git @('branch', '--show-current')
    if ($branch -ne 'beta') {
        if (-not $IsolatedCheckout -or $branch -or
            (Read-Git @('rev-parse', 'HEAD')) -ne (Read-Git @('rev-parse', 'refs/heads/beta'))) {
            throw 'Release requires beta or an explicitly isolated detached checkout of the exact beta commit.'
        }
    }
    Invoke-Checked git @('diff', '--quiet', 'HEAD', '--')
    $buildChanges = Read-Git @('status', '--porcelain', '--untracked-files=all', '--',
        'backend', 'frontend', 'pyproject.toml', 'uv.lock', 'README.md',
        'Dockerfile', 'Dockerfile.prebuilt', '.dockerignore')
    if ($buildChanges) { throw 'Commit or isolate changes in image inputs before release.' }
    $archivePath = 'openspec/changes/archive/' + $ArchiveName
    $archiveEntry = Read-Git @('ls-tree', '-d', 'HEAD', '--', $archivePath)
    if (-not $archiveEntry) { throw 'The selected archive must exist in the release commit.' }
    $commit = Read-Git @('rev-parse', 'HEAD')
    $shortCommit = Read-Git @('rev-parse', '--short', 'HEAD')
    $commitImage = $imageRepository + ':git-' + $shortCommit
    $latestImage = $imageRepository + ':latest'
    if (-not (Test-Path -LiteralPath $CapacityProfile)) { throw 'The frozen verified capacity profile is required.' }
    $capacityConfiguration = Get-Content -LiteralPath $CapacityProfile -Raw | ConvertFrom-Json
    Invoke-Checked docker @('info', '--format', '{{.ServerVersion}}')
    Invoke-Checked openspec @('validate', '--specs', '--strict')

    $stage = 'frontend build'
    # The Windows npm.ps1 shim is incompatible with inherited StrictMode.
    $npmExecutable = if (Get-Command npm.cmd -ErrorAction SilentlyContinue) { 'npm.cmd' } else { 'npm' }
    Invoke-Checked $npmExecutable @('ci', '--prefix', 'frontend')
    Invoke-Checked $npmExecutable @('run', 'build', '--prefix', 'frontend')
    $stage = 'Docker build'
    Invoke-Checked docker @('build', '-f', 'Dockerfile.prebuilt',
        '--build-arg', "IMAGE_REVISION=$commit",
        '--label', "org.opencontainers.image.revision=$commit",
        '-t', $commitImage, '.')

    $stage = 'isolated smoke test'
    $dockerHost = (& docker info --format '{{json .}}' | ConvertFrom-Json)
    if ($LASTEXITCODE -ne 0) { throw 'Cannot read Docker host capacity.' }
    $hostProbeArguments = @('run', '--rm', '--memory', '64m', '--memory-swap', '64m', '--cpus', '0.1', '--network', 'none',
        $commitImage, '/app/.venv/bin/python', '-c',
        'import json; from backend.utils.resource_diagnostics import read_effective_resources; print(json.dumps(read_effective_resources()))')
    $hostMetrics = (& docker @hostProbeArguments | ConvertFrom-Json)
    if ($LASTEXITCODE -ne 0) { throw 'Cannot measure Docker host headroom.' }
    Invoke-Checked uv @('run', 'python', 'scripts/capacity/check-profile.py', '--profile', $CapacityProfile,
        '--host-total', [string]$dockerHost.MemTotal,
        '--host-available', [string]$hostMetrics.host.memory_available_bytes,
        '--host-cpus', [string]$dockerHost.NCPU)
    $smokeArguments = @('run', '-d', '--name', $smokeName,
        '--memory', [string]$capacityConfiguration.web.memory_bytes,
        '--memory-swap', [string]$capacityConfiguration.web.memory_swap_total_bytes,
        '--cpus', [string]$capacityConfiguration.web.cpu_cpus,
        '-e', 'RUN_SCHEDULER_IN_WEB=false',
        '-e', ('RESOURCE_EXPECTED_MEMORY_BYTES=' + $capacityConfiguration.web.memory_bytes),
        '-e', ('RESOURCE_EXPECTED_MEMORY_SWAP_TOTAL_BYTES=' + $capacityConfiguration.web.memory_swap_total_bytes),
        '-e', ('RESOURCE_EXPECTED_CPU_CPUS=' + $capacityConfiguration.web.cpu_cpus),
        '-e', 'ADMIN_PASSWORD=archive-smoke-password',
        '-e', ('JWT_SECRET=' + [Guid]::NewGuid().ToString('N')),
        '-e', ('CONFIG_MASTER_KEY=' + [Guid]::NewGuid().ToString('N')))
    foreach ($setting in $capacityConfiguration.application.PSObject.Properties) {
        $smokeArguments += @('-e', ($setting.Name + '=' + [string]$setting.Value))
    }
    $smokeArguments += $commitImage
    Invoke-Checked docker $smokeArguments
    $smokeCreated = $true
    $ready = $false
    $deadline = (Get-Date).AddSeconds(90)
    do {
        try {
            $probe = & docker exec $smokeName python -c "import json, urllib.request; health=json.load(urllib.request.urlopen('http://127.0.0.1:7860/health', timeout=3)); assert health['success'] and not health['scheduler']['run_in_web'], health; page=urllib.request.urlopen('http://127.0.0.1:7860/', timeout=3).read(); assert b'<html' in page.lower(); print('health and frontend OK; scheduler disabled')" 2>&1
            $probeExit = $LASTEXITCODE
        }
        catch { $probeExit = 1 }
        if ($probeExit -eq 0) {
            Write-Host ($probe -join "`n")
            $ready = $true
            break
        }
        Start-Sleep -Seconds 3
    } while ((Get-Date) -lt $deadline)
    if (-not $ready) {
        & docker logs --tail 60 $smokeName
        throw 'Smoke test did not become healthy within 90 seconds.'
    }
    $limits = (& docker inspect $smokeName --format '{{json .HostConfig}}' | ConvertFrom-Json)
    if ($LASTEXITCODE -ne 0 -or $limits.Memory -ne $capacityConfiguration.web.memory_bytes -or
        $limits.MemorySwap -ne $capacityConfiguration.web.memory_swap_total_bytes -or
        $limits.NanoCpus -ne ($capacityConfiguration.web.cpu_cpus * 1000000000)) {
        throw 'Smoke container resource limits do not match the frozen profile.'
    }
    Invoke-Checked docker @('exec', $smokeName, '/app/.venv/bin/python', '-c',
        'import sys; from backend.utils.resource_diagnostics import read_effective_resources; r=read_effective_resources(); m=r[''memory'']; c=r[''cpu'']; assert m[''limit_bytes'']==int(sys.argv[1]); assert m[''swap_limit_bytes''] is not None and m[''swap_limit_bytes'']+m[''limit_bytes'']==int(sys.argv[2]); assert c[''quota_cpus'']==float(sys.argv[3]); print(''effective cgroup memory/swap/CPU match'')',
        [string]$capacityConfiguration.web.memory_bytes, [string]$capacityConfiguration.web.memory_swap_total_bytes, [string]$capacityConfiguration.web.cpu_cpus)
    Invoke-Checked docker @('rm', '-f', $smokeName)
    $smokeCreated = $false

    $stage = 'dual-container capacity acceptance'
    Invoke-Checked uv @('run', 'python', 'scripts/verify-runtime-capacity.py', '--image', $commitImage,
        '--profile', $CapacityProfile, '--mode', 'acceptance', '--output', 'data/capacity-reports')

    $stage = 'source verification before publication'
    Invoke-Checked git @('diff', '--quiet', 'HEAD', '--')
    if ((Read-Git @('rev-parse', 'HEAD')) -ne $commit) { throw 'HEAD changed during release.' }
    $buildChanges = Read-Git @('status', '--porcelain', '--untracked-files=all', '--',
        'backend', 'frontend', 'pyproject.toml', 'uv.lock', 'README.md',
        'Dockerfile', 'Dockerfile.prebuilt', '.dockerignore')
    if ($buildChanges) { throw 'Image inputs changed during release.' }
    $stage = 'Git push'
    Invoke-Checked git @('push', 'origin', 'HEAD:refs/heads/beta')
    $gitPushed = $true
    $stage = 'commit image push'
    Invoke-Checked docker @('push', $commitImage)
    $commitImagePushed = $true
    $stage = 'latest image push'
    Invoke-Checked docker @('tag', $commitImage, $latestImage)
    Invoke-Checked docker @('push', $latestImage)
    $latestPushed = $true
    $stage = 'release report'
    $digests = & docker image inspect $commitImage --format '{{json .RepoDigests}}'
    if ($LASTEXITCODE -ne 0) { throw 'Cannot read the published image digest.' }
    Write-Host "Archive: $archivePath"
    Write-Host "Git commit: $commit"
    Write-Host "Images: $commitImage, $latestImage"
    Write-Host "Digests: $digests"
    Write-Host 'Release complete. Production containers have not been restarted.'
}
catch {
    Write-Host "Release stopped at: $stage"
    Write-Host "Published: git=$gitPushed, commit-image=$commitImagePushed, latest=$latestPushed"
    throw
}
finally {
    if ($smokeCreated) { & docker rm -f $smokeName }
    Pop-Location
}
