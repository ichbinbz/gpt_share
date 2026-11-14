$ErrorActionPreference = "Stop"

Add-Type -AssemblyName System.Security

function Enable-CwsTls12 {
    [Net.ServicePointManager]::SecurityProtocol =
        [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
}

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

function Protect-CwsDeviceToken {
    param(
        [Parameter(Mandatory = $true)] [Security.SecureString] $Token,
        [Parameter(Mandatory = $true)] [string] $Path
    )

    $Pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Token)
    $PlainBytes = $null
    $Entropy = [Text.Encoding]::UTF8.GetBytes("CWS Codex device token v1")
    try {
        $PlainText = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($Pointer)
        $PlainBytes = [Text.Encoding]::UTF8.GetBytes($PlainText)
        $Scope = [System.Security.Cryptography.DataProtectionScope]::CurrentUser
        $Scheme = "dpapi-current-user"
        try {
            $ProtectedBytes = [System.Security.Cryptography.ProtectedData]::Protect($PlainBytes, $Entropy, $Scope)
        }
        catch [System.Security.Cryptography.CryptographicException] {
            $Scope = [System.Security.Cryptography.DataProtectionScope]::LocalMachine
            $Scheme = "dpapi-local-machine"
            $ProtectedBytes = [System.Security.Cryptography.ProtectedData]::Protect($PlainBytes, $Entropy, $Scope)
        }
        $Envelope = [ordered] @{
            version    = 1
            scheme     = $Scheme
            ciphertext = [Convert]::ToBase64String($ProtectedBytes)
        }
        Write-CwsJsonAtomic -Path $Path -Value $Envelope
        return $Scheme
    }
    finally {
        if ($PlainBytes) {
            [Array]::Clear($PlainBytes, 0, $PlainBytes.Length)
        }
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($Pointer)
    }
}

function Set-CwsPrivateDirectoryAcl {
    param([Parameter(Mandatory = $true)] [string] $Path)

    if (-not (Test-Path -LiteralPath $Path)) {
        New-Item -ItemType Directory -Path $Path -Force | Out-Null
    }
    $Sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    & icacls.exe $Path /inheritance:e /grant:r "*${Sid}:(OI)(CI)F" /T /C | Out-Null
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

function Get-CwsAccountChoices {
    param(
        [Parameter(Mandatory = $true)] $Config,
        [Parameter(Mandatory = $true)] [string] $TokenPath
    )

    $BrokerBase = ([string] $Config.broker_url).Trim().TrimEnd("/")
    if (-not $BrokerBase) { throw "配置中缺少 broker_url。" }
    $Token = Get-CwsDeviceToken -TokenPath $TokenPath
    $Request = @{
        Uri                = $BrokerBase + "/v1/client/accounts"
        Headers            = @{ Authorization = "Bearer $Token"; "User-Agent" = "CWS-Codex-Windows/0.1.11" }
        TimeoutSec         = 30
        MaximumRedirection = 0
        DisableKeepAlive   = $true
    }
    if ([string] $Config.proxy_url) { $Request.Proxy = [string] $Config.proxy_url }
    try { return Invoke-RestMethod @Request }
    finally { $Token = $null }
}

function Show-CwsAccountChooser {
    param([Parameter(Mandatory = $true)] $Payload, [string] $PortalUrl)

    $Accounts = @($Payload.accounts | Where-Object { $_.alias })
    $Available = @($Accounts | Where-Object { $_.available -eq $true })
    if ($Available.Count -eq 0) { throw "当前没有可用的 Codex 账号。" }
    Add-Type -AssemblyName System.Windows.Forms
    Add-Type -AssemblyName System.Drawing
    [System.Windows.Forms.Application]::EnableVisualStyles()
    $Form = New-Object System.Windows.Forms.Form
    $Form.Text = "CWS Codex 账号选择"
    $Form.StartPosition = "CenterScreen"
    $Form.Width = 760
    $Form.Height = 460
    $Form.MinimizeBox = $false
    $Form.MaximizeBox = $false
    $Title = New-Object System.Windows.Forms.Label
    $Title.Text = "请选择要使用的账号；推荐账号已预选。"
    $Title.AutoSize = $true
    $Title.Left = 18
    $Title.Top = 16
    $Form.Controls.Add($Title)
    $List = New-Object System.Windows.Forms.ListBox
    $List.Left = 18
    $List.Top = 48
    $List.Width = 704
    $List.Height = 270
    $Rows = @()
    foreach ($Account in $Accounts) {
        $windows = @($Account.windows)
        $remaining = (($windows | ForEach-Object { "$($_.name)余量 $($_.remaining_percent)%" }) -join ", ")
        if (-not $remaining) { $remaining = "余量未知" }
        $state = if ($Account.available -eq $true) { "可用" } else { "不可用：$($Account.status)" }
        $marker = if ($Account.recommended -eq $true -or [string]$Account.alias -eq [string]$Payload.recommended_alias) { "（推荐）" } else { "" }
        $Rows += $Account
        $LoginName = if ($Account.username) { [string] $Account.username } else { [string] $Account.alias }
        [void] $List.Items.Add("$LoginName    $state    $remaining    $marker")
    }
    $recommendedIndex = -1
    for ($i = 0; $i -lt $Rows.Count; $i++) {
        if ($Rows[$i].recommended -eq $true -or [string]$Rows[$i].alias -eq [string]$Payload.recommended_alias) { $recommendedIndex = $i; break }
    }
    if ($recommendedIndex -lt 0) { $recommendedIndex = 0 }
    $List.SelectedIndex = $recommendedIndex
    $Form.Controls.Add($List)
    if ($PortalUrl) {
        $PortalLink = New-Object System.Windows.Forms.LinkLabel
        $PortalLink.Text = "员工账号额度与令牌查询（需连通内网或 SSH 隧道）"
        $PortalLink.Left = 18
        $PortalLink.Top = 328
        $PortalLink.AutoSize = $true
        $PortalLink.Add_LinkClicked({ Start-Process -FilePath $PortalUrl | Out-Null })
        $Form.Controls.Add($PortalLink)
    }
    $Button = New-Object System.Windows.Forms.Button
    $Button.Text = "使用所选账号"
    $Button.Left = 570
    $Button.Top = 361
    $Button.Width = 152
    $Button.Height = 34
    $Button.Add_Click({
        if ($List.SelectedIndex -ge 0 -and $Rows[$List.SelectedIndex].available -eq $true) {
            $Form.Tag = [string] $Rows[$List.SelectedIndex].alias
            $Form.DialogResult = [System.Windows.Forms.DialogResult]::OK
            $Form.Close()
        }
        else { [System.Windows.Forms.MessageBox]::Show("请选择可用账号。", "CWS Codex") | Out-Null }
    })
    $Form.Controls.Add($Button)
    $Form.Add_FormClosing({ if ($Form.DialogResult -ne [System.Windows.Forms.DialogResult]::OK -and -not $Form.Tag) { $_.Cancel = $true } })
    [void] $Form.ShowDialog()
    return [string] $Form.Tag
}

function Get-CwsDeviceToken {
    param([Parameter(Mandatory = $true)] [string] $TokenPath)

    if (-not (Test-Path -LiteralPath $TokenPath)) {
        throw "Encrypted device token does not exist. Run Install-CwsCodex.ps1 again."
    }
    $Serialized = (Get-Content -LiteralPath $TokenPath -Raw -Encoding UTF8).Trim()
    if ($Serialized.StartsWith("{")) {
        $Envelope = $Serialized | ConvertFrom-Json
        if ($Envelope.version -ne 1 -or -not $Envelope.ciphertext) {
            throw "Encrypted device token envelope is invalid."
        }
        switch ([string] $Envelope.scheme) {
            "dpapi-current-user" { $Scope = [System.Security.Cryptography.DataProtectionScope]::CurrentUser }
            "dpapi-local-machine" { $Scope = [System.Security.Cryptography.DataProtectionScope]::LocalMachine }
            default { throw "Encrypted device token scheme is not supported: $($Envelope.scheme)" }
        }
        $Entropy = [Text.Encoding]::UTF8.GetBytes("CWS Codex device token v1")
        $ProtectedBytes = [Convert]::FromBase64String([string] $Envelope.ciphertext)
        $PlainBytes = [System.Security.Cryptography.ProtectedData]::Unprotect($ProtectedBytes, $Entropy, $Scope)
        try {
            return [Text.Encoding]::UTF8.GetString($PlainBytes)
        }
        finally {
            [Array]::Clear($PlainBytes, 0, $PlainBytes.Length)
        }
    }

    # Backward compatibility with the original ConvertFrom-SecureString format.
    $SecureToken = $Serialized | ConvertTo-SecureString
    $Pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecureToken)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($Pointer)
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($Pointer)
    }
}

function Get-CwsClientHome {
    param([Parameter(Mandatory = $true)] $Config)

    $Configured = [string] $Config.client_home
    if (-not $Configured) {
        $Configured = Join-Path $env:USERPROFILE ".cws-codex"
    }
    return [Environment]::ExpandEnvironmentVariables($Configured)
}

function Get-CwsFileSha256 {
    param([Parameter(Mandatory = $true)] [string] $Path)

    $Stream = [System.IO.File]::OpenRead($Path)
    $Hasher = [System.Security.Cryptography.SHA256]::Create()
    try {
        return ([System.BitConverter]::ToString($Hasher.ComputeHash($Stream))).Replace("-", "").ToLowerInvariant()
    }
    finally {
        $Hasher.Dispose()
        $Stream.Dispose()
    }
}

function Get-CwsSessionId {
    param([Parameter(Mandatory = $true)] [string] $Name)

    $Hasher = [System.Security.Cryptography.SHA256]::Create()
    try {
        $Bytes = [System.Text.Encoding]::UTF8.GetBytes($Name)
        return ([System.BitConverter]::ToString($Hasher.ComputeHash($Bytes))).Replace("-", "").ToLowerInvariant()
    }
    finally {
        $Hasher.Dispose()
    }
}

function Initialize-CwsAuthBackup {
    param(
        [Parameter(Mandatory = $true)] [string] $CodexHome,
        [Parameter(Mandatory = $true)] [string] $ClientHome
    )

    Set-CwsPrivateDirectoryAcl -Path $ClientHome
    if (-not (Test-Path -LiteralPath $CodexHome)) {
        New-Item -ItemType Directory -Path $CodexHome -Force | Out-Null
    }
    $StatePath = Join-Path $ClientHome "original-auth-state.json"
    if (Test-Path -LiteralPath $StatePath) {
        return
    }
    $AuthPath = Join-Path $CodexHome "auth.json"
    $BackupPath = Join-Path $ClientHome "original-auth.json"
    $HadAuth = Test-Path -LiteralPath $AuthPath
    if ($HadAuth) {
        Copy-Item -LiteralPath $AuthPath -Destination $BackupPath -Force
    }
    Write-CwsJsonAtomic -Path $StatePath -Value ([ordered] @{
        version  = 1
        had_auth = [bool] $HadAuth
    })
    Set-CwsPrivateDirectoryAcl -Path $ClientHome
}

function Initialize-CwsUsageBaseline {
    param(
        [Parameter(Mandatory = $true)] [string] $CodexHome,
        [Parameter(Mandatory = $true)] [string] $ClientHome
    )

    $BaselinePath = Join-Path $ClientHome "usage-baseline.json"
    if (Test-Path -LiteralPath $BaselinePath) {
        return
    }
    $Excluded = @()
    $SessionsRoot = Join-Path $CodexHome "sessions"
    if (Test-Path -LiteralPath $SessionsRoot) {
        $Excluded = @(
            Get-ChildItem -LiteralPath $SessionsRoot -Filter "*.jsonl" -Recurse -File -ErrorAction SilentlyContinue |
                ForEach-Object { Get-CwsSessionId -Name $_.Name } |
                Sort-Object -Unique
        )
    }
    Write-CwsJsonAtomic -Path $BaselinePath -Value ([ordered] @{
        version              = 1
        excluded_session_ids = $Excluded
    })
    Set-CwsPrivateDirectoryAcl -Path $ClientHome
}

function Set-CwsManagedAuthMarker {
    param(
        [Parameter(Mandatory = $true)] [string] $AuthPath,
        [Parameter(Mandatory = $true)] [string] $ClientHome
    )

    [System.IO.File]::WriteAllText(
        (Join-Path $ClientHome "managed-auth.sha256"),
        (Get-CwsFileSha256 -Path $AuthPath) + [Environment]::NewLine,
        (New-Object System.Text.UTF8Encoding($false))
    )
}

function Restore-CwsOriginalAuth {
    param(
        [Parameter(Mandatory = $true)] [string] $CodexHome,
        [Parameter(Mandatory = $true)] [string] $ClientHome
    )

    $StatePath = Join-Path $ClientHome "original-auth-state.json"
    if (-not (Test-Path -LiteralPath $StatePath)) {
        return $false
    }
    $AuthPath = Join-Path $CodexHome "auth.json"
    $MarkerPath = Join-Path $ClientHome "managed-auth.sha256"
    if ((Test-Path -LiteralPath $AuthPath) -and (Test-Path -LiteralPath $MarkerPath)) {
        $Expected = (Get-Content -LiteralPath $MarkerPath -Raw -Encoding UTF8).Trim()
        if ($Expected -and (Get-CwsFileSha256 -Path $AuthPath) -ne $Expected) {
            Write-Warning "当前 Codex 登录凭据已被其他程序修改，为避免覆盖，未恢复安装前凭据。"
            return $false
        }
    }
    $State = Get-Content -LiteralPath $StatePath -Raw -Encoding UTF8 | ConvertFrom-Json
    $BackupPath = Join-Path $ClientHome "original-auth.json"
    if ([bool] $State.had_auth) {
        if (-not (Test-Path -LiteralPath $BackupPath)) {
            throw "原 Codex 登录凭据备份不存在，无法恢复。"
        }
        Copy-Item -LiteralPath $BackupPath -Destination $AuthPath -Force
    }
    else {
        Remove-Item -LiteralPath $AuthPath -Force -ErrorAction SilentlyContinue
    }
    @($MarkerPath, $BackupPath, $StatePath, (Join-Path $ClientHome "usage-baseline.json")) |
        ForEach-Object { Remove-Item -LiteralPath $_ -Force -ErrorAction SilentlyContinue }
    return $true
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

function Find-CwsVsCodeCli {
    param([string] $VsCodePath)

    if ($VsCodePath) {
        $Expanded = [Environment]::ExpandEnvironmentVariables($VsCodePath)
        if ([System.IO.Path]::GetExtension($Expanded) -ieq ".cmd" -and (Test-Path -LiteralPath $Expanded)) {
            return $Expanded
        }
        $AdjacentCli = Join-Path (Split-Path $Expanded -Parent) "bin\code.cmd"
        if (Test-Path -LiteralPath $AdjacentCli) {
            return $AdjacentCli
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
