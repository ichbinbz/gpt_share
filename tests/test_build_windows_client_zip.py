import zipfile

from scripts.build_windows_client_zip import UTF8_BOM, build


def test_windows_zip_marks_unicode_names_and_adds_powershell_bom(tmp_path):
    source = tmp_path / "windows-client"
    source.mkdir()
    (source / "安装.cmd").write_text("@echo off\n", encoding="utf-8")
    (source / "Installer.ps1").write_text('Write-Host "安装"\n', encoding="utf-8")
    output = tmp_path / "client.zip"

    build(source, output)

    with zipfile.ZipFile(output) as archive:
        unicode_info = archive.getinfo("windows-client/安装.cmd")
        assert unicode_info.flag_bits & 0x800
        assert archive.read("windows-client/Installer.ps1").startswith(UTF8_BOM)
