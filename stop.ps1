$ErrorActionPreference = 'Stop'
$pidFile = Join-Path $PSScriptRoot 'outputs/server.pid'
if (-not (Test-Path -LiteralPath $pidFile)) { Write-Output 'No background PID file. Use Ctrl+C in the foreground console.'; exit 0 }
$taskProcessId = [int](Get-Content -LiteralPath $pidFile)
$taskProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$taskProcessId"
$expectedApp = Join-Path $PSScriptRoot 'app.py'
if ($taskProcess -and $taskProcess.CommandLine -and $taskProcess.CommandLine.Contains($expectedApp)) {
    try {
        Stop-Process -Id $taskProcessId -ErrorAction Stop
    } catch {
        # Fallback for Windows hosts where the PowerShell process adapter fails.
        & taskkill.exe /PID $taskProcessId /F
        if ($LASTEXITCODE -ne 0) { throw 'Could not stop the verified project process.' }
    }
    Write-Output "Stopped recruitment server PID $taskProcessId"
} elseif ($taskProcess) { throw 'PID belongs to another process; refused to stop it.' }
