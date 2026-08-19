$ErrorActionPreference = "Stop"

if (Get-Process -Name "Code" -ErrorAction SilentlyContinue) {
    throw "Please fully quit every VS Code window before using this launcher."
}

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$CodexHome = if ($env:CWS_CODEX_HOME) { $env:CWS_CODEX_HOME } else { Join-Path $HOME ".cws-codex" }
$VsCode = if ($env:VSCODE_BIN) {
    $env:VSCODE_BIN
} else {
    Join-Path $env:LOCALAPPDATA "Programs\Microsoft VS Code\Code.exe"
}

if (-not (Test-Path $VsCode)) {
    throw "Visual Studio Code executable not found: $VsCode. Set VSCODE_BIN and retry."
}

python (Join-Path $ScriptDir "codex_plus_sync.py") --codex-home $CodexHome
if ($LASTEXITCODE -ne 0) {
    throw "Failed to synchronize the CWS Codex lease."
}

$env:CODEX_HOME = $CodexHome
Start-Process -FilePath $VsCode -ArgumentList $args
