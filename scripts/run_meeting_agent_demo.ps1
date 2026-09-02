[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot "backend\.venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    throw "Backend virtual environment is missing: $python"
}

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)][string]$WorkingDirectory,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )
    Push-Location $WorkingDirectory
    try {
        & $python @Arguments
        if ($LASTEXITCODE -ne 0) {
            throw "Command failed with exit code ${LASTEXITCODE}: $($Arguments -join ' ')"
        }
    }
    finally {
        Pop-Location
    }
}

$productReport = Join-Path $projectRoot "reports\meeting-agent-product-v1"
$demoReport = Join-Path $projectRoot "reports\meeting-agent-demo"

Invoke-Checked -WorkingDirectory (Join-Path $projectRoot "backend") -Arguments @(
    "-B", "-m", "app.evals.cli",
    "--suite", "evals\cases\meeting_agent_product_v1.jsonl",
    "--provider", "scripted",
    "--trials", "3",
    "--seed", "20260901",
    "--require-complete-trace",
    "--output-dir", "..\reports\meeting-agent-product-v1"
)
Invoke-Checked -WorkingDirectory $projectRoot -Arguments @(
    "-B", "scripts\run_meeting_agent_full_chain_eval.py",
    "--output-dir", $productReport
)
Invoke-Checked -WorkingDirectory $projectRoot -Arguments @(
    "-B", "scripts\run_meeting_agent_demo.py",
    "--output-dir", $demoReport
)

$product = Get-Content -Raw (Join-Path $productReport "summary.json") | ConvertFrom-Json
$fullChain = Get-Content -Raw (Join-Path $productReport "full-chain-summary.json") | ConvertFrom-Json
$agentTraces = @(Get-ChildItem -Path (Join-Path $productReport "traces") -Recurse -File -Filter "*.json")
$fullChainTraces = @(Get-ChildItem -Path (Join-Path $productReport "full-chain-traces") -File -Filter "*.json")
if ($agentTraces.Count -ne 36) {
    throw "Expected 36 Agent traces, found $($agentTraces.Count)"
}
if ($fullChainTraces.Count -ne 2) {
    throw "Expected 2 full-chain traces, found $($fullChainTraces.Count)"
}

$productGates = @($product.metrics | Where-Object {
    $_.gate_class -eq "safe" -or $_.gate_class -eq "quality"
})
$allGates = $productGates + @($fullChain.metrics)
$passedGates = @($allGates | Where-Object { $_.result -eq "PASS" }).Count
if ($passedGates -ne $allGates.Count) {
    throw "Only $passedGates/$($allGates.Count) hard gates passed"
}

Write-Host "overall_result=$($product.overall_result) full_chain=$($fullChain.overall_result)"
Write-Host "hard_gates=$passedGates/$($allGates.Count)"
Write-Host "agent_traces=$($agentTraces.Count)"
$agentTraces | Sort-Object FullName | ForEach-Object { Write-Host "agent_trace=$($_.FullName)" }
Write-Host "full_chain_traces=$($fullChainTraces.Count)"
$fullChainTraces | Sort-Object FullName | ForEach-Object { Write-Host "full_chain_trace=$($_.FullName)" }
