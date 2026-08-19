from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_nsis_installer_is_per_user_and_runs_the_existing_configurator():
    definition = (ROOT / "windows-installer" / "CwsCodex.nsi").read_text(encoding="utf-8")
    configurator = (ROOT / "windows-client" / "Install-CwsCodex.ps1").read_text(encoding="utf-8")
    assert "RequestExecutionLevel user" in definition
    assert 'InstallDir "$LOCALAPPDATA\\CWS Codex"' in definition
    assert "Install-CwsCodex.ps1" in definition
    assert "CWS-Codex-Setup-v0.1.1.exe" in definition
    assert "WriteUninstaller" in definition
    assert "CurrentVersion\\Uninstall\\CWS Codex" in definition
    assert 'File /r "${PROJECT_ROOT}/windows-client/*.*"' in definition
    assert "公司 Codex（VS Code）.lnk" in configurator
