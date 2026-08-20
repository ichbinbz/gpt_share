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

function Get-CwsUsageDate {
    param([string] $Timestamp)

    try {
        $Value = [DateTimeOffset]::Parse($Timestamp, [Globalization.CultureInfo]::InvariantCulture)
        return $Value.ToOffset([TimeSpan]::FromHours(8)).ToString("yyyy-MM-dd")
    }
    catch {
        return [DateTimeOffset]::Now.ToOffset([TimeSpan]::FromHours(8)).ToString("yyyy-MM-dd")
    }
}

function Get-CwsUnicodeCharacterCount {
    param([string] $Text)

    if (-not $Text) { return [int64] 0 }
    return [int64] ([Text.Encoding]::UTF32.GetByteCount($Text) / 4)
}

function Get-CwsEstimatedTextTokens {
    param([string] $Text)

    if (-not $Text) { return [int64] 0 }
    $Tokens = [int64] 0
    $AsciiRun = [int64] 0
    foreach ($Character in $Text.ToCharArray()) {
        $Code = [int] $Character
        $IsAsciiWord = ($Code -ge 48 -and $Code -le 57) -or
            ($Code -ge 65 -and $Code -le 90) -or
            ($Code -ge 97 -and $Code -le 122) -or $Code -eq 95
        if ($IsAsciiWord) {
            $AsciiRun += 1
            continue
        }
        if ($AsciiRun -gt 0) {
            $Tokens += [int64] [Math]::Ceiling($AsciiRun / 4.0)
            $AsciiRun = 0
        }
        if (-not [char]::IsWhiteSpace($Character) -and -not [char]::IsSurrogate($Character)) {
            $Tokens += 1
        }
    }
    if ($AsciiRun -gt 0) {
        $Tokens += [int64] [Math]::Ceiling($AsciiRun / 4.0)
    }
    return $Tokens
}

function Get-CwsDailyBucket {
    param(
        [Parameter(Mandatory = $true)] [hashtable] $Buckets,
        [Parameter(Mandatory = $true)] [string] $Date
    )

    if (-not $Buckets.ContainsKey($Date)) {
        $Buckets[$Date] = [ordered]@{
            date                       = $Date
            input_tokens               = [int64] 0
            cached_input_tokens        = [int64] 0
            cache_write_input_tokens   = [int64] 0
            output_tokens              = [int64] 0
            reasoning_output_tokens    = [int64] 0
            total_tokens               = [int64] 0
            user_message_count         = [int64] 0
            user_text_characters       = [int64] 0
            user_text_tokens_estimated = [int64] 0
        }
    }
    return $Buckets[$Date]
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
    $Previous = @{}
    $Daily = @{}
    $UserMessageCount = [int64] 0
    $UserTextCharacters = [int64] 0
    $UserTextTokensEstimated = [int64] 0
    $Stream = $null
    $Reader = $null
    try {
        $Sharing = [System.IO.FileShare]::ReadWrite -bor [System.IO.FileShare]::Delete
        $Stream = New-Object System.IO.FileStream($File.FullName, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, $Sharing)
        $Reader = New-Object System.IO.StreamReader($Stream, [Text.Encoding]::UTF8, $true)
        while (-not $Reader.EndOfStream) {
            $Line = $Reader.ReadLine()
            if ($Line -notmatch '"token_count"' -and $Line -notmatch '"user_message"') { continue }
            try {
                $Event = $Line | ConvertFrom-Json
                if ($Event.payload.type -eq "token_count" -and $Event.payload.info.total_token_usage) {
                    $Latest = $Event.payload.info.total_token_usage
                    $Bucket = Get-CwsDailyBucket -Buckets $Daily -Date (Get-CwsUsageDate $Event.timestamp)
                    foreach ($Field in @("input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens")) {
                        $Current = Get-CwsTokenCount $Latest.$Field
                        $Before = Get-CwsTokenCount $Previous[$Field]
                        $Delta = if ($Current -ge $Before) { $Current - $Before } else { $Current }
                        $Bucket[$Field] += $Delta
                        $Previous[$Field] = $Current
                    }
                }
                elseif ($Event.payload.type -eq "user_message") {
                    $Message = [string] $Event.payload.message
                    $Characters = Get-CwsUnicodeCharacterCount $Message
                    $Estimated = Get-CwsEstimatedTextTokens $Message
                    $UserMessageCount += 1
                    $UserTextCharacters += $Characters
                    $UserTextTokensEstimated += $Estimated
                    $Bucket = Get-CwsDailyBucket -Buckets $Daily -Date (Get-CwsUsageDate $Event.timestamp)
                    $Bucket.user_message_count += 1
                    $Bucket.user_text_characters += $Characters
                    $Bucket.user_text_tokens_estimated += $Estimated
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
    finally {
        if ($Reader) { $Reader.Dispose() }
        elseif ($Stream) { $Stream.Dispose() }
    }
    if ($null -eq $Latest -and $UserMessageCount -eq 0) {
        continue
    }
    if ($null -eq $Latest) { $Latest = [pscustomobject]@{} }
    $Sessions.Add([pscustomobject][ordered]@{
        session_id                = $SessionId
        input_tokens              = (Get-CwsTokenCount $Latest.input_tokens)
        cached_input_tokens       = (Get-CwsTokenCount $Latest.cached_input_tokens)
        cache_write_input_tokens  = (Get-CwsTokenCount $Latest.cache_write_input_tokens)
        output_tokens             = (Get-CwsTokenCount $Latest.output_tokens)
        reasoning_output_tokens   = (Get-CwsTokenCount $Latest.reasoning_output_tokens)
        total_tokens              = (Get-CwsTokenCount $Latest.total_tokens)
        user_message_count         = $UserMessageCount
        user_text_characters       = $UserTextCharacters
        user_text_tokens_estimated = $UserTextTokensEstimated
        daily_usage                = @($Daily.Values | Sort-Object date)
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
