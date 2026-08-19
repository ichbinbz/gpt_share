param(
    [string] $ConfigPath = (Join-Path $PSScriptRoot "config.json"),
    [int] $IntervalSeconds = 300
)

$ErrorActionPreference = "Continue"
$LogPath = Join-Path $PSScriptRoot "sync.log"
$CreatedNew = $false
$Sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value.Replace("-", "_")
$Mutex = New-Object System.Threading.Mutex($true, "Local\CWS_Codex_Background_$Sid", [ref] $CreatedNew)
if (-not $CreatedNew) {
    $Mutex.Dispose()
    exit 0
}

try {
    while ($true) {
        try {
            & (Join-Path $PSScriptRoot "Sync-CwsCodex.ps1") -ConfigPath $ConfigPath -Quiet
            & (Join-Path $PSScriptRoot "Report-CwsCodexUsage.ps1") -ConfigPath $ConfigPath -Quiet
        }
        catch {
            $Line = "{0} {1}" -f [DateTime]::Now.ToString("s"), $_.Exception.Message
            Add-Content -LiteralPath $LogPath -Value $Line -Encoding UTF8
        }
        Start-Sleep -Seconds ([Math]::Max(60, $IntervalSeconds))
    }
}
finally {
    $Mutex.ReleaseMutex()
    $Mutex.Dispose()
}
