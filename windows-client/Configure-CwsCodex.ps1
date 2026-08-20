param([string] $ConfigPath = (Join-Path $PSScriptRoot "config.json"))

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "Common-CwsCodex.ps1")

$Config = Get-CwsConfig -ConfigPath $ConfigPath
Write-Host "请选择员工端代理："
Write-Host "  1. 公司内网代理 192.168.2.38:7897（默认）"
Write-Host "  2. 本机 Shadowrocket 127.0.0.1:1082"
Write-Host "  3. 自定义 HTTP/Mixed 代理"
Write-Host "  4. 直连（不使用代理）"
$Choice = Read-Host "输入 1-4"

switch ($Choice) {
    "1" { $Config.proxy_url = "http://192.168.2.38:7897" }
    "2" { $Config.proxy_url = "http://127.0.0.1:1082" }
    "3" { $Config.proxy_url = (Read-Host "输入完整代理地址，例如 http://127.0.0.1:10809").Trim() }
    "4" { $Config.proxy_url = "" }
    default { throw "无效选择。" }
}

Write-CwsJsonAtomic -Path $ConfigPath -Value $Config
Write-Host "代理配置已更新：$($Config.proxy_url)" -ForegroundColor Green
