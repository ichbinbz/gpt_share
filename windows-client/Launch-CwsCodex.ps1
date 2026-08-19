param([string] $ConfigPath = (Join-Path $PSScriptRoot "config.json"))

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "Common-CwsCodex.ps1")
$LogPath = Join-Path $PSScriptRoot "launch.log"

function Write-CwsLaunchLog {
    param([Parameter(Mandatory = $true)] [string] $Message)
    Add-Content -LiteralPath $LogPath -Value ("{0} {1}" -f [DateTime]::Now.ToString("s"), $Message) -Encoding UTF8
}

$Config = Get-CwsConfig -ConfigPath $ConfigPath
$VsCodePath = [Environment]::ExpandEnvironmentVariables([string] $Config.vscode_path)
if (-not $VsCodePath -or -not (Test-Path -LiteralPath $VsCodePath)) {
    $VsCodePath = Find-CwsVsCode
}
if (-not $VsCodePath) {
    throw "未找到 Visual Studio Code，请先安装 VS Code。"
}

$SyncSucceeded = $true
try {
    & (Join-Path $PSScriptRoot "Sync-CwsCodex.ps1") -ConfigPath $ConfigPath
    Write-CwsLaunchLog "Credential synchronization succeeded."
}
catch {
    $SyncSucceeded = $false
    Write-CwsLaunchLog ("Credential synchronization failed: " + $_.Exception.Message)
    Write-Warning "凭据同步失败，仍将启动 VS Code。请运行安装目录中的“诊断.cmd”检查代理和令牌。"
}

$CodexHome = [Environment]::ExpandEnvironmentVariables([string] $Config.codex_home)
$env:CODEX_HOME = $CodexHome
$VsCodeUserData = Join-Path $CodexHome "vscode-user-data"
if (-not (Test-Path -LiteralPath $VsCodeUserData)) {
    New-Item -ItemType Directory -Path $VsCodeUserData -Force | Out-Null
}
$VsCodeArguments = @("--new-window", "--user-data-dir", $VsCodeUserData)
$VsCodeArgumentLine = "--new-window --user-data-dir `"$VsCodeUserData`""
if ([System.IO.Path]::GetExtension($VsCodePath) -ieq ".cmd") {
    & $VsCodePath @VsCodeArguments
    if ($LASTEXITCODE -ne 0) {
        throw "VS Code launcher returned exit code $LASTEXITCODE"
    }
}
else {
    Start-Process -FilePath $VsCodePath -ArgumentList $VsCodeArgumentLine
}
Write-CwsLaunchLog ("VS Code launch requested: " + $VsCodePath)

$Watcher = Join-Path $PSScriptRoot "Watch-CwsCodex.ps1"
$WatcherArgs = "-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Watcher`" -ConfigPath `"$ConfigPath`""
Start-Process -FilePath "powershell.exe" -ArgumentList $WatcherArgs -WindowStyle Hidden

if (-not $SyncSucceeded) {
    Write-Host "VS Code 已启动，但 Codex 凭据尚未同步。诊断日志：$LogPath" -ForegroundColor Yellow
}
