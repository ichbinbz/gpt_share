import json
from pathlib import Path

import pytest

from scripts.codex_release_manifest import build_manifest, main


def test_manifest_records_literal_size_and_sha256(tmp_path: Path):
    asset = tmp_path / "CWS-Codex-Setup-v0.1.7.exe"
    asset.write_bytes(b"installer")

    manifest = build_manifest(
        "0.1.7", tmp_path, [asset.name], "2026-09-18T00:00:00Z"
    )

    assert manifest == {
        "release_version": "0.1.7",
        "published_at": "2026-09-18T00:00:00Z",
        "assets": [
            {
                "platform": "windows-installer",
                "name": "CWS-Codex-Setup-v0.1.7.exe",
                "size": 9,
                "sha256": "9c0d294c05fc1d88d698034609bb81c0c69196327594e4c69d2915c80fd9850c",
                "download_url": "/v1/client/releases/download/CWS-Codex-Setup-v0.1.7.exe",
            }
        ],
    }


def test_manifest_sorts_assets_deterministically(tmp_path: Path):
    windows = tmp_path / "CWS-Codex-Windows-v0.1.7.zip"
    linux = tmp_path / "CWS-Codex-Linux-v0.1.7.tar.gz"
    windows.write_bytes(b"windows")
    linux.write_bytes(b"linux")

    manifest = build_manifest(
        "0.1.7", tmp_path, [windows.name, linux.name], "2026-09-18T00:00:00Z"
    )

    assert [asset["name"] for asset in manifest["assets"]] == [linux.name, windows.name]


@pytest.mark.parametrize(
    "name",
    [
        "../CWS-Codex-Setup-v0.1.7.exe",
        "nested/CWS-Codex-Setup-v0.1.7.exe",
        r"nested\CWS-Codex-Setup-v0.1.7.exe",
        "auth.json",
        "CWS-Codex-Setup-v0.1.8.exe",
    ],
)
def test_manifest_rejects_unsafe_or_unlisted_names(tmp_path: Path, name: str):
    with pytest.raises(ValueError, match="asset name"):
        build_manifest("0.1.7", tmp_path, [name], "2026-09-18T00:00:00Z")


def test_manifest_rejects_wrong_release_version(tmp_path: Path):
    asset = tmp_path / "CWS-Codex-Setup-v0.1.7.exe"
    asset.write_bytes(b"installer")

    with pytest.raises(ValueError, match="version"):
        build_manifest("0.1.8", tmp_path, [asset.name], "2026-09-18T00:00:00Z")


def test_cli_atomically_writes_latest_json_without_emitting_asset_contents(
    tmp_path: Path, capsys
):
    asset = tmp_path / "CWS-Codex-Setup-v0.1.7.exe"
    asset.write_bytes(b"secret-installer-bytes")

    assert main(
        [
            "--version",
            "0.1.7",
            "--release-dir",
            str(tmp_path),
            "--published-at",
            "2026-09-18T00:00:00Z",
            asset.name,
        ]
    ) == 0

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    payload = json.loads((tmp_path / "latest.json").read_text(encoding="utf-8"))
    assert payload["assets"][0]["name"] == asset.name
    assert not list(tmp_path.glob(".latest.json.*"))
