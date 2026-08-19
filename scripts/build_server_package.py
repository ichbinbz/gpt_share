#!/usr/bin/env python3
"""Build the self-contained CWS Codex Linux server release archive."""

from __future__ import annotations

import argparse
import tarfile
from pathlib import Path


VERSION = "0.1.1"
ARCHIVE_ROOT = f"CWS-Codex-Server-v{VERSION}"
PACKAGE_FILES = {
    Path("server-package/README-Server.txt"): Path("README-Server.txt"),
    Path("server-package/install-server.sh"): Path("install-server.sh"),
    Path("server-package/requirements-server.txt"): Path("requirements-server.txt"),
    Path("scripts/codex_plus_broker.py"): Path("codex_plus_broker.py"),
    Path("scripts/codex_device_tokens.py"): Path("codex_device_tokens.py"),
    Path("scripts/import_employee_tokens_xlsx.py"): Path("import_employee_tokens_xlsx.py"),
    Path("scripts/codex_quota_dashboard.html"): Path("codex_quota_dashboard.html"),
    Path("deploy/cws-codex-broker.env.example"): Path("cws-codex-broker.env.example"),
    Path("deploy/cws-codex-broker.service"): Path("cws-codex-broker.service"),
    Path("CODEX_PLUS_SHARE.zh-CN.md"): Path("CODEX_PLUS_SHARE.zh-CN.md"),
    Path("SERVER_ACCOUNT_MANAGEMENT.zh-CN.md"): Path("SERVER_ACCOUNT_MANAGEMENT.zh-CN.md"),
}


def build(root: Path, output: Path) -> None:
    missing = [str(path) for path in PACKAGE_FILES if not (root / path).is_file()]
    if missing:
        raise FileNotFoundError("missing server package files: " + ", ".join(missing))
    output.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(output, "w:gz") as archive:
        for source, destination in PACKAGE_FILES.items():
            archive.add(root / source, arcname=str(Path(ARCHIVE_ROOT) / destination))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(f"dist/CWS-Codex-Server-v{VERSION}.tar.gz"),
    )
    args = parser.parse_args()
    build(args.root.resolve(), args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
