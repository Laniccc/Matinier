[CmdletBinding()]
param(
    [switch]$CheckOnly,
    [switch]$SkipMigration,
    [switch]$Demo
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

# Some sandboxed launchers inject both Path and PATH into the Windows process
# environment. Start-Process treats keys case-insensitively and fails while
# copying that invalid pair, so retain one canonical entry before spawning.
$processEnvironment = [Environment]::GetEnvironmentVariables("Process")
$pathKeys = @(
    $processEnvironment.Keys |
        Where-Object { [string]$_ -ieq "Path" }
)
if ($pathKeys.Count -gt 1) {
    $pathValue = [string]$processEnvironment[$pathKeys[0]]
    foreach ($pathKey in $pathKeys) {
        [Environment]::SetEnvironmentVariable(
            [string]$pathKey,
            $null,
            "Process"
        )
    }
    [Environment]::SetEnvironmentVariable("Path", $pathValue, "Process")
}

$projectRoot = Split-Path -Parent $PSScriptRoot
$backendRoot = Join-Path $projectRoot "backend"
$frontendRoot = Join-Path $projectRoot "frontend"
$environmentFile = Join-Path $projectRoot ".env"
$python = Join-Path $backendRoot ".venv\Scripts\python.exe"
$pnpmCommand = Get-Command pnpm.cmd -ErrorAction SilentlyContinue
if ($null -eq $pnpmCommand) {
    $pnpmCommand = Get-Command pnpm -ErrorAction SilentlyContinue
}
$dockerCommand = $null
if ($Demo) {
    $dockerCommand = Get-Command docker.exe -ErrorAction SilentlyContinue
    if ($null -eq $dockerCommand) {
        $dockerCommand = Get-Command docker -ErrorAction SilentlyContinue
    }
}

if (-not (Test-Path -LiteralPath $environmentFile -PathType Leaf)) {
    throw "Missing $environmentFile. Copy .env.example to .env and configure it."
}
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Missing backend Python environment: $python. Run uv sync in backend/."
}
if ($null -eq $pnpmCommand) {
    throw "pnpm was not found on PATH."
}
if (-not (Test-Path -LiteralPath (Join-Path $frontendRoot "node_modules"))) {
    throw "Missing frontend/node_modules. Run pnpm install in frontend/."
}
if ($Demo -and $null -eq $dockerCommand) {
    throw "Docker CLI was not found. Install and start Docker Desktop."
}

Push-Location $backendRoot
try {
    & $python -c "from app.settings import Settings; s=Settings(); print('Configuration: ok'); print('Database: ' + s.database_location)"
    if ($LASTEXITCODE -ne 0) {
        throw "Application settings validation failed."
    }
}
finally {
    Pop-Location
}

$persistentLogRoot = [string](& $python -c "import sys; sys.path.insert(0, r'$backendRoot'); from app.settings import Settings; print(Settings().logs_dir)")
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($persistentLogRoot)) {
    throw "Persistent log directory could not be resolved."
}

$apiArguments = @("-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000")
$workerArguments = @("-m", "app.worker.entrypoint", "dev")
$frontendArguments = @("dev", "--hostname", "127.0.0.1", "--port", "3000")

Write-Host "FastAPI: $python $($apiArguments -join ' ')"
Write-Host "Worker: $python $($workerArguments -join ' ')"
Write-Host "Frontend: $($pnpmCommand.Source) $($frontendArguments -join ' ')"
Write-Host "Replay: $python $projectRoot\scripts\run_demo_replay.py --file <audio.wav> --session-id <session-id>"
if ($Demo) {
    Write-Host "Demo: Docker LiveKit + http://127.0.0.1:3000/"
}

if ($CheckOnly) {
    Write-Host "Check-only passed. No migration or process was started."
    exit 0
}

if (-not $SkipMigration) {
    Push-Location $backendRoot
    try {
        & $python -m alembic upgrade head
        if ($LASTEXITCODE -ne 0) {
            throw "Alembic migration failed."
        }
    }
    finally {
        Pop-Location
    }
}

$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$logRoot = Join-Path $persistentLogRoot "dev-$timestamp"
# Per-process files: api.stdout.log, worker.stdout.log, frontend.stdout.log
New-Item -ItemType Directory -Path $logRoot -Force | Out-Null
$pidFile = Join-Path $logRoot "processes.json"

function Start-DevProcess {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$WorkingDirectory
    )

    $stdoutPath = Join-Path $logRoot "$Name.stdout.log"
    $stderrPath = Join-Path $logRoot "$Name.stderr.log"
    $process = Start-Process `
        -FilePath $FilePath `
        -ArgumentList $Arguments `
        -WorkingDirectory $WorkingDirectory `
        -WindowStyle Hidden `
        -RedirectStandardOutput $stdoutPath `
        -RedirectStandardError $stderrPath `
        -PassThru
    Write-Host "$Name started PID=$($process.Id)"
    return [pscustomobject]@{
        Name = $Name
        Process = $process
        Stdout = $stdoutPath
        Stderr = $stderrPath
    }
}

function Wait-WorkerRegistration {
    param(
        [Parameter(Mandatory = $true)]$WorkerProcess,
        [Parameter(Mandatory = $true)][string[]]$LogPaths,
        [double]$TimeoutSeconds = 20
    )

    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        $WorkerProcess.Refresh()
        if ($WorkerProcess.HasExited) {
            throw "Worker exited before registering. Review its logs."
        }
        foreach ($logPath in $LogPaths) {
            if (
                (Test-Path -LiteralPath $logPath -PathType Leaf) -and
                (Select-String -LiteralPath $logPath -SimpleMatch `
                    "registered worker" -Quiet)
            ) {
                Write-Host "Worker registration confirmed."
                return
            }
        }
        Start-Sleep -Milliseconds 200
    }
    throw "Worker did not register within $TimeoutSeconds seconds."
}

function Stop-ExactProcessTree {
    param([Parameter(Mandatory = $true)][int]$RootProcessId)

    $children = @(
        Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
            Where-Object { $_.ParentProcessId -eq $RootProcessId } |
            Select-Object -ExpandProperty ProcessId
    )
    foreach ($childProcessId in $children) {
        Stop-ExactProcessTree -RootProcessId $childProcessId
    }
    if (Get-Process -Id $RootProcessId -ErrorAction SilentlyContinue) {
        Stop-Process -Id $RootProcessId -Force -ErrorAction SilentlyContinue
    }
}

function Test-DockerEngine {
    # A stopped Docker engine writes a diagnostic to stderr. With the script's
    # ErrorActionPreference=Stop, PowerShell may promote that expected native
    # stderr into NativeCommandError before we can inspect LASTEXITCODE.
    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        & $dockerCommand.Source info --format "{{.ServerVersion}}" 2>&1 |
            Out-Null
        return $LASTEXITCODE -eq 0
    }
    catch {
        return $false
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
}

function Start-DockerDesktopEngine {
    $previousErrorActionPreference = $ErrorActionPreference
    $messages = @()
    $exitCode = 1
    try {
        $ErrorActionPreference = "Continue"
        $messages = @(& $dockerCommand.Source desktop start 2>&1)
        $exitCode = $LASTEXITCODE
    }
    catch {
        $messages += $_.Exception.Message
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }

    foreach ($message in $messages) {
        Write-Host $message
    }
    if ($exitCode -eq 0) {
        return $true
    }

    # Older or partially initialized Docker Desktop CLI builds may not support
    # `docker desktop start`. Fall back to the installed desktop executable.
    $desktopExecutable = Join-Path `
        $env:ProgramFiles `
        "Docker\Docker\Docker Desktop.exe"
    if (-not (Test-Path -LiteralPath $desktopExecutable -PathType Leaf)) {
        return $false
    }
    Start-Process `
        -FilePath $desktopExecutable `
        -WindowStyle Hidden
    return $true
}

function Wait-DockerEngine {
    param([int]$TimeoutSeconds = 120)

    if (Test-DockerEngine) {
        return
    }

    Write-Host "Docker engine is not ready. Running: docker desktop start"
    if (-not (Start-DockerDesktopEngine)) {
        throw "Docker Desktop could not be started."
    }

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-DockerEngine) {
            Write-Host "Docker engine is ready."
            return
        }
        Start-Sleep -Seconds 1
    }
    throw "Docker engine did not become ready within $TimeoutSeconds seconds."
}

function Wait-HttpEndpoint {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Url,
        [int]$TimeoutSeconds = 90
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $response = Invoke-WebRequest `
                -UseBasicParsing `
                -Uri $Url `
                -TimeoutSec 2
            if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 400) {
                Write-Host "$Name is ready: $Url"
                return
            }
        }
        catch {
            # The supervised process may still be starting.
        }
        Start-Sleep -Milliseconds 500
    }
    throw "$Name did not become ready within $TimeoutSeconds seconds: $Url"
}

$children = @()
$demoLiveKitOwned = $false
try {
    if ($Demo) {
        Wait-DockerEngine
        Push-Location $projectRoot
        try {
            $existingLiveKitContainer = (
                & $dockerCommand.Source compose ps -q livekit 2>$null |
                    Select-Object -First 1
            )
            $demoLiveKitOwned = [string]::IsNullOrWhiteSpace(
                $existingLiveKitContainer
            )
            Write-Host "Starting local LiveKit: docker compose up -d livekit"
            & $dockerCommand.Source compose up -d livekit
            if ($LASTEXITCODE -ne 0) {
                throw "Local LiveKit could not be started."
            }
        }
        finally {
            Pop-Location
        }
    }

    $children += Start-DevProcess `
        -Name "api" `
        -FilePath $python `
        -Arguments $apiArguments `
        -WorkingDirectory $backendRoot
    $workerChild = Start-DevProcess `
        -Name "worker" `
        -FilePath $python `
        -Arguments $workerArguments `
        -WorkingDirectory $backendRoot
    $children += $workerChild
    Wait-WorkerRegistration `
        -WorkerProcess $workerChild.Process `
        -LogPaths @($workerChild.Stdout, $workerChild.Stderr)
    $children += Start-DevProcess `
        -Name "frontend" `
        -FilePath $pnpmCommand.Source `
        -Arguments $frontendArguments `
        -WorkingDirectory $frontendRoot

    @(
        $children | ForEach-Object {
            [ordered]@{
                name = $_.Name
                pid = $_.Process.Id
                stdout = $_.Stdout
                stderr = $_.Stderr
            }
        }
    ) | ConvertTo-Json | Set-Content -LiteralPath $pidFile -Encoding UTF8

    Write-Host "Logs: $logRoot"
    Write-Host "Press Ctrl+C to stop the recorded API, Worker and Frontend trees."
    if ($Demo) {
        Wait-HttpEndpoint `
            -Name "FastAPI" `
            -Url "http://127.0.0.1:8000/health/ready"
        Wait-HttpEndpoint `
            -Name "Frontend" `
            -Url "http://127.0.0.1:3000/"
        $demoUrl = "http://127.0.0.1:3000/"
        Write-Host "Opening LiveCaption Studio: $demoUrl"
        Start-Process $demoUrl
        Write-Host "Demo is running. Keep this window open; press Ctrl+C to stop."
    }
    while ($true) {
        foreach ($child in $children) {
            $child.Process.Refresh()
            if ($child.Process.HasExited) {
                throw "$($child.Name) exited with code $($child.Process.ExitCode). See $($child.Stderr)"
            }
        }
        Start-Sleep -Seconds 1
    }
}
finally {
    foreach ($child in @($children | Sort-Object { $_.Process.Id } -Descending)) {
        Stop-ExactProcessTree -RootProcessId $child.Process.Id
    }
    if ($Demo -and $demoLiveKitOwned) {
        Push-Location $projectRoot
        try {
            Write-Host "Stopping launcher-owned LiveKit: docker compose down"
            & $dockerCommand.Source compose down
        }
        finally {
            Pop-Location
        }
    }
    Write-Host "Stopped recorded development processes. Logs remain at $logRoot"
}
