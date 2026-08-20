param(
    [string] $ConfigPath = (Join-Path $PSScriptRoot "config.json"),
    [switch] $Force
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "Common-CwsCodex.ps1")

$CurrentVersion = [Version] "0.1.5"
$Repository = "ichbinbz/gpt_share"
$ReleaseApiUrl = "https://api.github.com/repos/ichbinbz/gpt_share/releases/latest"
$Config = Get-CwsConfig -ConfigPath $ConfigPath
$ClientHome = Get-CwsClientHome -Config $Config
$StatePath = Join-Path $ClientHome "update-state.json"
$Now = [DateTimeOffset]::UtcNow

if (-not $Force -and (Test-Path -LiteralPath $StatePath)) {
    try {
        $State = Get-Content -LiteralPath $StatePath -Raw -Encoding UTF8 | ConvertFrom-Json
        $LastChecked = [DateTimeOffset]::Parse([string] $State.last_checked_at)
        if (($Now - $LastChecked).TotalHours -lt 24) { return $false }
    }
    catch {
        # Invalid state is replaced after the next successful check.
    }
}

$Request = @{
    Uri        = $ReleaseApiUrl
    Headers    = @{ "User-Agent" = "CWS-Codex-Windows/$CurrentVersion" }
    TimeoutSec = 30
}
$ProxyUrl = [string] $Config.proxy_url
if ($ProxyUrl) { $Request.Proxy = $ProxyUrl }
$Release = Invoke-RestMethod @Request
$Tag = ([string] $Release.tag_name).Trim().TrimStart("v")
$LatestVersion = $null
if (-not [Version]::TryParse($Tag, [ref] $LatestVersion)) {
    throw "GitHub Release 版本号无效：$($Release.tag_name)"
}

Write-CwsJsonAtomic -Path $StatePath -Value ([ordered]@{
    version         = 1
    last_checked_at = $Now.ToString("o")
    latest_version  = $LatestVersion.ToString()
})
if ($LatestVersion -le $CurrentVersion) { return $false }

$AssetName = "CWS-Codex-Setup-v$LatestVersion.exe"
$Asset = @($Release.assets | Where-Object { $_.name -eq $AssetName }) | Select-Object -First 1
if (-not $Asset -or -not $Asset.browser_download_url) {
    throw "GitHub Release 缺少 Windows 安装器：$AssetName"
}
$DownloadUri = [Uri] ([string] $Asset.browser_download_url)
if ($DownloadUri.Scheme -ne "https" -or $DownloadUri.Host -ne "github.com" -or
    -not $DownloadUri.AbsolutePath.StartsWith("/$Repository/releases/download/")) {
    throw "GitHub Release 下载地址未通过安全检查。"
}

Add-Type -AssemblyName System.Windows.Forms
$Choice = [System.Windows.Forms.MessageBox]::Show(
    "检测到 CWS Codex v$LatestVersion。是否立即从 GitHub 下载并自动更新？`r`n`r`n更新会保留设备令牌、VS Code 配置和历史对话。",
    "CWS Codex 客户端更新",
    [System.Windows.Forms.MessageBoxButtons]::YesNo,
    [System.Windows.Forms.MessageBoxIcon]::Information
)
if ($Choice -ne [System.Windows.Forms.DialogResult]::Yes) { return $false }

$DownloadPath = Join-Path ([IO.Path]::GetTempPath()) $AssetName
$Download = @{
    Uri        = $DownloadUri.AbsoluteUri
    OutFile    = $DownloadPath
    Headers    = @{ "User-Agent" = "CWS-Codex-Windows/$CurrentVersion" }
    TimeoutSec = 180
}
if ($ProxyUrl) { $Download.Proxy = $ProxyUrl }
Invoke-WebRequest @Download
try {
    $Digest = [string] $Asset.digest
    if ($Digest.StartsWith("sha256:")) {
        $Expected = $Digest.Substring(7).ToLowerInvariant()
        $Actual = (Get-FileHash -LiteralPath $DownloadPath -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($Actual -ne $Expected) { throw "下载文件 SHA-256 校验失败。" }
    }
    $Process = Start-Process -FilePath $DownloadPath -ArgumentList @("/S", "/AUTOUPDATE") -Wait -PassThru
    if ($Process.ExitCode -ne 0) { throw "自动更新安装器返回错误码 $($Process.ExitCode)。" }
}
finally {
    Remove-Item -LiteralPath $DownloadPath -Force -ErrorAction SilentlyContinue
}
return $true
