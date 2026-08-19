param(
    [string] $ConfigPath = (Join-Path $PSScriptRoot "config.json"),
    [switch] $NewLease,
    [switch] $Quiet
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "Common-CwsCodex.ps1")

$Config = Get-CwsConfig -ConfigPath $ConfigPath
$DeviceToken = Get-CwsDeviceToken -TokenPath (Join-Path $PSScriptRoot "device-token.dpapi")
$CodexHome = [Environment]::ExpandEnvironmentVariables([string] $Config.codex_home)
$LeasePath = Join-Path $CodexHome "cws-lease.json"
$AuthPath = Join-Path $CodexHome "auth.json"

Set-CwsPrivateDirectoryAcl -Path $CodexHome

$LeaseId = $null
if (-not $NewLease -and (Test-Path -LiteralPath $LeasePath)) {
    try {
        $LeaseState = Get-Content -LiteralPath $LeasePath -Raw -Encoding UTF8 | ConvertFrom-Json
        $LeaseId = [string] $LeaseState.lease_id
    }
    catch {
        $LeaseId = $null
    }
}

$Body = @{
    device_id = "$env:COMPUTERNAME\$env:USERNAME"
}
$ClientIp = Get-CwsLocalIPv4
if ($ClientIp) {
    $Body.client_ip = $ClientIp
}
if ($LeaseId) {
    $Body.lease_id = $LeaseId
}

$Request = @{
    Uri         = ([string] $Config.broker_url).TrimEnd("/") + "/v1/lease"
    Method      = "Post"
    Headers     = @{ Authorization = "Bearer $DeviceToken" }
    ContentType = "application/json"
    Body        = ($Body | ConvertTo-Json -Compress)
    TimeoutSec  = 30
}

$ProxyUrl = [string] $Config.proxy_url
if ($ProxyUrl) {
    $Request.Proxy = $ProxyUrl
}

$OldDefaultProxy = [System.Net.WebRequest]::DefaultWebProxy
try {
    if (-not $ProxyUrl) {
        [System.Net.WebRequest]::DefaultWebProxy = $null
    }
    $Lease = Invoke-RestMethod @Request
}
finally {
    [System.Net.WebRequest]::DefaultWebProxy = $OldDefaultProxy
    $DeviceToken = $null
}

if (-not $Lease.access_token -or -not $Lease.account_id -or -not $Lease.lease_id) {
    throw "Broker response is missing official ChatGPT authentication fields."
}

$AuthPayload = [ordered] @{
    auth_mode      = "chatgptAuthTokens"
    OPENAI_API_KEY = $null
    tokens         = [ordered] @{
        id_token     = [string] $Lease.access_token
        access_token = [string] $Lease.access_token
        refresh_token = ""
        account_id   = [string] $Lease.account_id
    }
    last_refresh   = [DateTime]::UtcNow.ToString("o")
}

$LeasePayload = [ordered] @{
    lease_id                = [string] $Lease.lease_id
    lease_expires_at        = $Lease.lease_expires_at
    account_alias           = [string] $Lease.account_alias
    access_token_expires_at = $Lease.access_token_expires_at
}

Write-CwsJsonAtomic -Path $AuthPath -Value $AuthPayload
Write-CwsJsonAtomic -Path $LeasePath -Value $LeasePayload
Set-CwsPrivateDirectoryAcl -Path $CodexHome

if (-not $Quiet) {
    Write-Host "CWS Codex credential synchronized." -ForegroundColor Green
    Write-Host "Account: $($Lease.account_alias); plan: $($Lease.plan_type)"
}
