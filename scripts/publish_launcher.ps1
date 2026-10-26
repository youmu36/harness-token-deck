[CmdletBinding()]
param(
    [ValidateSet('Menu', 'Preflight', 'Publish')]
    [string]$Action = 'Menu',
    [string]$Proxy = 'auto',
    [switch]$NoPause,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$PublisherArgs
)

$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskExit = 0
$taskTranscript = $false
Push-Location -LiteralPath $taskRoot
try {
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        throw 'uv is missing. Install it from https://docs.astral.sh/uv/getting-started/installation/'
    }
    $taskVersion = (Get-Content -Raw -LiteralPath 'VERSION').Trim()
    $taskOrigin = & git remote get-url origin
    if ($LASTEXITCODE -ne 0) { throw 'Cannot read origin. Run this launcher inside a Git checkout.' }
    Write-Host "Harness Token Deck $taskVersion - GitHub 发布工具" -ForegroundColor Cyan
    Write-Host "目标仓库：$taskOrigin"
    if ($Proxy -eq 'auto') {
        $taskSocket = New-Object System.Net.Sockets.TcpClient
        try {
            $taskConnection = $taskSocket.BeginConnect('127.0.0.1', 7890, $null, $null)
            if ($taskConnection.AsyncWaitHandle.WaitOne(600)) {
                try {
                    $taskSocket.EndConnect($taskConnection)
                    $Proxy = '7890'
                } catch { $Proxy = 'direct' }
            } else { $Proxy = 'direct' }
        } finally { $taskSocket.Dispose() }
        Write-Host "网络：$Proxy（自动检测本机 7890，无监听时直连）"
    } else { Write-Host "网络：$Proxy" }
    if ($Action -eq 'Menu') {
        Write-Host ''
        Write-Host '1. 仅检查（默认，不上传）'
        Write-Host '2. 正式发布源码和 Windows 安装包到 GitHub'
        Write-Host '0. 退出'
        $taskChoice = Read-Host '请选择'
        switch ($taskChoice) {
            ''  { $Action = 'Preflight' }
            '1' { $Action = 'Preflight' }
            '2' { $Action = 'Publish' }
            '0' { exit 0 }
            default { throw '无效选项，请输入 1、2 或 0。' }
        }
    }
    New-Item -ItemType Directory -Path '.verify/publish' -Force | Out-Null
    $taskLog = Join-Path $taskRoot ('.verify/publish/' + (Get-Date -Format 'yyyyMMdd-HHmmss-fff') + '.log')
    Start-Transcript -LiteralPath $taskLog | Out-Null
    $taskTranscript = $true
    $taskArguments = @('--proxy', $Proxy)
    if ($Action -eq 'Preflight') { $taskArguments += '--dry-run' }
    $taskArguments += $PublisherArgs
    & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'publish_release.ps1') @taskArguments
    $taskExit = $LASTEXITCODE
    if ($taskExit -ne 0) { throw "Publisher stopped (exit $taskExit). See the error above and $taskLog" }
    Write-Host ''
    Write-Host "$Action 已完成。" -ForegroundColor Green
    Write-Host "日志：$taskLog"
} catch {
    $taskExit = 1
    Write-Host $_.Exception.Message -ForegroundColor Red
} finally {
    if ($taskTranscript) { Stop-Transcript | Out-Null }
    Pop-Location
    if (-not $NoPause) { Read-Host '按回车关闭窗口' | Out-Null }
}
exit $taskExit
