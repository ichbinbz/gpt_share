#!/usr/bin/env python3
"""Build the authenticated CWS Codex v0.1.7 server release manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Sequence


RELEASE_VERSION = "0.1.7"
ASSET_PLATFORMS = {
    "CWS-Codex-Setup-v0.1.7.exe": "windows-installer",
    "CWS-Codex-Release-v0.1.7.zip": "release-bundle",
    "CWS-Codex-Windows-v0.1.7.zip": "windows",
    "CWS-Codex-Linux-v0.1.7.tar.gz": "linux",
    "CWS-Codex-Server-v0.1.7.tar.gz": "server",
}
ALLOWED_ASSET_NAMES = frozenset(ASSET_PLATFORMS)
DOWNLOAD_PREFIX = "/v1/client/releases/download/"


def valid_asset_name(name: object) -> bool:
    return (
        isinstance(name, str)
        and name in ALLOWED_ASSET_NAMES
        and "/" not in name
        and "\\" not in name
        and ".." not in name
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_manifest(
    version: str,
    release_dir: Path,
    asset_names: list[str],
    published_at: str,
) -> dict[str, Any]:
    if version != RELEASE_VERSION:
        raise ValueError(f"release version must be {RELEASE_VERSION}")
    if not isinstance(published_at, str) or not published_at:
        raise ValueError("published_at must be a non-empty string")
    if len(asset_names) != len(set(asset_names)):
        raise ValueError("asset names must be unique")

    assets: list[dict[str, Any]] = []
    for name in sorted(asset_names):
        if not valid_asset_name(name):
            raise ValueError(f"invalid asset name: {name!r}")
        asset_path = release_dir / name
        stat = asset_path.stat()
        if not asset_path.is_file():
            raise FileNotFoundError(asset_path)
        assets.append(
            {
                "platform": ASSET_PLATFORMS[name],
                "name": name,
                "size": stat.st_size,
                "sha256": sha256_file(asset_path),
                "download_url": f"{DOWNLOAD_PREFIX}{name}",
            }
        )
    return {
        "release_version": version,
        "published_at": published_at,
        "assets": assets,
    }


def atomic_write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o644)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            fd = -1
            json.dump(manifest, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        os.chmod(path, 0o644)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--release-dir", required=True, type=Path)
    parser.add_argument("--published-at", required=True)
    parser.add_argument("asset_names", nargs="+")
    args = parser.parse_args(argv)
    release_dir = args.release_dir.resolve()
    manifest = build_manifest(
        args.version,
        release_dir,
        list(args.asset_names),
        args.published_at,
    )
    atomic_write_manifest(release_dir / "latest.json", manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
