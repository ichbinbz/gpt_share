param(
    [string] $ConfigPath = (Join-Path $PSScriptRoot "config.json"),
    [switch] $Force,
    [switch] $LibraryMode
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "Common-CwsCodex.ps1")

$CurrentVersion = [Version] "0.1.7"
$Repository = "ichbinbz/gpt_share"
$ReleaseApiUrl = "https://api.github.com/repos/ichbinbz/gpt_share/releases/latest"
$script:CwsUpdateFallbackReason = $null

function ConvertTo-CwsSemanticVersion {
    param([Parameter(Mandatory = $true)] [string] $Value)

    $Normalized = $Value.Trim()
    if ($Normalized -notmatch '^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$') {
        throw "更新版本号不是有效的语义版本：$Value"
    }
    return [Version] $Normalized
}

function ConvertTo-CwsLiteralSize {
    param([Parameter(Mandatory = $true)] $Value)

    $IntegerTypes = @(
        [TypeCode]::SByte,
        [TypeCode]::Byte,
        [TypeCode]::Int16,
        [TypeCode]::UInt16,
        [TypeCode]::Int32,
        [TypeCode]::UInt32,
        [TypeCode]::Int64,
        [TypeCode]::UInt64
    )
    if ($null -eq $Value -or $IntegerTypes -notcontains [Type]::GetTypeCode($Value.GetType())) {
        throw "更新安装器 size 必须是 JSON 整数。"
    }
    try {
        $Size = [long] $Value
    }
    catch {
        throw "更新安装器 size 超出支持范围。"
    }
    if ($Size -lt 0) {
        throw "更新安装器 size 不能为负数。"
    }
    return $Size
}

function Test-CwsSameOriginUri {
    param(
        [Parameter(Mandatory = $true)] [Uri] $Expected,
        [Parameter(Mandatory = $true)] [Uri] $Actual
    )

    return $Expected.Scheme -eq $Actual.Scheme -and
        $Expected.Host -eq $Actual.Host -and
        $Expected.Port -eq $Actual.Port
}

function ConvertTo-CwsSafeUpdateError {
    param(
        [Parameter(Mandatory = $true)] $ErrorRecord,
        [string] $Secret
    )

    $Message = [string] $ErrorRecord.Exception.Message
    if ($Secret) {
        $Message = $Message.Replace($Secret, "[redacted]")
    }
    return [regex]::Replace($Message, '(?i)Bearer\s+[^\s,;]+', 'Bearer [redacted]')
}

function ConvertTo-CwsBrokerCandidate {
    param(
        [Parameter(Mandatory = $true)] $Release,
        [Parameter(Mandatory = $true)] [Uri] $BrokerUri,
        [Parameter(Mandatory = $true)] $Headers
    )

    $LatestVersion = ConvertTo-CwsSemanticVersion -Value ([string] $Release.release_version)
    $AssetName = "CWS-Codex-Setup-v$LatestVersion.exe"
    $Asset = @($Release.assets | Where-Object {
        $_.platform -eq "windows-installer" -and $_.name -eq $AssetName
    }) | Select-Object -First 1
    if (-not $Asset) {
        throw "Broker 发布清单缺少 Windows 安装器：$AssetName"
    }
    if ([string] $Asset.name -cne $AssetName) {
        throw "Broker 发布清单的安装器名称无效。"
    }
    $Size = ConvertTo-CwsLiteralSize -Value $Asset.size
    $Sha256 = [string] $Asset.sha256
    if ($Sha256 -cnotmatch '^[0-9a-f]{64}$') {
        throw "Broker 发布清单的 SHA-256 无效。"
    }
    $DownloadText = [string] $Asset.download_url
    if (-not $DownloadText) {
        throw "Broker 发布清单缺少下载地址。"
    }
    try {
        $DownloadUri = [Uri]::new($BrokerUri, $DownloadText)
    }
    catch {
        throw "Broker 发布清单的下载地址无效。"
    }
    if (-not (Test-CwsSameOriginUri -Expected $BrokerUri -Actual $DownloadUri)) {
        throw "Broker 发布清单的下载地址不是同源地址。"
    }

    return [pscustomobject] [ordered] @{
        source       = "Broker"
        version      = $LatestVersion.ToString()
        name         = $AssetName
        size         = $Size
        sha256       = $Sha256
        download_url = $DownloadUri.AbsoluteUri
        headers      = $Headers
    }
}

function ConvertTo-CwsGithubCandidate {
    param(
        [Parameter(Mandatory = $true)] $Release,
        [Parameter(Mandatory = $true)] $Headers
    )

    $Tag = ([string] $Release.tag_name).Trim()
    if ($Tag.StartsWith("v", [StringComparison]::Ordinal)) {
        $Tag = $Tag.Substring(1)
    }
    $LatestVersion = ConvertTo-CwsSemanticVersion -Value $Tag
    $AssetName = "CWS-Codex-Setup-v$LatestVersion.exe"
    $Asset = @($Release.assets | Where-Object { [string] $_.name -ceq $AssetName }) | Select-Object -First 1
    if (-not $Asset) {
        throw "GitHub Release 缺少 Windows 安装器：$AssetName"
    }
    $Size = ConvertTo-CwsLiteralSize -Value $Asset.size
    $Digest = [string] $Asset.digest
    if ($Digest -cnotmatch '^sha256:[0-9a-f]{64}$') {
        throw "GitHub Release 的 SHA-256 无效。"
    }
    try {
        $DownloadUri = [Uri] ([string] $Asset.browser_download_url)
    }
    catch {
        throw "GitHub Release 下载地址无效。"
    }
    if ($DownloadUri.Scheme -ne "https" -or $DownloadUri.Host -ne "github.com" -or
        $DownloadUri.Port -ne 443 -or $DownloadUri.UserInfo -or
        -not $DownloadUri.AbsolutePath.StartsWith("/$Repository/releases/download/")) {
        throw "GitHub Release 下载地址未通过安全检查。"
    }

    return [pscustomobject] [ordered] @{
        source       = "GitHub"
        version      = $LatestVersion.ToString()
        name         = [string] $Asset.name
        size         = $Size
        sha256       = $Digest.Substring(7)
        download_url = $DownloadUri.AbsoluteUri
        headers      = $Headers
    }
}

function Get-CwsUpdateCandidate {
    param(
        [Parameter(Mandatory = $true)] $Config,
        [Parameter(Mandatory = $true)] [string] $CurrentVersion,
        [Parameter(Mandatory = $true)] [string] $TokenPath,
        [scriptblock] $TokenReader = { param($Path) Get-CwsDeviceToken -TokenPath $Path },
        [scriptblock] $BrokerFetch = { param($Request) Invoke-RestMethod @Request },
        [scriptblock] $GithubFetch = { param($Request) Invoke-RestMethod @Request }
    )

    $script:CwsUpdateFallbackReason = $null
    $DeviceToken = $null
    $BrokerReason = $null
    try {
        $BrokerBase = ([string] $Config.broker_url).Trim().TrimEnd("/")
        if (-not $BrokerBase) {
            throw "配置中缺少 broker_url。"
        }
        $BrokerUri = [Uri] ($BrokerBase + "/")
        if ($BrokerUri.Scheme -ne "http" -and $BrokerUri.Scheme -ne "https") {
            throw "broker_url 必须使用 HTTP 或 HTTPS。"
        }
        $DeviceToken = [string] (& $TokenReader $TokenPath)
        if (-not $DeviceToken) {
            throw "设备令牌为空。"
        }
        $BrokerHeaders = [ordered] @{
            Authorization = "Bearer $DeviceToken"
            "User-Agent" = "CWS-Codex-Windows/$CurrentVersion"
        }
        $BrokerRequest = @{
            Uri                = $BrokerBase + "/v1/client/releases/latest"
            Headers            = $BrokerHeaders
            TimeoutSec         = 30
            MaximumRedirection = 0
            DisableKeepAlive   = $true
        }
        $ProxyUrl = [string] $Config.proxy_url
        if ($ProxyUrl) { $BrokerRequest.Proxy = $ProxyUrl }
        $Release = & $BrokerFetch $BrokerRequest
        return ConvertTo-CwsBrokerCandidate -Release $Release -BrokerUri $BrokerUri -Headers $BrokerHeaders
    }
    catch {
        $BrokerReason = ConvertTo-CwsSafeUpdateError -ErrorRecord $_ -Secret $DeviceToken
        $script:CwsUpdateFallbackReason = $BrokerReason
        Write-Warning "Broker 更新检查失败，改用 GitHub：$BrokerReason"
    }

    try {
        Enable-CwsTls12
        $GithubHeaders = [ordered] @{
            "User-Agent" = "CWS-Codex-Windows/$CurrentVersion"
        }
        $GithubRequest = @{
            Uri        = $ReleaseApiUrl
            Headers    = $GithubHeaders
            TimeoutSec = 30
        }
        $ProxyUrl = [string] $Config.proxy_url
        if ($ProxyUrl) { $GithubRequest.Proxy = $ProxyUrl }
        $Release = & $GithubFetch $GithubRequest
        return ConvertTo-CwsGithubCandidate -Release $Release -Headers $GithubHeaders
    }
    catch {
        $GithubReason = ConvertTo-CwsSafeUpdateError -ErrorRecord $_
        throw "Broker 更新检查失败：$BrokerReason；GitHub 更新检查失败：$GithubReason"
    }
}

function Install-CwsUpdateCandidate {
    param(
        [Parameter(Mandatory = $true)] $Candidate,
        [string] $DownloadPath = (Join-Path ([IO.Path]::GetTempPath()) ([string] $Candidate.name)),
        [string] $ProxyUrl,
        [scriptblock] $DownloadFetch = { param($Request) Invoke-WebRequest @Request },
        [scriptblock] $InstallerStart = {
            param($InstallerPath)
            Start-Process -FilePath $InstallerPath -ArgumentList @("/S", "/AUTOUPDATE") -Wait -PassThru
        }
    )

    $Download = @{
        Uri        = [string] $Candidate.download_url
        OutFile    = $DownloadPath
        Headers    = $Candidate.headers
        TimeoutSec = 180
    }
    if ([string] $Candidate.source -ceq "Broker") {
        $Download.MaximumRedirection = 0
    }
    if ($ProxyUrl) { $Download.Proxy = $ProxyUrl }
    try {
        & $DownloadFetch $Download
        $ActualSize = (Get-Item -LiteralPath $DownloadPath).Length
        if ($ActualSize -ne [long] $Candidate.size) {
            throw "下载文件 size 校验失败：期望 $($Candidate.size)，实际 $ActualSize。"
        }
        $ActualSha256 = Get-CwsFileSha256 -Path $DownloadPath
        if ($ActualSha256 -cne [string] $Candidate.sha256) {
            throw "下载文件 SHA-256 校验失败。"
        }
        $Process = & $InstallerStart $DownloadPath
        if ($Process.ExitCode -ne 0) {
            throw "自动更新安装器返回错误码 $($Process.ExitCode)。"
        }
        return $true
    }
    finally {
        Remove-Item -LiteralPath $DownloadPath -Force -ErrorAction SilentlyContinue
    }
}

if ($LibraryMode) {
    return
}

$Config = Get-CwsConfig -ConfigPath $ConfigPath
$ClientHome = Get-CwsClientHome -Config $Config
$TokenPath = Join-Path $PSScriptRoot "device-token.dpapi"
$StatePath = Join-Path $ClientHome "update-state.json"
$Now = [DateTimeOffset]::UtcNow
$Candidate = Get-CwsUpdateCandidate `
    -Config $Config `
    -CurrentVersion $CurrentVersion.ToString() `
    -TokenPath $TokenPath
$LatestVersion = ConvertTo-CwsSemanticVersion -Value ([string] $Candidate.version)

Write-CwsJsonAtomic -Path $StatePath -Value ([ordered]@{
    version         = 1
    last_checked_at = $Now.ToString("o")
    current_version = $CurrentVersion.ToString()
    latest_version  = $LatestVersion.ToString()
    source          = [string] $Candidate.source
})

Write-Host "CWS Codex 当前版本：$CurrentVersion；更新来源：$($Candidate.source)；最新版本：$LatestVersion"
if ($script:CwsUpdateFallbackReason) {
    Write-Host "Broker 回退原因：$script:CwsUpdateFallbackReason"
}
if ($LatestVersion -le $CurrentVersion) { return $false }

Add-Type -AssemblyName System.Windows.Forms
$Choice = [System.Windows.Forms.MessageBox]::Show(
    "检测到 CWS Codex v$LatestVersion。是否立即从 $($Candidate.source) 下载并自动更新？`r`n`r`n更新会保留设备令牌、VS Code 配置和历史对话。",
    "CWS Codex 客户端更新",
    [System.Windows.Forms.MessageBoxButtons]::YesNo,
    [System.Windows.Forms.MessageBoxIcon]::Information
)
if ($Choice -ne [System.Windows.Forms.DialogResult]::Yes) { return $false }

Install-CwsUpdateCandidate -Candidate $Candidate -ProxyUrl ([string] $Config.proxy_url) | Out-Null
return $true
