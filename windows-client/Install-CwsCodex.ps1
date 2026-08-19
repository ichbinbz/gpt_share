param(
    [string] $BrokerUrl = "http://codex.cws.internal:8765",
    [string] $ProxyUrl = "http://192.168.2.38:7897",
    [string] $InstallDir = (Join-Path $env:LOCALAPPDATA "CWS Codex"),
    [string] $CodexHome = (Join-Path $env:USERPROFILE ".cws-codex"),
    [switch] $SkipExtensionInstall
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "Common-CwsCodex.ps1")

Write-Host "正在安装 CWS Codex 员工端..." -ForegroundColor Cyan
Set-CwsPrivateDirectoryAcl -Path $InstallDir
Set-CwsPrivateDirectoryAcl -Path $CodexHome

$PayloadFiles = @(
    "Common-CwsCodex.ps1",
    "Sync-CwsCodex.ps1",
    "Report-CwsCodexUsage.ps1",
    "Diagnose-CwsCodex.ps1",
    "Watch-CwsCodex.ps1",
    "Launch-CwsCodex.ps1",
    "Configure-CwsCodex.ps1",
    "Uninstall-CwsCodex.ps1",
    "CwsCodex.ico",
    "Launch-CwsCodex.cmd",
    "Configure-Proxy.cmd",
    "Uninstall.cmd",
    "诊断.cmd"
)
foreach ($File in $PayloadFiles) {
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot $File) -Destination (Join-Path $InstallDir $File) -Force
}

$TokenPath = Join-Path $InstallDir "device-token.dpapi"
$KeepExisting = $false
if (Test-Path -LiteralPath $TokenPath) {
    $Answer = Read-Host "检测到已保存的设备令牌，是否保留？[Y/n]"
    $KeepExisting = (-not $Answer) -or $Answer.ToLowerInvariant().StartsWith("y")
}
if (-not $KeepExisting) {
    $SecureToken = Read-Host "请输入管理员从服务器签发的本机专用设备令牌" -AsSecureString
    try {
        if ($SecureToken.Length -lt 20) {
            throw "设备令牌格式无效。"
        }
        $EncryptedToken = $SecureToken | ConvertFrom-SecureString
        if (-not $EncryptedToken) {
            throw "设备令牌不能为空。"
        }
        $EncryptedToken | Set-Content -LiteralPath $TokenPath -Encoding UTF8
    }
    finally {
        if ($SecureToken) {
            $SecureToken.Dispose()
        }
    }
}

$VsCodePath = Find-CwsVsCode
if (-not $VsCodePath) {
    throw "未找到 Visual Studio Code，请安装后重新运行安装器。"
}

$Config = [ordered] @{
    broker_url = $BrokerUrl
    proxy_url  = $ProxyUrl
    codex_home = $CodexHome
    vscode_path = $VsCodePath
}
$ConfigPath = Join-Path $InstallDir "config.json"
Write-CwsJsonAtomic -Path $ConfigPath -Value $Config
Set-CwsPrivateDirectoryAcl -Path $InstallDir

if (-not $SkipExtensionInstall) {
    Write-Host "正在检查并安装 OpenAI Codex VS Code 扩展..."
    try {
        & $VsCodePath --install-extension openai.chatgpt --force | Out-Host
    }
    catch {
        Write-Warning "自动安装扩展失败，请在 VS Code 扩展市场手动安装 OpenAI Codex。"
    }
}

try {
    & (Join-Path $InstallDir "Sync-CwsCodex.ps1") -ConfigPath $ConfigPath -NewLease
}
catch {
    Write-Warning "首次同步失败：$($_.Exception.Message)"
    Write-Warning "安装已完成。连接公司内网或代理后，可双击桌面快捷方式重试。"
}

$Desktop = [Environment]::GetFolderPath("Desktop")
$ShortcutPath = Join-Path $Desktop "公司 Codex（VS Code）.lnk"
$IconPath = Join-Path $InstallDir "CwsCodex.ico"
$Shell = New-Object -ComObject WScript.Shell
$Shortcut = $Shell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = Join-Path $InstallDir "Launch-CwsCodex.cmd"
$Shortcut.WorkingDirectory = $InstallDir
$Shortcut.IconLocation = "$IconPath,0"
$Shortcut.Description = "启动已配置公司 Codex 环境的 Visual Studio Code"
$Shortcut.Save()

@("CWS Codex.lnk", "公司编程助手.lnk") | ForEach-Object {
    Remove-Item -LiteralPath (Join-Path $Desktop $_) -Force -ErrorAction SilentlyContinue
}

$StartupDir = [Environment]::GetFolderPath("Startup")
$StartupShortcutPath = Join-Path $StartupDir "公司 Codex 后台同步.lnk"
$StartupShortcut = $Shell.CreateShortcut($StartupShortcutPath)
$StartupShortcut.TargetPath = "powershell.exe"
$WatcherPath = Join-Path $InstallDir "Watch-CwsCodex.ps1"
$StartupShortcut.Arguments = "-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$WatcherPath`" -ConfigPath `"$ConfigPath`""
$StartupShortcut.WorkingDirectory = $InstallDir
$StartupShortcut.WindowStyle = 7
$StartupShortcut.IconLocation = "$IconPath,0"
$StartupShortcut.Description = "公司 Codex 凭据同步和 Token 用量上报"
$StartupShortcut.Save()

@("CWS Codex Background.lnk", "公司编程助手后台.lnk") | ForEach-Object {
    Remove-Item -LiteralPath (Join-Path $StartupDir $_) -Force -ErrorAction SilentlyContinue
}

Start-Process -FilePath "powershell.exe" -ArgumentList $StartupShortcut.Arguments -WindowStyle Hidden

Write-Host "安装完成。请双击桌面的“公司 Codex（VS Code）”快捷方式。" -ForegroundColor Green
