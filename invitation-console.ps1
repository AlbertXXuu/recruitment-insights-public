[CmdletBinding()]
param(
    [string]$PythonExe,
    [switch]$ConfigureRuntimeOnly,
    [switch]$ConfigureCloudflare,
    [switch]$CloudflareCheckOnly,
    [switch]$StartServices
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$consoleScript = Join-Path $PSScriptRoot 'scripts/invitation_console.py'
$runtimeDirectory = Join-Path (Join-Path $PSScriptRoot 'outputs') 'invitation-test'
$runtimePathFile = Join-Path $runtimeDirectory 'python-path.txt'
$explicitRuntime = $PSBoundParameters.ContainsKey('PythonExe')

try {
    if ($CloudflareCheckOnly -and -not $ConfigureCloudflare) {
        throw '-CloudflareCheckOnly 必须与 -ConfigureCloudflare 一起使用。'
    }
    if ($ConfigureRuntimeOnly -and $ConfigureCloudflare) {
        throw '运行时配置与 Cloudflare 配置请分别执行。'
    }
    if (-not (Test-Path -LiteralPath $consoleScript -PathType Leaf)) {
        throw '未找到本机邀请控制台程序，请使用完整的当前源码。'
    }

    $savedRuntimeExists = Test-Path -LiteralPath $runtimePathFile
    if ($explicitRuntime) {
        if ([string]::IsNullOrWhiteSpace($PythonExe)) {
            throw '-PythonExe 必须指定实际的 python.exe 路径。'
        }
        $candidateRuntime = $PythonExe
    } elseif ($savedRuntimeExists) {
        if (-not (Test-Path -LiteralPath $runtimePathFile -PathType Leaf)) {
            throw '运行时配置不是普通文件，请通过 -PythonExe 重新配置。'
        }
        $candidateRuntime = [System.IO.File]::ReadAllText($runtimePathFile).Trim()
        if ([string]::IsNullOrWhiteSpace($candidateRuntime) -or
            -not [System.IO.Path]::IsPathRooted($candidateRuntime)) {
            throw '已保存的 Python 路径无效，请通过 -PythonExe 重新配置。'
        }
    } else {
        $pythonCommand = Get-Command python -CommandType Application -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($null -eq $pythonCommand) {
            throw '尚未配置 Python，请通过 -PythonExe 指定已安装的合格解释器。'
        }
        $candidateRuntime = $pythonCommand.Path
    }

    if (-not (Test-Path -LiteralPath $candidateRuntime -PathType Leaf)) {
        throw 'Python 文件不存在，请通过 -PythonExe 重新配置。'
    }
    $resolvedRuntime = (Resolve-Path -LiteralPath $candidateRuntime).ProviderPath
    if ([System.IO.Path]::GetExtension($resolvedRuntime) -ine '.exe') {
        throw 'Python 路径必须指向实际的解释器 .exe 文件。'
    }
    if ([System.IO.Path]::GetFileName($resolvedRuntime) -in @('py.exe', 'pyw.exe', 'pythonw.exe')) {
        throw '请指定支持交互控制台的 Python 解释器，不使用 py 启动器或 pythonw。'
    }
    # An App Execution Alias can open the Store instead of running Python.
    if ($resolvedRuntime -match '(?i)\\Microsoft\\WindowsApps\\python(?:3)?\.exe$') {
        throw '检测到 Windows 应用执行别名，请指定实际安装的 python.exe。'
    }

    # This probe is memory-only. Do not create the output directory or save a
    # runtime until Python/SQLite/JSON requirements have all passed.
    & $resolvedRuntime -X utf8 $consoleScript --check-runtime
    if ($LASTEXITCODE -ne 0) {
        throw 'Python／SQLite 运行时检查未通过；未保存新配置，也未启动控制台。请通过 -PythonExe 重新配置。'
    }

    if ($explicitRuntime -or $ConfigureRuntimeOnly -or -not $savedRuntimeExists) {
        # Reject linked ancestors and shared files before any configuration write.
        $runtimePathCheck = @'
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
try:
    from scripts.invitation_console import reject_links
    reject_links(Path(sys.argv[2]))
except Exception as exc:
    print('运行时配置路径检查失败：' + str(exc), file=sys.stderr)
    raise SystemExit(1)
'@
        & $resolvedRuntime -B -X utf8 -c $runtimePathCheck $PSScriptRoot $runtimePathFile
        if ($LASTEXITCODE -ne 0) {
            throw '运行时配置路径不安全；未保存新配置，也未启动控制台。请检查输出路径，不要删除或移动链接文件。'
        }
        [void][System.IO.Directory]::CreateDirectory($runtimeDirectory)
        $runtimeEncoding = [System.Text.UTF8Encoding]::new($false)
        [System.IO.File]::WriteAllText($runtimePathFile, $resolvedRuntime, $runtimeEncoding)
        Write-Host "已保存 Python 路径：$resolvedRuntime"
    }
    if ($ConfigureRuntimeOnly) {
        Write-Host '运行时配置完成；未启动源站。'
        exit 0
    }

    Push-Location -LiteralPath $PSScriptRoot
    try {
        if ($ConfigureCloudflare) {
            $setupScript = Join-Path $PSScriptRoot 'scripts/configure_cloudflare.py'
            if ($CloudflareCheckOnly) {
                & $resolvedRuntime -X utf8 $setupScript --check-only
            } else {
                # The normal Windows helper only prepares this connector's
                # local credentials. Route and DNS are configured in the UI.
                & $resolvedRuntime -X utf8 $setupScript --runtime-only
            }
        } else {
            $consoleArguments = @()
            if ($StartServices) { $consoleArguments += '--start-services' }
            & $resolvedRuntime -X utf8 $consoleScript @consoleArguments
        }
        $consoleExit = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    exit $consoleExit
} catch {
    Write-Host ("无法打开邀请控制台：" + $_.Exception.Message) -ForegroundColor Red
    exit 1
}
