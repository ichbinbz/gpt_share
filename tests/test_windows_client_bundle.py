from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WINDOWS_CLIENT = ROOT / "windows-client"
UTF8_BOM = b"\xef\xbb\xbf"


def read(name: str) -> str:
    return (WINDOWS_CLIENT / name).read_text(encoding="utf-8-sig")


def test_powershell_scripts_have_utf8_bom_for_windows_powershell_51():
    scripts = sorted(WINDOWS_CLIENT.glob("*.ps1"))
    assert scripts
    assert all(path.read_bytes().startswith(UTF8_BOM) for path in scripts)


def test_windows_bundle_contains_one_click_entrypoints():
    required = {
        "Install.cmd",
        "安装.cmd",
        "Install-CwsCodex.ps1",
        "Sync-CwsCodex.ps1",
        "Report-CwsCodexUsage.ps1",
        "Update-CwsCodex.ps1",
        "Diagnose-CwsCodex.ps1",
        "Launch-CwsCodex.ps1",
        "Launch-CwsCodex.cmd",
        "Configure-CwsCodex.ps1",
        "Configure-Proxy.cmd",
        "Uninstall-CwsCodex.ps1",
        "Uninstall.cmd",
        "Common-CwsCodex.ps1",
        "Watch-CwsCodex.ps1",
        "README-Windows.txt",
        "管理员签发说明.txt",
        "诊断.cmd",
        "CwsCodex.ico",
    }
    assert required <= {path.name for path in WINDOWS_CLIENT.iterdir()}
    icon = (WINDOWS_CLIENT / "CwsCodex.ico").read_bytes()
    assert icon.startswith(b"\x00\x00\x01\x00")
    assert len(icon) > 10_000


def test_windows_client_uses_official_external_chatgpt_auth_without_refresh_token():
    sync = read("Sync-CwsCodex.ps1")
    assert 'auth_mode      = "chatgptAuthTokens"' in sync
    assert 'refresh_token = ""' in sync
    assert "model_provider" not in sync
    assert "custom provider" not in sync.lower()


def test_device_token_is_dpapi_encrypted_and_not_written_to_config():
    installer = read("Install-CwsCodex.ps1")
    common = read("Common-CwsCodex.ps1")
    assert "Protect-CwsDeviceToken" in installer
    assert "Add-Type -AssemblyName System.Security" in common
    assert "System.Security.Cryptography.DataProtectionScope]::CurrentUser" in common
    assert "System.Security.Cryptography.DataProtectionScope]::LocalMachine" in common
    assert "scheme     = $Scheme" in common
    assert "ConvertTo-SecureString" in common
    assert '"device-token.dpapi"' in installer
    assert "device_token" not in installer.split("$Config =", 1)[1].split("Write-CwsJsonAtomic", 1)[0]
    assert "AsPlainText" not in installer


def test_private_acl_preserves_safe_profile_inheritance():
    common = read("Common-CwsCodex.ps1")
    assert "/inheritance:e" in common
    assert "/inheritance:r" not in common


def test_installer_supports_isolated_local_smoke_test_and_persistent_error_log():
    installer = read("Install-CwsCodex.ps1")
    assert "[Security.SecureString] $DeviceToken" in installer
    assert "[switch] $SkipInitialSync" in installer
    assert "[switch] $SkipBackgroundStart" in installer
    assert "[string] $DesktopDir" in installer
    assert "[string] $StartupDir" in installer
    assert "CWS-Codex-Install.log" in installer
    assert "catch [System.UnauthorizedAccessException]" in installer
    assert ".inaccessible-" in installer


def test_employee_defaults_and_proxy_override_are_present():
    installer = read("Install-CwsCodex.ps1")
    configure = read("Configure-CwsCodex.ps1")
    assert "http://codex.cws.internal:8765" in installer
    assert "http://192.168.2.38:7897" in installer
    assert "http://127.0.0.1:1082" in configure


def test_launcher_reuses_default_vscode_profile_and_default_codex_history():
    installer = read("Install-CwsCodex.ps1")
    launcher = read("Launch-CwsCodex.ps1")
    sync = read("Sync-CwsCodex.ps1")
    assert '[string] $CodexHome = (Join-Path $env:USERPROFILE ".codex")' in installer
    assert '[string] $ClientHome = (Join-Path $env:USERPROFILE ".cws-codex")' in installer
    assert "client_home = $ClientHome" in installer
    assert "Initialize-CwsAuthBackup" in sync
    assert "$LeasePath = Join-Path $ClientHome" in sync
    assert "$AuthPath = Join-Path $CodexHome" in sync
    assert "$env:CODEX_HOME = $CodexHome" in launcher
    assert launcher.index("$env:CODEX_HOME = $CodexHome") < launcher.index("Start-Process -FilePath $VsCodePath")
    assert "--user-data-dir" not in launcher
    assert "vscode-user-data" not in launcher
    assert "--new-window" not in launcher
    assert "Start-Process -FilePath $VsCodePath -ArgumentList" not in launcher
    assert "Start-Process -FilePath $VsCodePath" in launcher
    assert 'Get-Process -Name "Code"' in launcher


def test_original_auth_is_hash_guarded_and_usage_excludes_existing_sessions():
    common = read("Common-CwsCodex.ps1")
    reporter = read("Report-CwsCodexUsage.ps1")
    uninstaller = read("Uninstall-CwsCodex.ps1")
    assert "original-auth-state.json" in common
    assert "original-auth.json" in common
    assert "managed-auth.sha256" in common
    assert "当前 Codex 登录凭据已被其他程序修改" in common
    assert "usage-baseline.json" in common
    assert "$ExcludedSessionIds.Contains($SessionId)" in reporter
    assert "Restore-CwsOriginalAuth" in uninstaller


def test_launcher_opens_vscode_even_when_credential_sync_fails_and_logs_reason():
    launcher = read("Launch-CwsCodex.ps1")
    assert "Credential synchronization failed" in launcher
    assert "仍将启动 VS Code" in launcher
    assert "launch.log" in launcher
    assert launcher.index("catch {") < launcher.index("Start-Process -FilePath $VsCodePath")


def test_diagnostics_checks_vscode_proxy_broker_and_token_without_printing_secret():
    diagnostics = read("Diagnose-CwsCodex.ps1")
    assert "Find-CwsVsCode" in diagnostics
    assert '"/healthz"' in diagnostics
    assert "Sync-CwsCodex.ps1" in diagnostics
    assert "device-token.dpapi" in diagnostics
    assert "Write-Host $DeviceToken" not in diagnostics


def test_usage_reporter_sends_only_cumulative_counters_with_device_token():
    reporter = read("Report-CwsCodexUsage.ps1")
    assert '"token_count"' in reporter
    assert "total_token_usage" in reporter
    assert '"/v1/usage/report"' in reporter
    assert 'Authorization = "Bearer $DeviceToken"' in reporter
    assert "Get-CwsSessionId" in reporter
    assert "prompt" not in reporter.lower()
    assert "message.content" not in reporter.lower()
    assert "user_text_tokens_estimated" in reporter
    assert "daily_usage" in reporter
    assert "Get-CwsEstimatedTextTokens" in reporter
    assert "payload.message" in reporter
    assert "Body.prompt" not in reporter


def test_windows_client_checks_github_release_and_preserves_token_during_auto_update():
    updater = read("Update-CwsCodex.ps1")
    installer = read("Install-CwsCodex.ps1")
    launcher = read("Launch-CwsCodex.ps1")
    assert "api.github.com/repos/ichbinbz/gpt_share/releases/latest" in updater
    assert "CWS-Codex-Setup-v$LatestVersion.exe" in updater
    assert "System.Windows.Forms.MessageBox" in updater
    assert 'ArgumentList @("/S", "/AUTOUPDATE")' in updater
    assert "sha256:" in updater
    assert "[switch] $AutoUpdate" in installer
    assert "$KeepExisting = $AutoUpdate" in installer
    assert 'client_version = $ClientVersion' in installer
    assert '"Update-CwsCodex.ps1"' in installer
    assert "Client auto-update completed" in launcher


def test_background_worker_is_hidden_autostart_and_not_tied_to_vscode_lifetime():
    installer = read("Install-CwsCodex.ps1")
    watcher = read("Watch-CwsCodex.ps1")
    assert '[Environment]::GetFolderPath("Startup")' in installer
    assert "公司 Codex 后台同步.lnk" in installer
    assert "WindowStyle Hidden" in installer
    assert "while ($true)" in watcher
    assert "Report-CwsCodexUsage.ps1" in watcher
    assert 'while (Get-Process -Name "Code"' not in watcher
    assert "System.Threading.Mutex" in watcher


def test_shortcuts_use_chinese_names_custom_icon_and_remove_legacy_names():
    installer = read("Install-CwsCodex.ps1")
    uninstaller = read("Uninstall-CwsCodex.ps1")
    assert "公司 Codex（VS Code）.lnk" in installer
    assert '"CwsCodex.ico"' in installer
    assert '$Shortcut.IconLocation = "$IconPath,0"' in installer
    assert "CWS Codex.lnk" in installer
    assert "CWS Codex Background.lnk" in installer
    assert "公司 Codex（VS Code）.lnk" in uninstaller
    assert "公司 Codex 后台同步.lnk" in uninstaller


def test_windows_client_reports_local_ipv4_with_lease_and_usage():
    common = read("Common-CwsCodex.ps1")
    sync = read("Sync-CwsCodex.ps1")
    reporter = read("Report-CwsCodexUsage.ps1")
    assert "function Get-CwsLocalIPv4" in common
    assert "Get-NetIPConfiguration" in common
    assert "$Body.client_ip = $ClientIp" in sync
    assert "$Body.client_ip = $ClientIp" in reporter
