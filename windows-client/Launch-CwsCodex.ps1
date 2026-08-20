param([string] $ConfigPath = (Join-Path $PSScriptRoot "config.json"))

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "Common-CwsCodex.ps1")
$LogPath = Join-Path $PSScriptRoot "launch.log"

function Write-CwsLaunchLog {
    param([Parameter(Mandatory = $true)] [string] $Message)
    Add-Content -LiteralPath $LogPath -Value ("{0} {1}" -f [DateTime]::Now.ToString("s"), $Message) -Encoding UTF8
}

$Config = Get-CwsConfig -ConfigPath $ConfigPath
$Updater = Join-Path $PSScriptRoot "Update-CwsCodex.ps1"
if (Test-Path -LiteralPath $Updater) {
    try {
        $Updated = & $Updater -ConfigPath $ConfigPath
        if ($Updated) {
            Write-CwsLaunchLog "Client auto-update completed; restarting launcher."
            $RestartArgs = "-NoLogo -NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" -ConfigPath `"$ConfigPath`""
            Start-Process -FilePath "powershell.exe" -ArgumentList $RestartArgs -WindowStyle Hidden
            return
        }
    }
    catch {
        Write-CwsLaunchLog ("Client update check failed: " + $_.Exception.Message)
    }
}
$VsCodePath = [Environment]::ExpandEnvironmentVariables([string] $Config.vscode_path)
if (-not $VsCodePath -or -not (Test-Path -LiteralPath $VsCodePath)) {
    $VsCodePath = Find-CwsVsCode
}
if (-not $VsCodePath) {
    throw "未找到 Visual Studio Code，请先安装 VS Code。"
}

$InstallExtension = $null -eq $Config.install_extension -or [bool] $Config.install_extension
$ExtensionRoot = Join-Path $env:USERPROFILE ".vscode\extensions"
$ExtensionInstalled = @(Get-ChildItem -LiteralPath $ExtensionRoot -Directory -Filter "openai.chatgpt-*" -ErrorAction SilentlyContinue).Count -gt 0
if ($InstallExtension -and -not $ExtensionInstalled) {
    $VsCodeCli = Find-CwsVsCodeCli -VsCodePath $VsCodePath
    if ($VsCodeCli) {
        try {
            Write-CwsLaunchLog "Installing Codex extension from the VS Code CLI before launch."
            $ExtensionProcess = Start-Process -FilePath $VsCodeCli -ArgumentList @(
                "--install-extension", "openai.chatgpt", "--force"
            ) -WindowStyle Hidden -PassThru
            if (-not $ExtensionProcess.WaitForExit(30000)) {
                Stop-Process -Id $ExtensionProcess.Id -Force -ErrorAction SilentlyContinue
                Write-CwsLaunchLog "Codex extension installation timed out after 30 seconds; continuing launch."
            }
            elseif ($ExtensionProcess.ExitCode -ne 0) {
                Write-CwsLaunchLog "Codex extension installer returned exit code $($ExtensionProcess.ExitCode); continuing launch."
            }
        }
        catch {
            Write-CwsLaunchLog ("Codex extension installation failed; continuing launch: " + $_.Exception.Message)
        }
    }
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
$RunningVsCode = Get-Process -Name "Code" -ErrorAction SilentlyContinue
if ($RunningVsCode) {
    Write-Warning "VS Code 已在运行。若这是首次切换公司 Codex 凭据，请先关闭所有 VS Code 窗口，再重新使用桌面快捷方式启动。"
}
if ([System.IO.Path]::GetExtension($VsCodePath) -ieq ".cmd") {
    & $VsCodePath
    if ($LASTEXITCODE -ne 0) {
        throw "VS Code launcher returned exit code $LASTEXITCODE"
    }
}
else {
    Start-Process -FilePath $VsCodePath
}
Write-CwsLaunchLog ("VS Code launch requested: " + $VsCodePath)

$Watcher = Join-Path $PSScriptRoot "Watch-CwsCodex.ps1"
$WatcherArgs = "-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Watcher`" -ConfigPath `"$ConfigPath`""
Start-Process -FilePath "powershell.exe" -ArgumentList $WatcherArgs -WindowStyle Hidden

if (-not $SyncSucceeded) {
    Write-Host "VS Code 已启动，但 Codex 凭据尚未同步。诊断日志：$LogPath" -ForegroundColor Yellow
}
