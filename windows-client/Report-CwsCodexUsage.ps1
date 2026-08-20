param(
    [string] $ConfigPath = (Join-Path $PSScriptRoot "config.json"),
    [switch] $Quiet
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "Common-CwsCodex.ps1")

function Get-CwsTokenCount {
    param($Value)

    if ($null -eq $Value) {
        return [int64] 0
    }
    try {
        return [Math]::Max([int64] 0, [int64] $Value)
    }
    catch {
        return [int64] 0
    }
}

$Config = Get-CwsConfig -ConfigPath $ConfigPath
$CodexHome = [Environment]::ExpandEnvironmentVariables([string] $Config.codex_home)
$ClientHome = Get-CwsClientHome -Config $Config
$SessionsRoot = Join-Path $CodexHome "sessions"
if (-not (Test-Path -LiteralPath $SessionsRoot)) {
    if (-not $Quiet) {
        Write-Host "No Codex sessions are available to report."
    }
    return
}

$ExcludedSessionIds = New-Object 'System.Collections.Generic.HashSet[string]'
$BaselinePath = Join-Path $ClientHome "usage-baseline.json"
if (Test-Path -LiteralPath $BaselinePath) {
    try {
        $Baseline = Get-Content -LiteralPath $BaselinePath -Raw -Encoding UTF8 | ConvertFrom-Json
        foreach ($SessionId in @($Baseline.excluded_session_ids)) {
            [void] $ExcludedSessionIds.Add([string] $SessionId)
        }
    }
    catch {
        throw "CWS Codex usage baseline is invalid: $BaselinePath"
    }
}

$Sessions = New-Object System.Collections.Generic.List[object]
foreach ($File in Get-ChildItem -LiteralPath $SessionsRoot -Filter "*.jsonl" -Recurse -File -ErrorAction SilentlyContinue) {
    $SessionId = Get-CwsSessionId -Name $File.Name
    if ($ExcludedSessionIds.Contains($SessionId)) {
        continue
    }
    $Latest = $null
    try {
        foreach ($Line in [System.IO.File]::ReadLines($File.FullName)) {
            if ($Line -notmatch '"token_count"') {
                continue
            }
            try {
                $Event = $Line | ConvertFrom-Json
                if ($Event.payload.type -eq "token_count" -and $Event.payload.info.total_token_usage) {
                    $Latest = $Event.payload.info.total_token_usage
                }
            }
            catch {
                # A partially written final JSONL line is retried during the next scan.
            }
        }
    }
    catch {
        continue
    }
    if ($null -eq $Latest) {
        continue
    }
    $Sessions.Add([pscustomobject][ordered]@{
        session_id                = $SessionId
        input_tokens              = (Get-CwsTokenCount $Latest.input_tokens)
        cached_input_tokens       = (Get-CwsTokenCount $Latest.cached_input_tokens)
        cache_write_input_tokens  = (Get-CwsTokenCount $Latest.cache_write_input_tokens)
        output_tokens             = (Get-CwsTokenCount $Latest.output_tokens)
        reasoning_output_tokens   = (Get-CwsTokenCount $Latest.reasoning_output_tokens)
        total_tokens              = (Get-CwsTokenCount $Latest.total_tokens)
    })
}

if ($Sessions.Count -eq 0) {
    if (-not $Quiet) {
        Write-Host "No Codex token counters are available to report."
    }
    return
}

$DeviceToken = Get-CwsDeviceToken -TokenPath (Join-Path $PSScriptRoot "device-token.dpapi")
$ClientIp = Get-CwsLocalIPv4
$ProxyUrl = [string] $Config.proxy_url
$OldDefaultProxy = [System.Net.WebRequest]::DefaultWebProxy
$Reported = 0
try {
    if (-not $ProxyUrl) {
        [System.Net.WebRequest]::DefaultWebProxy = $null
    }
    for ($Offset = 0; $Offset -lt $Sessions.Count; $Offset += 200) {
        $End = [Math]::Min($Offset + 199, $Sessions.Count - 1)
        $Batch = @($Sessions[$Offset..$End])
        $Body = @{ sessions = $Batch }
        if ($ClientIp) {
            $Body.client_ip = $ClientIp
        }
        $Request = @{
            Uri         = ([string] $Config.broker_url).TrimEnd("/") + "/v1/usage/report"
            Method      = "Post"
            Headers     = @{ Authorization = "Bearer $DeviceToken" }
            ContentType = "application/json"
            Body        = ($Body | ConvertTo-Json -Depth 8 -Compress)
            TimeoutSec  = 30
        }
        if ($ProxyUrl) {
            $Request.Proxy = $ProxyUrl
        }
        $Result = Invoke-RestMethod @Request
        $Reported += [int] $Result.accepted_sessions
    }
}
finally {
    [System.Net.WebRequest]::DefaultWebProxy = $OldDefaultProxy
    $DeviceToken = $null
}

if (-not $Quiet) {
    Write-Host "Reported cumulative token counters for $Reported Codex sessions." -ForegroundColor Green
}
