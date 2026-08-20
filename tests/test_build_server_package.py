import tarfile
from pathlib import Path, PurePosixPath

from scripts.build_server_package import ARCHIVE_ROOT, PACKAGE_FILES, build


def test_server_release_archive_contains_only_deployment_material(tmp_path: Path):
    root = Path(__file__).resolve().parents[1]
    output = tmp_path / "server.tar.gz"
    build(root, output)

    with tarfile.open(output, "r:gz") as archive:
        names = set(archive.getnames())
        modes = {member.name: member.mode for member in archive.getmembers()}
        installer = archive.extractfile(str(PurePosixPath(ARCHIVE_ROOT, "install-server.sh"))).read()

    expected = {
        str(PurePosixPath(ARCHIVE_ROOT, destination.as_posix()))
        for destination in PACKAGE_FILES.values()
    }
    assert names == expected
    assert not any("auth.json" in name for name in names)
    assert not any("device-tokens.json" in name for name in names)
    assert not any(name.endswith("broker.env") for name in names)
    assert modes[str(PurePosixPath(ARCHIVE_ROOT, "install-server.sh"))] == 0o755
    assert modes[str(PurePosixPath(ARCHIVE_ROOT, "codex_plus_broker.py"))] == 0o755
    assert modes[str(PurePosixPath(ARCHIVE_ROOT, "README-Server.txt"))] == 0o644
    assert b"\r\n" not in installer
    assert installer.startswith(b"#!/usr/bin/env bash\n")
