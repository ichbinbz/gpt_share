param(
    [string] $BrokerUrl = "http://codex.cws.internal:8765",
    [string] $ProxyUrl = "http://192.168.2.38:7897",
    [string] $InstallDir = (Join-Path $env:LOCALAPPDATA "CWS Codex"),
    [string] $CodexHome = (Join-Path $env:USERPROFILE ".codex"),
    [string] $ClientHome = (Join-Path $env:USERPROFILE ".cws-codex"),
    [switch] $SkipExtensionInstall,
    [switch] $SkipInitialSync,
    [switch] $SkipBackgroundStart,
    [switch] $AutoUpdate,
    [Security.SecureString] $DeviceToken,
    [string] $DesktopDir,
    [string] $StartupDir
)

$ErrorActionPreference = "Stop"
$ClientVersion = "0.1.4"
. (Join-Path $PSScriptRoot "Common-CwsCodex.ps1")
$InstallLogPath = Join-Path $env:TEMP "CWS-Codex-Install.log"

trap {
    $Details = @(
        "$(Get-Date -Format o) installation failed",
        "Message: $($_.Exception.Message)",
        "Category: $($_.CategoryInfo)",
        "Position: $($_.InvocationInfo.PositionMessage)",
        "Stack: $($_.ScriptStackTrace)"
    ) -join [Environment]::NewLine
    try {
        [System.IO.File]::WriteAllText($InstallLogPath, $Details + [Environment]::NewLine, (New-Object System.Text.UTF8Encoding($true)))
    }
    catch {
        # Preserve the original installation error even if logging fails.
    }
    Write-Error "安装失败：$($_.Exception.Message)；详细日志：$InstallLogPath"
    exit 1
}

Write-Host "正在安装 CWS Codex 员工端..." -ForegroundColor Cyan
$ExistingConfigPath = Join-Path $InstallDir "config.json"
if ($AutoUpdate -and (Test-Path -LiteralPath $ExistingConfigPath)) {
    $ExistingConfig = Get-Content -LiteralPath $ExistingConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($ExistingConfig.broker_url) { $BrokerUrl = [string] $ExistingConfig.broker_url }
    if ($null -ne $ExistingConfig.proxy_url) { $ProxyUrl = [string] $ExistingConfig.proxy_url }
    if ($ExistingConfig.codex_home) { $CodexHome = [string] $ExistingConfig.codex_home }
    if ($ExistingConfig.client_home) { $ClientHome = [string] $ExistingConfig.client_home }
}
Set-CwsPrivateDirectoryAcl -Path $ClientHome
if (-not (Test-Path -LiteralPath $CodexHome)) {
    New-Item -ItemType Directory -Path $CodexHome -Force | Out-Null
}

$PayloadFiles = @(
    "Common-CwsCodex.ps1",
    "Sync-CwsCodex.ps1",
    "Report-CwsCodexUsage.ps1",
    "Update-CwsCodex.ps1",
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
function Copy-CwsPayload {
    foreach ($File in $PayloadFiles) {
        $SourcePath = Join-Path $PSScriptRoot $File
        $DestinationPath = Join-Path $InstallDir $File
        if ([System.IO.Path]::GetFullPath($SourcePath) -ine [System.IO.Path]::GetFullPath($DestinationPath)) {
            Copy-Item -LiteralPath $SourcePath -Destination $DestinationPath -Force
        }
    }
}

Set-CwsPrivateDirectoryAcl -Path $InstallDir
try {
    Copy-CwsPayload
}
catch [System.UnauthorizedAccessException] {
    if ([System.IO.Path]::GetFullPath($PSScriptRoot).TrimEnd("\") -ieq [System.IO.Path]::GetFullPath($InstallDir).TrimEnd("\")) {
        throw
    }
    $BackupDir = "{0}.inaccessible-{1}-{2}" -f $InstallDir, (Get-Date -Format "yyyyMMdd-HHmmss"), $PID
    Write-Warning "旧安装目录包含无法覆盖的文件，正在保留到：$BackupDir"
    Move-Item -LiteralPath $InstallDir -Destination $BackupDir
    Set-CwsPrivateDirectoryAcl -Path $InstallDir
    Copy-CwsPayload
}

$TokenPath = Join-Path $InstallDir "device-token.dpapi"
$KeepExisting = $AutoUpdate -and (Test-Path -LiteralPath $TokenPath)
if (Test-Path -LiteralPath $TokenPath) {
    if (-not $AutoUpdate) {
        $Answer = Read-Host "检测到已保存的设备令牌，是否保留？[Y/n]"
        $KeepExisting = (-not $Answer) -or $Answer.ToLowerInvariant().StartsWith("y")
    }
}
if (-not $KeepExisting) {
    $SecureToken = $DeviceToken
    if (-not $SecureToken) {
        $SecureToken = Read-Host "请输入管理员从服务器签发的本机专用设备令牌" -AsSecureString
    }
    try {
        if ($SecureToken.Length -lt 20) {
            throw "设备令牌格式无效。"
        }
        $ProtectionScheme = Protect-CwsDeviceToken -Token $SecureToken -Path $TokenPath
        if (-not $ProtectionScheme) {
            throw "设备令牌不能为空。"
        }
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
    client_home = $ClientHome
    vscode_path = $VsCodePath
    client_version = $ClientVersion
}
$ConfigPath = Join-Path $InstallDir "config.json"
Write-CwsJsonAtomic -Path $ConfigPath -Value $Config
Set-CwsPrivateDirectoryAcl -Path $InstallDir

if (-not $SkipExtensionInstall -and -not $AutoUpdate) {
    Write-Host "正在检查并安装 OpenAI Codex VS Code 扩展..."
    try {
        & $VsCodePath --install-extension openai.chatgpt --force | Out-Host
    }
    catch {
        Write-Warning "自动安装扩展失败，请在 VS Code 扩展市场手动安装 OpenAI Codex。"
    }
}

if (-not $SkipInitialSync) {
    try {
        & (Join-Path $InstallDir "Sync-CwsCodex.ps1") -ConfigPath $ConfigPath -NewLease
    }
    catch {
        Write-Warning "首次同步失败：$($_.Exception.Message)"
        Write-Warning "安装已完成。连接公司内网或代理后，可双击桌面快捷方式重试。"
    }
}

if (-not $DesktopDir) {
    $DesktopDir = [Environment]::GetFolderPath("Desktop")
}
if (-not (Test-Path -LiteralPath $DesktopDir)) {
    New-Item -ItemType Directory -Path $DesktopDir -Force | Out-Null
}
$ShortcutPath = Join-Path $DesktopDir "公司 Codex（VS Code）.lnk"
$IconPath = Join-Path $InstallDir "CwsCodex.ico"
$Shell = New-Object -ComObject WScript.Shell
$Shortcut = $Shell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = Join-Path $InstallDir "Launch-CwsCodex.cmd"
$Shortcut.WorkingDirectory = $InstallDir
$Shortcut.IconLocation = "$IconPath,0"
$Shortcut.Description = "启动已配置公司 Codex 环境的 Visual Studio Code"
$Shortcut.Save()

@("CWS Codex.lnk", "公司编程助手.lnk") | ForEach-Object {
    Remove-Item -LiteralPath (Join-Path $DesktopDir $_) -Force -ErrorAction SilentlyContinue
}

if (-not $StartupDir) {
    $StartupDir = [Environment]::GetFolderPath("Startup")
}
if (-not (Test-Path -LiteralPath $StartupDir)) {
    New-Item -ItemType Directory -Path $StartupDir -Force | Out-Null
}
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

if (-not $SkipBackgroundStart) {
    Start-Process -FilePath "powershell.exe" -ArgumentList $StartupShortcut.Arguments -WindowStyle Hidden
}

Remove-Item -LiteralPath $InstallLogPath -Force -ErrorAction SilentlyContinue
Write-Host "安装完成。请双击桌面的“公司 Codex（VS Code）”快捷方式。" -ForegroundColor Green
