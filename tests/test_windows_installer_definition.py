from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALLER_DEFINITION = ROOT / "windows-installer" / "CwsCodex.nsi"
UTF8_BOM = b"\xef\xbb\xbf"


def test_nsis_definition_has_utf8_bom_for_windows_compiler():
    assert INSTALLER_DEFINITION.read_bytes().startswith(UTF8_BOM)


def test_nsis_installer_is_per_user_and_runs_the_existing_configurator():
    definition = INSTALLER_DEFINITION.read_text(encoding="utf-8-sig")
    configurator = (ROOT / "windows-client" / "Install-CwsCodex.ps1").read_text(encoding="utf-8-sig")
    assert "RequestExecutionLevel user" in definition
    assert 'InstallDir "$LOCALAPPDATA\\CWS Codex"' in definition
    assert "Install-CwsCodex.ps1" in definition
    assert "CWS-Codex-Setup-v0.1.4.exe" in definition
    assert 'VIProductVersion "0.1.4.0"' in definition
    assert '"DisplayVersion" "0.1.4"' in definition
    assert "WriteUninstaller" in definition
    assert "CurrentVersion\\Uninstall\\CWS Codex" in definition
    assert "NSIS_WIN32_MAKENSIS" in definition
    assert '!define PROJECT_SEP "\\"' in definition
    assert '!define PROJECT_SEP "/"' in definition
    assert 'File /r "${PROJECT_ROOT}${PROJECT_SEP}windows-client${PROJECT_SEP}*.*"' in definition
    assert "$TEMP\\CWS-Codex-Install.log" in definition
    assert "公司 Codex（VS Code）.lnk" in configurator
