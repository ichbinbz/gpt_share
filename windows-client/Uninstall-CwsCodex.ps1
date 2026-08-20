param([string] $InstallDir = (Join-Path $env:LOCALAPPDATA "CWS Codex"))

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "Common-CwsCodex.ps1")

Get-CimInstance Win32_Process -Filter "Name = 'powershell.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like "*Watch-CwsCodex.ps1*" -and $_.ProcessId -ne $PID } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

$Desktop = [Environment]::GetFolderPath("Desktop")
@("公司 Codex（VS Code）.lnk", "CWS Codex.lnk", "公司编程助手.lnk") | ForEach-Object {
    Remove-Item -LiteralPath (Join-Path $Desktop $_) -Force -ErrorAction SilentlyContinue
}
$StartupDir = [Environment]::GetFolderPath("Startup")
@("公司 Codex 后台同步.lnk", "CWS Codex Background.lnk", "公司编程助手后台.lnk") | ForEach-Object {
    Remove-Item -LiteralPath (Join-Path $StartupDir $_) -Force -ErrorAction SilentlyContinue
}

$ConfigPath = Join-Path $InstallDir "config.json"
if (Test-Path -LiteralPath $ConfigPath) {
    try {
        $Config = Get-CwsConfig -ConfigPath $ConfigPath
        $CodexHome = [Environment]::ExpandEnvironmentVariables([string] $Config.codex_home)
        $ClientHome = Get-CwsClientHome -Config $Config
        if (Restore-CwsOriginalAuth -CodexHome $CodexHome -ClientHome $ClientHome) {
            Write-Host "已恢复安装前的 Codex 登录凭据。" -ForegroundColor Green
        }
    }
    catch {
        Write-Warning "恢复安装前 Codex 凭据失败：$($_.Exception.Message)"
    }
}

$RemoveCredentials = Read-Host "是否同时删除 $env:USERPROFILE\.cws-codex 中的短期凭据？[y/N]"
if ($RemoveCredentials -and $RemoveCredentials.ToLowerInvariant().StartsWith("y")) {
    Remove-Item -LiteralPath (Join-Path $env:USERPROFILE ".cws-codex") -Recurse -Force -ErrorAction SilentlyContinue
}

Set-Location $env:TEMP
Remove-Item -LiteralPath $InstallDir -Recurse -Force -ErrorAction SilentlyContinue
Write-Host "CWS Codex 员工端已卸载。" -ForegroundColor Green
