#!/usr/bin/env python3
"""Build a Windows Explorer- and Windows PowerShell 5.1-compatible client ZIP."""

from __future__ import annotations

import argparse
import zipfile
from pathlib import Path


UTF8_BOM = b"\xef\xbb\xbf"
VERSION = "0.1.2"


def payload_for_windows(path: Path) -> bytes:
    payload = path.read_bytes()
    if path.suffix.lower() in {".ps1", ".txt"} and not payload.startswith(UTF8_BOM):
        return UTF8_BOM + payload
    return payload


def build(source: Path, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(source.iterdir(), key=lambda item: item.name.casefold()):
            if path.is_file() and path.name != ".DS_Store":
                archive.writestr(f"{source.name}/{path.name}", payload_for_windows(path))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("windows-client"))
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(f"dist/CWS-Codex-Windows-v{VERSION}.zip"),
    )
    args = parser.parse_args()
    build(args.source, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
