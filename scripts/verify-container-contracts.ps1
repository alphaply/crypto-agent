param(
    [Parameter(Mandatory=$true)][string]$Image,
    [Parameter(Mandatory=$true)][string]$CapacityProfile,
    [string]$Output = 'data/container-contract-smoke.txt'
)
$ErrorActionPreference = 'Stop'
$repository = Split-Path $PSScriptRoot -Parent
Push-Location $repository
try {
    $settings = Get-Content -LiteralPath $CapacityProfile -Raw | ConvertFrom-Json
    $dependencies = (Resolve-Path -LiteralPath '.venv/Lib/site-packages').Path
    $runtime = Join-Path $repository 'data/pytest-container-runtime'
    New-Item -ItemType Directory -Path $runtime -Force | Out-Null
    Get-ChildItem -LiteralPath $dependencies -Directory | Where-Object {
        $_.Name -in @('pytest','_pytest','pluggy','iniconfig') -or $_.Name -match '^(pytest|pluggy|iniconfig)-.*\.dist-info$'
    } | ForEach-Object { Copy-Item -LiteralPath $_.FullName -Destination $runtime -Recurse -Force }
    Copy-Item -LiteralPath (Join-Path $dependencies 'py.py') -Destination $runtime -Force
    $tests = (Resolve-Path -LiteralPath 'tests').Path
    $arguments = @('run','--rm','--memory',[string]$settings.web.memory_bytes,
        '--memory-swap',[string]$settings.web.memory_swap_total_bytes,'--cpus',[string]$settings.web.cpu_cpus,
        '--network','none','--mount',"type=bind,source=$tests,target=/app/tests,readonly",
        '--mount',"type=bind,source=$runtime,target=/verification/python,readonly",
        '-e','PYTHONPATH=/verification/python:/app','-e','PYTHONDONTWRITEBYTECODE=1','-e','RUN_SCHEDULER_IN_WEB=false',
        '-e',('RESOURCE_EXPECTED_MEMORY_BYTES='+$settings.web.memory_bytes),
        '-e',('RESOURCE_EXPECTED_MEMORY_SWAP_TOTAL_BYTES='+$settings.web.memory_swap_total_bytes),
        '-e',('RESOURCE_EXPECTED_CPU_CPUS='+$settings.web.cpu_cpus))
    foreach ($setting in $settings.application.PSObject.Properties) {
        $arguments += @('-e',($setting.Name+'='+[string]$setting.Value))
    }
    $arguments += @($Image,'/app/.venv/bin/python','-m','pytest',
        'tests/test_paper_binance_scope.py','tests/test_single_call_execution.py',
        'tests/test_trade_audit_pages.py','tests/test_mcp_canonical_catalog.py','-q','-p','no:cacheprovider')
    & docker @arguments > $Output 2>&1
    $result = $LASTEXITCODE
    Get-Content -LiteralPath $Output -Tail 12
    if ($result -ne 0) { throw "Isolated contract smoke failed: exit=$result; $Output" }
} finally { Pop-Location }
