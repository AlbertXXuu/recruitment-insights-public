param([int]$Port = 8765, [int]$SyncInterval = 0, [switch]$Background)
$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$pythonCommand = Get-Command python -ErrorAction Stop
$pythonPath = $pythonCommand.Source
$projectApp = Join-Path $projectRoot 'app.py'
$expectedInfo = & $pythonPath -c 'import sys,json;sys.path.insert(0,sys.argv[1]);from recruitment_insights.config import INSTANCE_ID,VERSION;print(json.dumps(dict(instance_id=INSTANCE_ID,version=VERSION)))' $projectRoot | ConvertFrom-Json
$expectedInstance = $expectedInfo.instance_id
$expectedVersion = $expectedInfo.version
$health = $null
try {
    $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/health" -TimeoutSec 2
} catch { }
if ($null -ne $health) {
    if ($health.service -eq 'recruitment-insights' -and $health.instance_id -eq $expectedInstance -and $health.version -eq $expectedVersion) {
        Write-Output "Already running: http://127.0.0.1:$Port"
        exit 0
    }
    throw "Port $Port is occupied by a different project or an older version. Stop that instance or choose another port."
}
if ($Background) {
    $logDirectory = Join-Path $projectRoot 'outputs'
    New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
    $taskProcess = Start-Process -FilePath $pythonPath -ArgumentList @(('"' + $projectApp + '"'), '--port', "$Port", '--sync-interval', "$SyncInterval") -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $logDirectory 'server.out.log') -RedirectStandardError (Join-Path $logDirectory 'server.err.log')
    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        $taskProcess.Refresh()
        if ($taskProcess.HasExited) { throw 'Server exited before becoming healthy. Check outputs/server.err.log.' }
        try {
            $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/health" -TimeoutSec 1
            if ($health.service -eq 'recruitment-insights' -and $health.instance_id -eq $expectedInstance -and $health.version -eq $expectedVersion) {
                $taskProcess.Id | Set-Content -LiteralPath (Join-Path $logDirectory 'server.pid')
                Write-Output "Started PID $($taskProcess.Id): http://127.0.0.1:$Port"
                exit 0
            }
        } catch { }
        Start-Sleep -Milliseconds 250
    }
    Stop-Process -Id $taskProcess.Id -ErrorAction SilentlyContinue
    throw 'Server did not become healthy. Check outputs/server.err.log.'
} else {
    & $pythonPath $projectApp --port $Port --sync-interval $SyncInterval
}
