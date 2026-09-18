import json
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WINDOWS_CLIENT = ROOT / "windows-client"
POWERSHELL = shutil.which("powershell.exe") or shutil.which("powershell")


@unittest.skipUnless(POWERSHELL, "Windows PowerShell is required")
class WindowsUpdateRuntimeTests(unittest.TestCase):
    def test_normal_updater_reads_token_from_install_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory)
            shutil.copy2(WINDOWS_CLIENT / "Update-CwsCodex.ps1", fixture)
            config = fixture / "config.json"
            config.write_text(json.dumps({"broker_url": "http://127.0.0.1:8765", "client_home": str(fixture / "state")}))
            common = r'''
function Get-CwsConfig { param($ConfigPath) Get-Content $ConfigPath -Raw | ConvertFrom-Json }
function Get-CwsClientHome { param($Config) $Config.client_home }
function Get-CwsDeviceToken {
    param($TokenPath)
    [IO.File]::WriteAllText((Join-Path $PSScriptRoot "token-path.txt"), $TokenPath)
    if ($TokenPath -ne (Join-Path $PSScriptRoot "device-token.dpapi")) { throw "Wrong token location" }
    "fixture-token"
}
function Write-CwsJsonAtomic { param($Path, $Value) }
function Enable-CwsTls12 { }
function Invoke-RestMethod {
    param($Uri, $Headers, $TimeoutSec, $MaximumRedirection, $DisableKeepAlive)
    if (-not $DisableKeepAlive) { throw "Broker update connection must close before credential sync" }
    if ($Uri -notlike "*/v1/client/releases/latest") { throw "Unexpected GitHub fallback" }
    [pscustomobject]@{
        release_version="0.1.7"; published_at="2026-09-18T00:00:00Z";
        assets=@([pscustomobject]@{
            platform="windows-installer"; name="CWS-Codex-Setup-v0.1.7.exe"; size=9;
            sha256="9c0d294c05fc1d88d698034609bb81c0c69196327594e4c69d2915c80fd9850c";
            download_url="/v1/client/releases/download/CWS-Codex-Setup-v0.1.7.exe"
        })
    }
}
'''
            (fixture / "Common-CwsCodex.ps1").write_text(common, encoding="utf-8-sig")
            result = self.run_powershell(fixture / "Update-CwsCodex.ps1", "-ConfigPath", config)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((fixture / "token-path.txt").read_text(), str(fixture / "device-token.dpapi"))

    def run_powershell(self, script: Path, *args: Path | str, timeout: int = 10):
        return subprocess.run(
            [
                POWERSHELL,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script),
                *(str(arg) for arg in args),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )

    def run_update_library(self, body: str, timeout: int = 10):
        with tempfile.TemporaryDirectory() as temporary_directory:
            harness = Path(temporary_directory) / "update-harness.ps1"
            harness.write_text(
                textwrap.dedent(
                    f"""\
                    param([string] $UpdaterPath)
                    $ErrorActionPreference = "Stop"
                    $WarningPreference = "SilentlyContinue"
                    [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
                    . $UpdaterPath -LibraryMode
                    {body}
                    """
                ),
                encoding="utf-8-sig",
            )
            return self.run_powershell(
                harness, WINDOWS_CLIENT / "Update-CwsCodex.ps1", timeout=timeout
            )

    def test_tls_initialization_enables_tls12_without_dropping_existing_protocols(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            harness = temporary / "tls-harness.ps1"
            harness.write_text(
                textwrap.dedent(
                    """\
                    param([string] $CommonPath)
                    $ErrorActionPreference = "Stop"
                    [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
                    . $CommonPath
                    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls11
                    Enable-CwsTls12
                    [pscustomobject]@{
                        tls11 = (([Net.ServicePointManager]::SecurityProtocol -band [Net.SecurityProtocolType]::Tls11) -ne 0)
                        tls12 = (([Net.ServicePointManager]::SecurityProtocol -band [Net.SecurityProtocolType]::Tls12) -ne 0)
                    } | ConvertTo-Json -Compress
                    """
                ),
                encoding="utf-8-sig",
            )

            result = self.run_powershell(
                harness, WINDOWS_CLIENT / "Common-CwsCodex.ps1"
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout.strip()), {"tls11": True, "tls12": True})

    def test_launcher_status_mode_displays_installed_version(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            fixture = Path(temporary_directory)
            shutil.copy2(WINDOWS_CLIENT / "Common-CwsCodex.ps1", fixture)
            shutil.copy2(WINDOWS_CLIENT / "Launch-CwsCodex.ps1", fixture)

            markers = {
                "updater": fixture / "updater-invoked.log",
                "sync": fixture / "sync-invoked.log",
                "watcher": fixture / "watcher-invoked.log",
                "vscode": fixture / "vscode-invoked.log",
                "launcher": fixture / "launch.log",
            }
            (fixture / "Update-CwsCodex.ps1").write_text(
                'Add-Content -LiteralPath (Join-Path $PSScriptRoot "updater-invoked.log") -Value "invoked"\nreturn $false\n',
                encoding="utf-8-sig",
            )
            (fixture / "Sync-CwsCodex.ps1").write_text(
                'Add-Content -LiteralPath (Join-Path $PSScriptRoot "sync-invoked.log") -Value "invoked"\n',
                encoding="utf-8-sig",
            )
            (fixture / "Watch-CwsCodex.ps1").write_text(
                'Add-Content -LiteralPath (Join-Path $PSScriptRoot "watcher-invoked.log") -Value "invoked"\n',
                encoding="utf-8-sig",
            )
            fake_vscode = fixture / "fake-code.cmd"
            fake_vscode.write_text(
                '@echo off\r\n>"%~dp0vscode-invoked.log" echo invoked\r\n',
                encoding="ascii",
            )
            config_path = fixture / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "client_version": "0.1.7",
                        "client_home": str(fixture / "client-home"),
                        "codex_home": str(fixture / "codex-home"),
                        "vscode_path": str(fake_vscode),
                        "install_extension": False,
                    }
                ),
                encoding="utf-8",
            )
            harness = fixture / "status-harness.ps1"
            harness.write_text(
                textwrap.dedent(
                    """\
                    param([string] $LauncherPath, [string] $ConfigPath)
                    $ErrorActionPreference = "Stop"
                    [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
                    & $LauncherPath -ConfigPath $ConfigPath -StatusOnly
                    """
                ),
                encoding="utf-8-sig",
            )

            result = self.run_powershell(
                harness,
                fixture / "Launch-CwsCodex.ps1",
                config_path,
                timeout=10,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("CWS Codex 员工端 v0.1.7", result.stdout)
            self.assertEqual(
                [name for name, marker in markers.items() if marker.exists()],
                [],
                "status mode must not run updater/sync/watcher/VS Code or write launch.log",
            )

    def test_broker_success_selects_authenticated_candidate_without_github(self):
        result = self.run_update_library(
            r"""
            $Config = [pscustomobject]@{
                broker_url = "https://broker.example.test"
                proxy_url = "http://proxy.example.test:8080"
            }
            $TokenReader = { param($TokenPath) "device-secret" }
            $BrokerFetch = {
                param($Request)
                [pscustomobject]@{
                    release_version = "0.1.7"
                    published_at = "2026-09-18T00:00:00Z"
                    assets = @([pscustomobject]@{
                        platform = "windows-installer"
                        name = "CWS-Codex-Setup-v0.1.7.exe"
                        size = 9
                        sha256 = "9c0d294c05fc1d88d698034609bb81c0c69196327594e4c69d2915c80fd9850c"
                        download_url = "/v1/client/releases/download/CWS-Codex-Setup-v0.1.7.exe"
                    })
                }
            }
            $GithubFetch = { param($Request) throw "GitHub must not be called" }
            $Candidate = Get-CwsUpdateCandidate `
                -Config $Config `
                -CurrentVersion "0.1.6" `
                -TokenPath "unused.dpapi" `
                -TokenReader $TokenReader `
                -BrokerFetch $BrokerFetch `
                -GithubFetch $GithubFetch
            $Candidate | ConvertTo-Json -Depth 10 -Compress
            """
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        candidate = json.loads(result.stdout.strip())
        self.assertEqual(
            candidate,
            {
                "source": "Broker",
                "version": "0.1.7",
                "name": "CWS-Codex-Setup-v0.1.7.exe",
                "size": 9,
                "sha256": "9c0d294c05fc1d88d698034609bb81c0c69196327594e4c69d2915c80fd9850c",
                "download_url": "https://broker.example.test/v1/client/releases/download/CWS-Codex-Setup-v0.1.7.exe",
                "headers": {
                    "Authorization": "Bearer device-secret",
                    "User-Agent": "CWS-Codex-Windows/0.1.6",
                },
            },
        )

    def test_broker_failure_falls_back_to_github_without_authorization(self):
        result = self.run_update_library(
            r"""
            $Config = [pscustomobject]@{
                broker_url = "https://broker.example.test"
                proxy_url = "http://proxy.example.test:8080"
            }
            $script:GithubCalls = 0
            $TokenReader = { param($TokenPath) "device-secret" }
            $BrokerFetch = { param($Request) throw "broker unavailable" }
            $GithubFetch = {
                param($Request)
                $script:GithubCalls++
                [pscustomobject]@{
                    tag_name = "v0.1.7"
                    name = "CWS Codex v0.1.7"
                    assets = @([pscustomobject]@{
                        name = "CWS-Codex-Setup-v0.1.7.exe"
                        size = 9
                        digest = "sha256:9c0d294c05fc1d88d698034609bb81c0c69196327594e4c69d2915c80fd9850c"
                        browser_download_url = "https://github.com/ichbinbz/gpt_share/releases/download/v0.1.7/CWS-Codex-Setup-v0.1.7.exe"
                    })
                }
            }
            $Candidate = Get-CwsUpdateCandidate `
                -Config $Config `
                -CurrentVersion "0.1.6" `
                -TokenPath "unused.dpapi" `
                -TokenReader $TokenReader `
                -BrokerFetch $BrokerFetch `
                -GithubFetch $GithubFetch
            [pscustomobject]@{
                github_calls = $script:GithubCalls
                candidate = $Candidate
            } | ConvertTo-Json -Depth 10 -Compress
            """
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout.strip())
        self.assertEqual(payload["github_calls"], 1)
        self.assertEqual(payload["candidate"]["source"], "GitHub")
        self.assertEqual(payload["candidate"]["version"], "0.1.7")
        self.assertNotIn("Authorization", payload["candidate"]["headers"])
        self.assertEqual(
            payload["candidate"]["headers"],
            {"User-Agent": "CWS-Codex-Windows/0.1.6"},
        )

    def test_broker_and_github_failures_are_reported_together_without_secret(self):
        result = self.run_update_library(
            r"""
            $Config = [pscustomobject]@{
                broker_url = "https://broker.example.test"
                proxy_url = ""
            }
            $TokenReader = { param($TokenPath) "device-secret" }
            $BrokerFetch = { param($Request) throw "broker rejected device-secret" }
            $GithubFetch = { param($Request) throw "github unavailable" }
            try {
                Get-CwsUpdateCandidate `
                    -Config $Config `
                    -CurrentVersion "0.1.6" `
                    -TokenPath "unused.dpapi" `
                    -TokenReader $TokenReader `
                    -BrokerFetch $BrokerFetch `
                    -GithubFetch $GithubFetch | Out-Null
                throw "expected both sources to fail"
            }
            catch {
                [pscustomobject]@{ error = $_.Exception.Message } |
                    ConvertTo-Json -Compress
            }
            """
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        error = json.loads(result.stdout.strip())["error"]
        self.assertIn("broker rejected", error)
        self.assertIn("github unavailable", error)
        self.assertNotIn("device-secret", error)

    def test_invalid_broker_metadata_always_falls_back_to_github(self):
        result = self.run_update_library(
            r"""
            $Config = [pscustomobject]@{
                broker_url = "https://broker.example.test"
                proxy_url = ""
            }
            $TokenReader = { param($TokenPath) "device-secret" }
            $GithubFetch = {
                param($Request)
                $script:GithubCalls++
                [pscustomobject]@{
                    tag_name = "v0.1.7"
                    name = "CWS Codex v0.1.7"
                    assets = @([pscustomobject]@{
                        name = "CWS-Codex-Setup-v0.1.7.exe"
                        size = 9
                        digest = "sha256:9c0d294c05fc1d88d698034609bb81c0c69196327594e4c69d2915c80fd9850c"
                        browser_download_url = "https://github.com/ichbinbz/gpt_share/releases/download/v0.1.7/CWS-Codex-Setup-v0.1.7.exe"
                    })
                }
            }
            $Cases = @(
                [pscustomobject]@{ field = "version"; value = "0.1" },
                [pscustomobject]@{ field = "name"; value = "wrong.exe" },
                [pscustomobject]@{ field = "size"; value = "9" },
                [pscustomobject]@{ field = "size"; value = -1 },
                [pscustomobject]@{ field = "sha256"; value = "9C0D294C05FC1D88D698034609BB81C0C69196327594E4C69D2915C80FD9850C" },
                [pscustomobject]@{ field = "download_url"; value = "https://evil.example.test/CWS-Codex-Setup-v0.1.7.exe" }
            )
            $script:GithubCalls = 0
            $Sources = @()
            foreach ($Case in $Cases) {
                $Release = [pscustomobject]@{
                    release_version = "0.1.7"
                    published_at = "2026-09-18T00:00:00Z"
                    assets = @([pscustomobject]@{
                        platform = "windows-installer"
                        name = "CWS-Codex-Setup-v0.1.7.exe"
                        size = 9
                        sha256 = "9c0d294c05fc1d88d698034609bb81c0c69196327594e4c69d2915c80fd9850c"
                        download_url = "/v1/client/releases/download/CWS-Codex-Setup-v0.1.7.exe"
                    })
                }
                if ($Case.field -eq "version") { $Release.release_version = $Case.value }
                else { $Release.assets[0].($Case.field) = $Case.value }
                $BrokerFetch = { param($Request) $Release }.GetNewClosure()
                $Candidate = Get-CwsUpdateCandidate `
                    -Config $Config `
                    -CurrentVersion "0.1.6" `
                    -TokenPath "unused.dpapi" `
                    -TokenReader $TokenReader `
                    -BrokerFetch $BrokerFetch `
                    -GithubFetch $GithubFetch
                $Sources += $Candidate.source
            }
            [pscustomobject]@{
                github_calls = $script:GithubCalls
                sources = $Sources
            } | ConvertTo-Json -Depth 10 -Compress
            """
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout.strip())
        self.assertEqual(payload["github_calls"], 6)
        self.assertEqual(payload["sources"], ["GitHub"] * 6)

    def test_size_and_sha256_are_verified_before_installer_runs(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            download_path = Path(temporary_directory) / "download.exe"
            result = self.run_update_library(
                rf"""
                $Candidate = [pscustomobject]@{{
                    source = "Broker"
                    version = "0.1.7"
                    name = "CWS-Codex-Setup-v0.1.7.exe"
                    size = 10
                    sha256 = "9c0d294c05fc1d88d698034609bb81c0c69196327594e4c69d2915c80fd9850c"
                    download_url = "https://broker.example.test/v1/client/releases/download/CWS-Codex-Setup-v0.1.7.exe"
                    headers = @{{ Authorization = "Bearer device-secret" }}
                }}
                $script:InstallerStarted = $false
                $DownloadFetch = {{
                    param($Request)
                    [IO.File]::WriteAllBytes($Request.OutFile, [Text.Encoding]::UTF8.GetBytes("installer"))
                }}
                $InstallerStart = {{
                    param($InstallerPath)
                    $script:InstallerStarted = $true
                    [pscustomobject]@{{ ExitCode = 0 }}
                }}
                try {{
                    Install-CwsUpdateCandidate `
                        -Candidate $Candidate `
                        -DownloadPath "{str(download_path).replace('"', '`"')}" `
                        -DownloadFetch $DownloadFetch `
                        -InstallerStart $InstallerStart | Out-Null
                    throw "expected metadata verification to fail"
                }}
                catch {{
                    [pscustomobject]@{{
                        installer_started = $script:InstallerStarted
                        error = $_.Exception.Message
                    }} | ConvertTo-Json -Compress
                }}
                """
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout.strip())
        self.assertFalse(payload["installer_started"])
        self.assertIn("size", payload["error"].lower())

    def test_sha256_mismatch_prevents_installer_from_running(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            download_path = Path(temporary_directory) / "download.exe"
            result = self.run_update_library(
                rf"""
                $Candidate = [pscustomobject]@{{
                    source = "GitHub"
                    version = "0.1.7"
                    name = "CWS-Codex-Setup-v0.1.7.exe"
                    size = 9
                    sha256 = "0000000000000000000000000000000000000000000000000000000000000000"
                    download_url = "https://github.com/ichbinbz/gpt_share/releases/download/v0.1.7/CWS-Codex-Setup-v0.1.7.exe"
                    headers = @{{ "User-Agent" = "CWS-Codex-Windows/0.1.6" }}
                }}
                $script:InstallerStarted = $false
                $DownloadFetch = {{
                    param($Request)
                    [IO.File]::WriteAllBytes($Request.OutFile, [Text.Encoding]::UTF8.GetBytes("installer"))
                }}
                $InstallerStart = {{
                    param($InstallerPath)
                    $script:InstallerStarted = $true
                    [pscustomobject]@{{ ExitCode = 0 }}
                }}
                try {{
                    Install-CwsUpdateCandidate `
                        -Candidate $Candidate `
                        -DownloadPath "{str(download_path).replace('"', '`"')}" `
                        -DownloadFetch $DownloadFetch `
                        -InstallerStart $InstallerStart | Out-Null
                    throw "expected metadata verification to fail"
                }}
                catch {{
                    [pscustomobject]@{{
                        installer_started = $script:InstallerStarted
                        error = $_.Exception.Message
                    }} | ConvertTo-Json -Compress
                }}
                """
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout.strip())
        self.assertFalse(payload["installer_started"])
        self.assertIn("sha-256", payload["error"].lower())

    def test_broker_manifest_request_propagates_proxy_auth_and_disables_redirects(self):
        result = self.run_update_library(
            r"""
            $Config = [pscustomobject]@{
                broker_url = "https://broker.example.test"
                proxy_url = "http://proxy.example.test:8080"
            }
            $script:ObservedRequest = $null
            $TokenReader = { param($TokenPath) "device-secret" }
            $BrokerFetch = {
                param($Request)
                $script:ObservedRequest = $Request
                [pscustomobject]@{
                    release_version = "0.1.7"
                    published_at = "2026-09-18T00:00:00Z"
                    assets = @([pscustomobject]@{
                        platform = "windows-installer"
                        name = "CWS-Codex-Setup-v0.1.7.exe"
                        size = 9
                        sha256 = "9c0d294c05fc1d88d698034609bb81c0c69196327594e4c69d2915c80fd9850c"
                        download_url = "/v1/client/releases/download/CWS-Codex-Setup-v0.1.7.exe"
                    })
                }
            }
            $GithubFetch = { param($Request) throw "GitHub must not be called" }
            $Candidate = Get-CwsUpdateCandidate `
                -Config $Config `
                -CurrentVersion "0.1.6" `
                -TokenPath "unused.dpapi" `
                -TokenReader $TokenReader `
                -BrokerFetch $BrokerFetch `
                -GithubFetch $GithubFetch
            [pscustomobject]@{
                source = $Candidate.source
                uri = [string] $script:ObservedRequest.Uri
                proxy = [string] $script:ObservedRequest.Proxy
                maximum_redirection = $script:ObservedRequest.MaximumRedirection
                authorization = [string] $script:ObservedRequest.Headers.Authorization
                user_agent = [string] $script:ObservedRequest.Headers."User-Agent"
            } | ConvertTo-Json -Compress
            """
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout.strip()),
            {
                "source": "Broker",
                "uri": "https://broker.example.test/v1/client/releases/latest",
                "proxy": "http://proxy.example.test:8080",
                "maximum_redirection": 0,
                "authorization": "Bearer device-secret",
                "user_agent": "CWS-Codex-Windows/0.1.6",
            },
        )

    def test_github_manifest_request_propagates_proxy_without_authorization(self):
        result = self.run_update_library(
            r"""
            $Config = [pscustomobject]@{
                broker_url = "https://broker.example.test"
                proxy_url = "http://proxy.example.test:8080"
            }
            $script:ObservedRequest = $null
            $TokenReader = { param($TokenPath) "device-secret" }
            $BrokerFetch = { param($Request) throw "broker unavailable" }
            $GithubFetch = {
                param($Request)
                $script:ObservedRequest = $Request
                [pscustomobject]@{
                    tag_name = "v0.1.7"
                    name = "CWS Codex v0.1.7"
                    assets = @([pscustomobject]@{
                        name = "CWS-Codex-Setup-v0.1.7.exe"
                        size = 9
                        digest = "sha256:9c0d294c05fc1d88d698034609bb81c0c69196327594e4c69d2915c80fd9850c"
                        browser_download_url = "https://github.com/ichbinbz/gpt_share/releases/download/v0.1.7/CWS-Codex-Setup-v0.1.7.exe"
                    })
                }
            }
            $Candidate = Get-CwsUpdateCandidate `
                -Config $Config `
                -CurrentVersion "0.1.6" `
                -TokenPath "unused.dpapi" `
                -TokenReader $TokenReader `
                -BrokerFetch $BrokerFetch `
                -GithubFetch $GithubFetch
            [pscustomobject]@{
                source = $Candidate.source
                proxy = [string] $script:ObservedRequest.Proxy
                authorization_present = $script:ObservedRequest.Headers.Contains("Authorization")
                user_agent = [string] $script:ObservedRequest.Headers."User-Agent"
            } | ConvertTo-Json -Compress
            """
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout.strip()),
            {
                "source": "GitHub",
                "proxy": "http://proxy.example.test:8080",
                "authorization_present": False,
                "user_agent": "CWS-Codex-Windows/0.1.6",
            },
        )

    def test_broker_download_propagates_proxy_auth_and_disables_redirects(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            download_path = Path(temporary_directory) / "download.exe"
            result = self.run_update_library(
                rf"""
                $Candidate = [pscustomobject]@{{
                    source = "Broker"
                    version = "0.1.7"
                    name = "CWS-Codex-Setup-v0.1.7.exe"
                    size = 9
                    sha256 = "9c0d294c05fc1d88d698034609bb81c0c69196327594e4c69d2915c80fd9850c"
                    download_url = "https://broker.example.test/v1/client/releases/download/CWS-Codex-Setup-v0.1.7.exe"
                    headers = @{{
                        Authorization = "Bearer device-secret"
                        "User-Agent" = "CWS-Codex-Windows/0.1.6"
                    }}
                }}
                $script:ObservedRequest = $null
                $DownloadFetch = {{
                    param($Request)
                    $script:ObservedRequest = $Request
                    [IO.File]::WriteAllBytes($Request.OutFile, [Text.Encoding]::UTF8.GetBytes("installer"))
                }}
                $InstallerStart = {{
                    param($InstallerPath)
                    [pscustomobject]@{{ ExitCode = 0 }}
                }}
                $Installed = Install-CwsUpdateCandidate `
                    -Candidate $Candidate `
                    -DownloadPath "{str(download_path).replace('"', '`"')}" `
                    -ProxyUrl "http://proxy.example.test:8080" `
                    -DownloadFetch $DownloadFetch `
                    -InstallerStart $InstallerStart
                [pscustomobject]@{{
                    installed = $Installed
                    proxy = [string] $script:ObservedRequest.Proxy
                    maximum_redirection = $script:ObservedRequest.MaximumRedirection
                    authorization = [string] $script:ObservedRequest.Headers.Authorization
                    user_agent = [string] $script:ObservedRequest.Headers."User-Agent"
                }} | ConvertTo-Json -Compress
                """
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout.strip()),
            {
                "installed": True,
                "proxy": "http://proxy.example.test:8080",
                "maximum_redirection": 0,
                "authorization": "Bearer device-secret",
                "user_agent": "CWS-Codex-Windows/0.1.6",
            },
        )
        self.assertFalse(download_path.exists())

    def test_partial_download_is_removed_when_fetch_throws(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            download_path = Path(temporary_directory) / "partial.exe"
            result = self.run_update_library(
                rf"""
                $Candidate = [pscustomobject]@{{
                    source = "Broker"
                    version = "0.1.7"
                    name = "CWS-Codex-Setup-v0.1.7.exe"
                    size = 9
                    sha256 = "9c0d294c05fc1d88d698034609bb81c0c69196327594e4c69d2915c80fd9850c"
                    download_url = "https://broker.example.test/v1/client/releases/download/CWS-Codex-Setup-v0.1.7.exe"
                    headers = @{{ Authorization = "Bearer device-secret" }}
                }}
                $DownloadFetch = {{
                    param($Request)
                    [IO.File]::WriteAllBytes($Request.OutFile, [byte[]](1, 2, 3))
                    throw "download interrupted"
                }}
                try {{
                    Install-CwsUpdateCandidate `
                        -Candidate $Candidate `
                        -DownloadPath "{str(download_path).replace('"', '`"')}" `
                        -DownloadFetch $DownloadFetch | Out-Null
                    throw "expected download to fail"
                }}
                catch {{
                    [pscustomobject]@{{
                        error = $_.Exception.Message
                        partial_exists = Test-Path -LiteralPath "{str(download_path).replace('"', '`"')}"
                    }} | ConvertTo-Json -Compress
                }}
                """
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout.strip())
            self.assertIn("download interrupted", payload["error"])
            self.assertFalse(payload["partial_exists"])
            self.assertFalse(download_path.exists())

    def test_github_download_propagates_proxy_without_authorization(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            download_path = Path(temporary_directory) / "download.exe"
            result = self.run_update_library(
                rf"""
                $Candidate = [pscustomobject]@{{
                    source = "GitHub"
                    version = "0.1.7"
                    name = "CWS-Codex-Setup-v0.1.7.exe"
                    size = 9
                    sha256 = "9c0d294c05fc1d88d698034609bb81c0c69196327594e4c69d2915c80fd9850c"
                    download_url = "https://github.com/ichbinbz/gpt_share/releases/download/v0.1.7/CWS-Codex-Setup-v0.1.7.exe"
                    headers = @{{ "User-Agent" = "CWS-Codex-Windows/0.1.6" }}
                }}
                $script:ObservedRequest = $null
                $DownloadFetch = {{
                    param($Request)
                    $script:ObservedRequest = $Request
                    [IO.File]::WriteAllBytes($Request.OutFile, [Text.Encoding]::UTF8.GetBytes("installer"))
                }}
                $InstallerStart = {{
                    param($InstallerPath)
                    [pscustomobject]@{{ ExitCode = 0 }}
                }}
                $Installed = Install-CwsUpdateCandidate `
                    -Candidate $Candidate `
                    -DownloadPath "{str(download_path).replace('"', '`"')}" `
                    -ProxyUrl "http://proxy.example.test:8080" `
                    -DownloadFetch $DownloadFetch `
                    -InstallerStart $InstallerStart
                [pscustomobject]@{{
                    installed = $Installed
                    proxy = [string] $script:ObservedRequest.Proxy
                    authorization_present = $script:ObservedRequest.Headers.Contains("Authorization")
                    user_agent = [string] $script:ObservedRequest.Headers."User-Agent"
                }} | ConvertTo-Json -Compress
                """
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout.strip()),
            {
                "installed": True,
                "proxy": "http://proxy.example.test:8080",
                "authorization_present": False,
                "user_agent": "CWS-Codex-Windows/0.1.6",
            },
        )
        self.assertFalse(download_path.exists())

    def test_github_asset_name_must_match_canonical_case(self):
        result = self.run_update_library(
            r"""
            $Release = [pscustomobject]@{
                tag_name = "v0.1.7"
                name = "CWS Codex v0.1.7"
                assets = @([pscustomobject]@{
                    name = "cws-codex-setup-v0.1.7.exe"
                    size = 9
                    digest = "sha256:9c0d294c05fc1d88d698034609bb81c0c69196327594e4c69d2915c80fd9850c"
                    browser_download_url = "https://github.com/ichbinbz/gpt_share/releases/download/v0.1.7/CWS-Codex-Setup-v0.1.7.exe"
                })
            }
            $Rejected = $false
            try {
                ConvertTo-CwsGithubCandidate `
                    -Release $Release `
                    -Headers @{ "User-Agent" = "CWS-Codex-Windows/0.1.6" } | Out-Null
            }
            catch { $Rejected = $true }
            [pscustomobject]@{ rejected = $Rejected } | ConvertTo-Json -Compress
            """
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout.strip()), {"rejected": True})

    def test_github_download_url_rejects_userinfo_and_nonstandard_port(self):
        result = self.run_update_library(
            r"""
            $Urls = @(
                "https://user@github.com/ichbinbz/gpt_share/releases/download/v0.1.7/CWS-Codex-Setup-v0.1.7.exe",
                "https://github.com:444/ichbinbz/gpt_share/releases/download/v0.1.7/CWS-Codex-Setup-v0.1.7.exe"
            )
            $Rejected = @()
            foreach ($Url in $Urls) {
                $Release = [pscustomobject]@{
                    tag_name = "v0.1.7"
                    name = "CWS Codex v0.1.7"
                    assets = @([pscustomobject]@{
                        name = "CWS-Codex-Setup-v0.1.7.exe"
                        size = 9
                        digest = "sha256:9c0d294c05fc1d88d698034609bb81c0c69196327594e4c69d2915c80fd9850c"
                        browser_download_url = $Url
                    })
                }
                try {
                    ConvertTo-CwsGithubCandidate `
                        -Release $Release `
                        -Headers @{ "User-Agent" = "CWS-Codex-Windows/0.1.6" } | Out-Null
                    $Rejected += $false
                }
                catch { $Rejected += $true }
            }
            [pscustomobject]@{ rejected = $Rejected } | ConvertTo-Json -Compress
            """
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout.strip()), {"rejected": [True, True]})

    def test_github_tag_allows_at_most_one_v_prefix(self):
        result = self.run_update_library(
            r"""
            $Release = [pscustomobject]@{
                tag_name = "vv0.1.7"
                name = "CWS Codex v0.1.7"
                assets = @([pscustomobject]@{
                    name = "CWS-Codex-Setup-v0.1.7.exe"
                    size = 9
                    digest = "sha256:9c0d294c05fc1d88d698034609bb81c0c69196327594e4c69d2915c80fd9850c"
                    browser_download_url = "https://github.com/ichbinbz/gpt_share/releases/download/v0.1.7/CWS-Codex-Setup-v0.1.7.exe"
                })
            }
            $Rejected = $false
            try {
                ConvertTo-CwsGithubCandidate `
                    -Release $Release `
                    -Headers @{ "User-Agent" = "CWS-Codex-Windows/0.1.6" } | Out-Null
            }
            catch { $Rejected = $true }
            [pscustomobject]@{ rejected = $Rejected } | ConvertTo-Json -Compress
            """
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout.strip()), {"rejected": True})

    def test_each_selection_clears_previous_broker_fallback_reason(self):
        result = self.run_update_library(
            r"""
            $Config = [pscustomobject]@{
                broker_url = "https://broker.example.test"
                proxy_url = ""
            }
            $TokenReader = { param($TokenPath) "device-secret" }
            $GithubFetch = {
                param($Request)
                [pscustomobject]@{
                    tag_name = "v0.1.7"
                    name = "CWS Codex v0.1.7"
                    assets = @([pscustomobject]@{
                        name = "CWS-Codex-Setup-v0.1.7.exe"
                        size = 9
                        digest = "sha256:9c0d294c05fc1d88d698034609bb81c0c69196327594e4c69d2915c80fd9850c"
                        browser_download_url = "https://github.com/ichbinbz/gpt_share/releases/download/v0.1.7/CWS-Codex-Setup-v0.1.7.exe"
                    })
                }
            }
            $FailingBrokerFetch = { param($Request) throw "broker unavailable" }
            Get-CwsUpdateCandidate `
                -Config $Config `
                -CurrentVersion "0.1.6" `
                -TokenPath "unused.dpapi" `
                -TokenReader $TokenReader `
                -BrokerFetch $FailingBrokerFetch `
                -GithubFetch $GithubFetch | Out-Null
            $HadFallback = [bool] $script:CwsUpdateFallbackReason

            $SuccessfulBrokerFetch = {
                param($Request)
                [pscustomobject]@{
                    release_version = "0.1.7"
                    published_at = "2026-09-18T00:00:00Z"
                    assets = @([pscustomobject]@{
                        platform = "windows-installer"
                        name = "CWS-Codex-Setup-v0.1.7.exe"
                        size = 9
                        sha256 = "9c0d294c05fc1d88d698034609bb81c0c69196327594e4c69d2915c80fd9850c"
                        download_url = "/v1/client/releases/download/CWS-Codex-Setup-v0.1.7.exe"
                    })
                }
            }
            $Candidate = Get-CwsUpdateCandidate `
                -Config $Config `
                -CurrentVersion "0.1.6" `
                -TokenPath "unused.dpapi" `
                -TokenReader $TokenReader `
                -BrokerFetch $SuccessfulBrokerFetch `
                -GithubFetch $GithubFetch
            [pscustomobject]@{
                had_fallback = $HadFallback
                second_source = $Candidate.source
                fallback_cleared = $null -eq $script:CwsUpdateFallbackReason
            } | ConvertTo-Json -Compress
            """
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout.strip()),
            {
                "had_fallback": True,
                "second_source": "Broker",
                "fallback_cleared": True,
            },
        )


if __name__ == "__main__":
    unittest.main()
