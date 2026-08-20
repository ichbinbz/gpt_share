import zipfile

from scripts.build_windows_release import UTF8_BOM, build


def test_windows_release_contains_installer_and_simple_guide(tmp_path):
    installer = tmp_path / "CWS-Codex-Setup-v0.1.2.exe"
    installer.write_bytes(b"mock installer")
    guide = tmp_path / "guide.txt"
    guide.write_text("安装后从桌面快捷方式启动。", encoding="utf-8")
    output = tmp_path / "release.zip"

    build(installer, guide, output)

    with zipfile.ZipFile(output) as archive:
        assert archive.namelist() == [installer.name, "使用说明.txt"]
        assert archive.read(installer.name) == b"mock installer"
        assert archive.read("使用说明.txt").startswith(UTF8_BOM)
        assert archive.getinfo("使用说明.txt").flag_bits & 0x800
