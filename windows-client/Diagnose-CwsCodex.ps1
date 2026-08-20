param([string] $ConfigPath = (Join-Path $PSScriptRoot "config.json"))

$ErrorActionPreference = "Continue"
. (Join-Path $PSScriptRoot "Common-CwsCodex.ps1")

Write-Host "CWS Codex 员工端诊断" -ForegroundColor Cyan
Write-Host "安装目录：$PSScriptRoot"

try {
    $Config = Get-CwsConfig -ConfigPath $ConfigPath
    Write-Host "[正常] 已读取配置文件" -ForegroundColor Green
    Write-Host "Broker：$($Config.broker_url)"
    Write-Host "代理：$($Config.proxy_url)"
    Write-Host "CODEX_HOME：$($Config.codex_home)"
}
catch {
    Write-Host "[失败] 配置文件：$($_.Exception.Message)" -ForegroundColor Red
    exit 1
}

$VsCodePath = [Environment]::ExpandEnvironmentVariables([string] $Config.vscode_path)
if ($VsCodePath -and (Test-Path -LiteralPath $VsCodePath)) {
    Write-Host "[正常] VS Code：$VsCodePath" -ForegroundColor Green
}
else {
    $VsCodePath = Find-CwsVsCode
    if ($VsCodePath) {
        Write-Host "[正常] 重新发现 VS Code：$VsCodePath" -ForegroundColor Green
    }
    else {
        Write-Host "[失败] 未找到 VS Code" -ForegroundColor Red
    }
}

try {
    $DeviceToken = Get-CwsDeviceToken -TokenPath (Join-Path $PSScriptRoot "device-token.dpapi")
    if ($DeviceToken -and $DeviceToken.StartsWith("cwsdt_") -and $DeviceToken.Length -ge 30) {
        Write-Host "[正常] 设备令牌可以在本机安全解密" -ForegroundColor Green
    }
    else {
        Write-Host "[失败] 设备令牌格式不正确；安装时应输入 cwsdt_ 开头的密钥" -ForegroundColor Red
    }
}
catch {
    Write-Host "[失败] 设备令牌：$($_.Exception.Message)" -ForegroundColor Red
}
finally {
    $DeviceToken = $null
}

$HealthRequest = @{
    Uri        = ([string] $Config.broker_url).TrimEnd("/") + "/healthz"
    Method     = "Get"
    TimeoutSec = 15
}
if ([string] $Config.proxy_url) {
    $HealthRequest.Proxy = [string] $Config.proxy_url
}
try {
    $Health = Invoke-RestMethod @HealthRequest
    Write-Host "[正常] Broker 可访问，账号数：$($Health.accounts)" -ForegroundColor Green
}
catch {
    Write-Host "[失败] Broker/代理连接：$($_.Exception.Message)" -ForegroundColor Red
}

try {
    & (Join-Path $PSScriptRoot "Sync-CwsCodex.ps1") -ConfigPath $ConfigPath -Quiet
    Write-Host "[正常] 员工令牌鉴权和凭据同步成功" -ForegroundColor Green
}
catch {
    Write-Host "[失败] 凭据同步：$($_.Exception.Message)" -ForegroundColor Red
}

foreach ($Name in @("launch.log", "sync.log")) {
    $Path = Join-Path $PSScriptRoot $Name
    if (Test-Path -LiteralPath $Path) {
        Write-Host ""
        Write-Host "最近的 $Name：" -ForegroundColor Yellow
        Get-Content -LiteralPath $Path -Tail 10 -Encoding UTF8
    }
}
