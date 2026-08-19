import tarfile
from pathlib import Path

from scripts.build_server_package import ARCHIVE_ROOT, PACKAGE_FILES, build


def test_server_release_archive_contains_only_deployment_material(tmp_path: Path):
    root = Path(__file__).resolve().parents[1]
    output = tmp_path / "server.tar.gz"
    build(root, output)

    with tarfile.open(output, "r:gz") as archive:
        names = set(archive.getnames())

    expected = {
        str(Path(ARCHIVE_ROOT) / destination)
        for destination in PACKAGE_FILES.values()
    }
    assert names == expected
    assert not any("auth.json" in name for name in names)
    assert not any("device-tokens.json" in name for name in names)
    assert not any(name.endswith("broker.env") for name in names)
