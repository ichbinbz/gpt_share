#!/usr/bin/env python3
"""Build the CWS Codex Ubuntu/Linux employee client archive."""

from __future__ import annotations

import argparse
import io
import tarfile
from pathlib import Path


VERSION = "0.1.4"
ARCHIVE_ROOT = f"CWS-Codex-Linux-v{VERSION}"
CLIENT_FILES = (
    Path("linux-client/install.sh"),
    Path("linux-client/uninstall.sh"),
    Path("linux-client/cws_codex.py"),
    Path("linux-client/README-Linux.txt"),
)


def build(root: Path, output: Path) -> None:
    missing = [str(path) for path in CLIENT_FILES if not (root / path).is_file()]
    if missing:
        raise FileNotFoundError("missing Linux client files: " + ", ".join(missing))
    output.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(output, "w:gz") as archive:
        for relative in CLIENT_FILES:
            source = root / relative
            info = archive.gettarinfo(str(source), arcname=f"{ARCHIVE_ROOT}/{source.name}")
            info.mode = 0o755 if source.suffix in {".sh", ".py"} else 0o644
            payload = source.read_bytes().replace(b"\r\n", b"\n")
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(f"dist/CWS-Codex-Linux-v{VERSION}.tar.gz"),
    )
    args = parser.parse_args()
    build(args.root.resolve(), args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
