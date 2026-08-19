$ErrorActionPreference = "Stop"

function Write-CwsJsonAtomic {
    param(
        [Parameter(Mandatory = $true)] [string] $Path,
        [Parameter(Mandatory = $true)] $Value
    )

    $Parent = Split-Path -Parent $Path
    if (-not (Test-Path -LiteralPath $Parent)) {
        New-Item -ItemType Directory -Path $Parent -Force | Out-Null
    }
    $TempPath = "$Path.tmp-$PID"
    $Json = $Value | ConvertTo-Json -Depth 20
    $Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($TempPath, $Json + [Environment]::NewLine, $Utf8NoBom)
    Move-Item -LiteralPath $TempPath -Destination $Path -Force
}

function Set-CwsPrivateDirectoryAcl {
    param([Parameter(Mandatory = $true)] [string] $Path)

    if (-not (Test-Path -LiteralPath $Path)) {
        New-Item -ItemType Directory -Path $Path -Force | Out-Null
    }
    $Sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    & icacls.exe $Path /inheritance:r /grant:r "*${Sid}:(OI)(CI)F" /T /C | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to restrict permissions on $Path"
    }
}

function Get-CwsConfig {
    param([Parameter(Mandatory = $true)] [string] $ConfigPath)

    if (-not (Test-Path -LiteralPath $ConfigPath)) {
        throw "CWS Codex configuration does not exist: $ConfigPath"
    }
    return Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
}

function Get-CwsDeviceToken {
    param([Parameter(Mandatory = $true)] [string] $TokenPath)

    if (-not (Test-Path -LiteralPath $TokenPath)) {
        throw "Encrypted device token does not exist. Run Install-CwsCodex.ps1 again."
    }
    $SecureToken = Get-Content -LiteralPath $TokenPath -Raw -Encoding UTF8 | ConvertTo-SecureString
    $Pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecureToken)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($Pointer)
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($Pointer)
    }
}

function Find-CwsVsCode {
    $Candidates = @(
        (Join-Path $env:LOCALAPPDATA "Programs\Microsoft VS Code\Code.exe"),
        (Join-Path $env:ProgramFiles "Microsoft VS Code\Code.exe")
    )
    if (${env:ProgramFiles(x86)}) {
        $Candidates += Join-Path ${env:ProgramFiles(x86)} "Microsoft VS Code\Code.exe"
    }
    foreach ($Candidate in $Candidates) {
        if ($Candidate -and (Test-Path -LiteralPath $Candidate)) {
            return $Candidate
        }
    }
    $Command = Get-Command code.cmd -ErrorAction SilentlyContinue
    if ($Command) {
        return $Command.Source
    }
    return $null
}

function Get-CwsLocalIPv4 {
    $Candidates = New-Object System.Collections.Generic.List[string]
    try {
        foreach ($Network in Get-NetIPConfiguration -ErrorAction Stop) {
            if (-not $Network.IPv4DefaultGateway) {
                continue
            }
            foreach ($Address in @($Network.IPv4Address)) {
                $Value = [string] $Address.IPAddress
                if ($Value -and $Value -notmatch '^(127\.|169\.254\.)') {
                    $Candidates.Add($Value)
                }
            }
        }
    }
    catch {
        try {
            foreach ($Address in [System.Net.Dns]::GetHostAddresses([System.Net.Dns]::GetHostName())) {
                if ($Address.AddressFamily -eq [System.Net.Sockets.AddressFamily]::InterNetwork) {
                    $Value = $Address.ToString()
                    if ($Value -notmatch '^(127\.|169\.254\.)') {
                        $Candidates.Add($Value)
                    }
                }
            }
        }
        catch {
            return $null
        }
    }
    $Unique = @($Candidates | Select-Object -Unique)
    foreach ($Address in $Unique) {
        if ($Address -match '^10\.' -or $Address -match '^192\.168\.' -or $Address -match '^172\.(1[6-9]|2[0-9]|3[01])\.') {
            return $Address
        }
    }
    return $Unique | Select-Object -First 1
}
