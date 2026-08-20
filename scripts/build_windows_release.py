#!/usr/bin/env python3
"""Bundle the Windows graphical installer with a short employee guide."""

from __future__ import annotations

import argparse
import zipfile
from pathlib import Path


VERSION = "0.1.4"
UTF8_BOM = b"\xef\xbb\xbf"


def build(installer: Path, guide: Path, output: Path) -> None:
    missing = [str(path) for path in (installer, guide) if not path.is_file()]
    if missing:
        raise FileNotFoundError("missing Windows release files: " + ", ".join(missing))

    guide_payload = guide.read_bytes()
    if not guide_payload.startswith(UTF8_BOM):
        guide_payload = UTF8_BOM + guide_payload

    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.write(installer, arcname=installer.name)
        archive.writestr("使用说明.txt", guide_payload)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--installer",
        type=Path,
        default=Path(f"dist/CWS-Codex-Setup-v{VERSION}.exe"),
    )
    parser.add_argument(
        "--guide",
        type=Path,
        default=Path("windows-client/使用说明.txt"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(f"dist/CWS-Codex-Release-v{VERSION}.zip"),
    )
    args = parser.parse_args()
    build(args.installer, args.guide, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
