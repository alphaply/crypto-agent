param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^\d{4}-\d{2}-\d{2}-[a-z0-9][a-z0-9-]*$')]
    [string]$ArchiveName
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
    if ($branch -ne 'beta') { throw 'Release requires the beta branch.' }
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
    Invoke-Checked docker @('info', '--format', '{{.ServerVersion}}')
    Invoke-Checked openspec @('validate', '--specs', '--strict')

    $stage = 'frontend build'
    # The Windows npm.ps1 shim is incompatible with inherited StrictMode.
    $npmExecutable = if (Get-Command npm.cmd -ErrorAction SilentlyContinue) { 'npm.cmd' } else { 'npm' }
    Invoke-Checked $npmExecutable @('ci', '--prefix', 'frontend')
    Invoke-Checked $npmExecutable @('run', 'build', '--prefix', 'frontend')
    $stage = 'Docker build'
    Invoke-Checked docker @('build', '-f', 'Dockerfile.prebuilt',
        '--label', "org.opencontainers.image.revision=$commit",
        '-t', $commitImage, '.')

    $stage = 'isolated smoke test'
    Invoke-Checked docker @('run', '-d', '--name', $smokeName,
        '-e', 'RUN_SCHEDULER_IN_WEB=false',
        '-e', 'ADMIN_PASSWORD=archive-smoke-password',
        '-e', ('JWT_SECRET=' + [Guid]::NewGuid().ToString('N')),
        '-e', ('CONFIG_MASTER_KEY=' + [Guid]::NewGuid().ToString('N')),
        $commitImage)
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
    Invoke-Checked docker @('rm', '-f', $smokeName)
    $smokeCreated = $false

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
